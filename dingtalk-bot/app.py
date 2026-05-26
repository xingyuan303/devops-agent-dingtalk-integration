"""
DingTalk Bot — AWS DevOps Agent SRE Chat

Stream mode: long-lived WebSocket, bidirectional chat with DevOps Agent.
"""

import collections
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request

import boto3
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dingtalk-bot")

# ── Config ────────────────────────────────────────────────────────────────────
DINGTALK_APP_KEY = os.environ["DINGTALK_APP_KEY"]
DINGTALK_APP_SECRET = os.environ["DINGTALK_APP_SECRET"]
AGENT_SPACE_ID = os.environ["DEVOPS_AGENT_SPACE_ID"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

REPLY_MAX_BYTES = 3500
MAX_BACKOFF_SECONDS = 60
MAX_SESSIONS = 200
GATEWAY_URL = "https://api.dingtalk.com/v1.0/gateway/connections/open"

HELP_TEXT = """### 🤖 DevOps Agent Bot 使用说明

- 在群里 **@我** 并输入问题，我会调用 AWS DevOps Agent 进行分析
- 支持多轮对话（同一群聊共享上下文）
- 输入 `/help` 查看本帮助
- 输入 `/reset` 重置当前对话上下文

**示例问题：**
- 为什么 CPU 使用率飙升？
- 最近有哪些告警？
- 请分析 RDS 连接数异常"""

# ── Globals ───────────────────────────────────────────────────────────────────
devops = boto3.client("devops-agent", region_name=AWS_REGION)
_sessions: collections.OrderedDict[str, str] = collections.OrderedDict()
_lock = threading.Lock()
_shutdown = threading.Event()

# ── DingTalk access token ─────────────────────────────────────────────────────
_access_token: str = ""
_token_expires: float = 0
_token_lock = threading.Lock()


def _get_access_token() -> str:
    global _access_token, _token_expires
    with _token_lock:
        if _access_token and time.time() < _token_expires:
            return _access_token
        data = json.dumps({"appKey": DINGTALK_APP_KEY, "appSecret": DINGTALK_APP_SECRET}).encode()
        req = urllib.request.Request(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                _access_token = result.get("accessToken", "")
                _token_expires = time.time() + result.get("expireIn", 7200) - 300
                return _access_token
        except Exception as e:
            logger.error("Failed to get access_token: %s", e)
            return ""


# ── Session management (LRU) ──────────────────────────────────────────────────
def get_or_create_execution(session_key: str) -> str:
    with _lock:
        if session_key in _sessions:
            _sessions.move_to_end(session_key)
            return _sessions[session_key]
        # Evict oldest if at capacity
        while len(_sessions) >= MAX_SESSIONS:
            evicted = _sessions.popitem(last=False)
            logger.info("Evicted session: %s", evicted[0])
        resp = devops.create_chat(agentSpaceId=AGENT_SPACE_ID)
        _sessions[session_key] = resp["executionId"]
        logger.info("Created execution %s for session %s", resp["executionId"], session_key)
        return _sessions[session_key]


def reset_session(session_key: str):
    with _lock:
        _sessions.pop(session_key, None)


def ask_devops_agent(session_key: str, query: str) -> str:
    execution_id = get_or_create_execution(session_key)
    try:
        resp = devops.send_message(agentSpaceId=AGENT_SPACE_ID, executionId=execution_id, content=query)
        blocks: dict[int, list[str]] = {}
        for event in resp.get("events", []):
            if "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                idx = block.get("contentBlockIndex", 0)
                text_delta = block.get("delta", {}).get("textDelta", {})
                if "text" in text_delta:
                    blocks.setdefault(idx, []).append(text_delta["text"])
            elif "responseFailed" in event:
                return f"DevOps Agent 返回错误：{event['responseFailed'].get('errorMessage', 'unknown')}"
        if not blocks:
            return "（DevOps Agent 未返回内容）"
        return "".join(blocks[max(blocks.keys())])
    except Exception:
        logger.exception("DevOps Agent call failed")
        reset_session(session_key)
        return "调用 DevOps Agent 失败，已重置会话，请重试。"


# ── UTF-8 chunker ─────────────────────────────────────────────────────────────
def split_utf8_chunks(text: str, max_bytes: int = REPLY_MAX_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks, buf, buf_bytes = [], [], 0
    for line in text.split("\n"):
        line_bytes = len(line.encode("utf-8")) + 1
        if line_bytes > max_bytes:
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_bytes = [], 0
            enc = line.encode("utf-8")
            for start in range(0, len(enc), max_bytes):
                piece = enc[start:start + max_bytes].decode("utf-8", errors="ignore")
                if piece:
                    chunks.append(piece)
            continue
        if buf_bytes + line_bytes > max_bytes:
            chunks.append("\n".join(buf))
            buf, buf_bytes = [line], line_bytes
        else:
            buf.append(line)
            buf_bytes += line_bytes
    if buf:
        chunks.append("\n".join(buf))
    return chunks


# ── DingTalk messaging ────────────────────────────────────────────────────────
def _send_group_message(open_conversation_id: str, text: str) -> bool:
    token = _get_access_token()
    if not token:
        return False
    payload = json.dumps({
        "robotCode": DINGTALK_APP_KEY,
        "openConversationId": open_conversation_id,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": "DevOps Agent", "text": text}, ensure_ascii=False),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
        data=payload, headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-acs-dingtalk-access-token": token,
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        logger.error("Group msg failed: %s %s", e.code, e.read().decode("utf-8", errors="replace")[:200])
        return False


def _send_single_message(user_id: str, text: str) -> bool:
    token = _get_access_token()
    if not token:
        return False
    payload = json.dumps({
        "robotCode": DINGTALK_APP_KEY,
        "userIds": [user_id],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": "DevOps Agent", "text": text}, ensure_ascii=False),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
        data=payload, headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-acs-dingtalk-access-token": token,
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        logger.error("Single msg failed: %s", e.code)
        return False


def send_reply(conversation_id: str | None, sender_id: str, conversation_type: str, text: str):
    chunks = split_utf8_chunks(text)
    for i, chunk in enumerate(chunks, 1):
        content = f"（{i}/{len(chunks)}）\n{chunk}" if len(chunks) > 1 else chunk
        if conversation_type == "2" and conversation_id:
            _send_group_message(conversation_id, content)
        else:
            _send_single_message(sender_id, content)


# ── Callback handling ─────────────────────────────────────────────────────────
def handle_callback(data: dict):
    conversation_id = data.get("conversationId", "")
    conversation_type = data.get("conversationType", "1")
    sender_id = data.get("senderId", data.get("senderStaffId", ""))
    session_key = conversation_id if conversation_type == "2" else (sender_id or "default")

    text_obj = data.get("text", {})
    text = (text_obj.get("content", "") if isinstance(text_obj, dict) else str(text_obj)).strip()
    if not text:
        return

    logger.info("Received [%s]: %s", session_key, text[:200])

    # Built-in commands
    if text.lower() in ("/help", "帮助"):
        send_reply(conversation_id, sender_id, conversation_type, HELP_TEXT)
        return
    if text.lower() in ("/reset", "重置"):
        reset_session(session_key)
        send_reply(conversation_id, sender_id, conversation_type, "✅ 对话上下文已重置")
        return

    def _process():
        # Instant ACK
        send_reply(conversation_id, sender_id, conversation_type, "收到，正在思考…")
        reply = ask_devops_agent(session_key, text)
        logger.info("Reply [%s]: %s", session_key, reply[:200])
        send_reply(conversation_id, sender_id, conversation_type, reply)

    threading.Thread(target=_process, daemon=True).start()


# ── DingTalk Stream ───────────────────────────────────────────────────────────
def _open_connection() -> dict:
    data = json.dumps({
        "clientId": DINGTALK_APP_KEY,
        "clientSecret": DINGTALK_APP_SECRET,
        "subscriptions": [{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}],
    }).encode()
    req = urllib.request.Request(GATEWAY_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
        endpoint = result.get("endpoint", "")
        ticket = result.get("ticket", "")
        if not endpoint or not ticket:
            raise ValueError(f"Gateway incomplete: {result}")
        logger.info("Gateway opened: %s", endpoint[:80])
        return {"endpoint": endpoint, "ticket": ticket}


def run_once():
    conn = _open_connection()
    with ws_connect(f"{conn['endpoint']}?ticket={conn['ticket']}", ping_interval=None) as ws:
        logger.info("Connected to DingTalk Stream")
        while not _shutdown.is_set():
            try:
                raw = ws.recv(timeout=30)
            except TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type", "")
            headers = msg.get("headers", {})
            data_str = msg.get("data", "")

            if msg_type == "SYSTEM":
                logger.info("System event: %s", headers.get("topic", ""))
            elif msg_type == "PING":
                ws.send(json.dumps({"code": 200, "headers": headers, "message": "OK", "data": data_str}))
            elif msg_type == "CALLBACK":
                ws.send(json.dumps({"code": 200, "headers": headers, "message": "OK", "data": ""}))
                try:
                    data = json.loads(data_str) if isinstance(data_str, str) else data_str
                except (json.JSONDecodeError, TypeError):
                    continue
                handle_callback(data)


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _signal_handler(signum, frame):
    logger.info("Received signal %s, shutting down…", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def main():
    logger.info("Starting DingTalk Bot Stream client …")
    attempt = 0
    while not _shutdown.is_set():
        try:
            run_once()
            attempt = 0
        except ConnectionClosed as exc:
            delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            attempt += 1
            logger.warning("WS closed (%s); reconnecting in %ds", exc, delay)
            _shutdown.wait(delay)
        except Exception:
            delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            attempt += 1
            logger.exception("Error; reconnecting in %ds", delay)
            _shutdown.wait(delay)
    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
