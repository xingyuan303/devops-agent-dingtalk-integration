"""
DingTalk Bot — AWS DevOps Agent SRE Chat

Long-lived WebSocket connection via DingTalk Stream protocol.
Forwards @bot messages to DevOps Agent Chat API and streams replies back.

DingTalk Stream protocol:
1. POST /v1.0/gateway/connections/open → get WebSocket endpoint + ticket
2. Connect to wss://... with ticket as query param
3. Receive SYSTEM/CALLBACK/PING events
4. Reply to CALLBACK events with ACK
5. Send proactive messages via OpenAPI /v1.0/robot/groupMessages/send
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import boto3
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dingtalk-bot")

# ── Environment variables ─────────────────────────────────────────────────────
DINGTALK_APP_KEY = os.environ["DINGTALK_APP_KEY"]
DINGTALK_APP_SECRET = os.environ["DINGTALK_APP_SECRET"]
AGENT_SPACE_ID = os.environ["DEVOPS_AGENT_SPACE_ID"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

REPLY_MAX_BYTES = 3500
MAX_BACKOFF_SECONDS = 60
GATEWAY_URL = "https://api.dingtalk.com/v1.0/gateway/connections/open"

# ── AWS DevOps Agent client ───────────────────────────────────────────────────
devops = boto3.client("devops-agent", region_name=AWS_REGION)

# ── Per-session execution ID cache ────────────────────────────────────────────
_sessions: dict[str, str] = {}
_lock = threading.Lock()

# ── DingTalk access token cache ───────────────────────────────────────────────
_access_token: str = ""
_token_expires: float = 0
_token_lock = threading.Lock()


def _get_access_token() -> str:
    global _access_token, _token_expires
    with _token_lock:
        if _access_token and time.time() < _token_expires:
            return _access_token
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = json.dumps({
            "appKey": DINGTALK_APP_KEY,
            "appSecret": DINGTALK_APP_SECRET,
        }).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                _access_token = result.get("accessToken", "")
                _token_expires = time.time() + result.get("expireIn", 7200) - 300
                logger.info("DingTalk access_token refreshed")
                return _access_token
        except Exception as e:
            logger.error("Failed to get access_token: %s", e)
            return ""


def get_or_create_execution(session_key: str) -> str:
    with _lock:
        if session_key not in _sessions:
            resp = devops.create_chat(agentSpaceId=AGENT_SPACE_ID)
            _sessions[session_key] = resp["executionId"]
            logger.info("Created execution %s for session %s", resp["executionId"], session_key)
        return _sessions[session_key]


def ask_devops_agent(session_key: str, query: str) -> str:
    execution_id = get_or_create_execution(session_key)
    try:
        resp = devops.send_message(
            agentSpaceId=AGENT_SPACE_ID,
            executionId=execution_id,
            content=query,
        )
        blocks: dict[int, list[str]] = {}
        for event in resp.get("events", []):
            if "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                idx = block.get("contentBlockIndex", 0)
                delta = block.get("delta", {})
                text_delta = delta.get("textDelta", {})
                if "text" in text_delta:
                    blocks.setdefault(idx, []).append(text_delta["text"])
            elif "responseFailed" in event:
                err = event["responseFailed"]
                return f"DevOps Agent 返回错误：{err.get('errorMessage', 'unknown')}"

        if not blocks:
            return "（DevOps Agent 未返回内容）"
        last_idx = max(blocks.keys())
        return "".join(blocks[last_idx])
    except Exception:
        logger.exception("DevOps Agent call failed")
        with _lock:
            _sessions.pop(session_key, None)
        return "调用 DevOps Agent 失败，已重置会话，请重试。"


# ── UTF-8 chunker ─────────────────────────────────────────────────────────────
def split_utf8_chunks(text: str, max_bytes: int = REPLY_MAX_BYTES) -> list[str]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    buf_bytes = 0
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
    url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
    payload = {
        "robotCode": DINGTALK_APP_KEY,
        "openConversationId": open_conversation_id,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": "DevOps Agent", "text": text}, ensure_ascii=False),
    }
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "x-acs-dingtalk-access-token": token,
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode())
            logger.info("Group message sent: %s", open_conversation_id)
            return True
    except urllib.error.HTTPError as e:
        logger.error("Group message failed: %s %s", e.code, e.read().decode("utf-8", errors="replace")[:300])
        return False


def _send_single_message(user_id: str, text: str) -> bool:
    token = _get_access_token()
    if not token:
        return False
    url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    payload = {
        "robotCode": DINGTALK_APP_KEY,
        "userIds": [user_id],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": "DevOps Agent", "text": text}, ensure_ascii=False),
    }
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "x-acs-dingtalk-access-token": token,
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode())
            logger.info("Single message sent: %s", user_id)
            return True
    except urllib.error.HTTPError as e:
        logger.error("Single message failed: %s", e.code)
        return False


def send_reply(conversation_id: str | None, sender_id: str, conversation_type: str, text: str):
    chunks = split_utf8_chunks(text, REPLY_MAX_BYTES)
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

    def _process():
        reply = ask_devops_agent(session_key, text)
        logger.info("Reply [%s]: %s", session_key, reply[:200])
        send_reply(conversation_id, sender_id, conversation_type, reply)

    threading.Thread(target=_process, daemon=True).start()


# ── DingTalk Stream protocol ──────────────────────────────────────────────────
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
    ws_url = f"{conn['endpoint']}?ticket={conn['ticket']}"
    with ws_connect(ws_url, ping_interval=None) as ws:
        logger.info("Connected to DingTalk Stream")
        for raw in ws:
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


def main():
    logger.info("Starting DingTalk Bot Stream client …")
    attempt = 0
    while True:
        try:
            run_once()
            attempt = 0
        except ConnectionClosed as exc:
            delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            attempt += 1
            logger.warning("WS closed (%s); reconnecting in %ds", exc, delay)
            time.sleep(delay)
        except Exception:
            delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
            attempt += 1
            logger.exception("Error; reconnecting in %ds", delay)
            time.sleep(delay)


if __name__ == "__main__":
    main()
