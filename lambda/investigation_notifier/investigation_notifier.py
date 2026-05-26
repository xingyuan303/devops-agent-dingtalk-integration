"""Lambda: Forward DevOps Agent investigation results to DingTalk."""
import json
import logging
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DINGTALK_SECRET_NAME = os.environ.get("DINGTALK_SECRET_NAME", "")
DINGTALK_CHAT_ID = os.environ.get("DINGTALK_CHAT_ID", "")
DEVOPS_AGENT_SPACE_ID = os.environ.get("DEVOPS_AGENT_SPACE_ID", "")

_secrets_client = None
_dingtalk_creds = None
_devops_client = None
_access_token = ""
_token_expires = 0
_processed_events = set()


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _devops():
    global _devops_client
    if _devops_client is None:
        _devops_client = boto3.client("devops-agent", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _devops_client


def _get_dingtalk_creds():
    global _dingtalk_creds
    if _dingtalk_creds is None and DINGTALK_SECRET_NAME:
        resp = _secrets().get_secret_value(SecretId=DINGTALK_SECRET_NAME)
        _dingtalk_creds = json.loads(resp["SecretString"])
    return _dingtalk_creds or {}


def _get_access_token():
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires:
        return _access_token
    creds = _get_dingtalk_creds()
    if not creds:
        return ""
    payload = json.dumps({
        "appKey": creds["DINGTALK_APP_KEY"],
        "appSecret": creds["DINGTALK_APP_SECRET"],
    }).encode()
    req = Request(
        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            _access_token = result.get("accessToken", "")
            _token_expires = time.time() + result.get("expireIn", 7200) - 300
            return _access_token
    except URLError as e:
        logger.error("Failed to get access_token: %s", e)
        return ""


# ── Summary fetching ───────────────────────────────────────────────────────────
def _get_summary(execution_id, record_type="investigation_summary_md"):
    if not execution_id:
        return None
    for attempt in range(3):
        try:
            response = _devops().list_journal_records(
                agentSpaceId=DEVOPS_AGENT_SPACE_ID,
                executionId=execution_id,
                recordType=record_type,
            )
            for record in response.get("records", []):
                content = record.get("content")
                if isinstance(content, dict):
                    return content.get("text") or content.get("markdown") or str(content)
                if isinstance(content, str):
                    return content
        except Exception as exc:
            logger.error("Failed to fetch %s (attempt %d): %s", record_type, attempt + 1, exc)
            if attempt < 2:
                time.sleep(5)
    return None


def _format_summary(summary_text):
    if not summary_text:
        return ""
    lines = summary_text.strip().split("\n")
    sections = []
    current_heading = None
    current_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("# "):
            if current_heading:
                sections.append((current_heading, current_lines))
            current_heading = stripped.lstrip("# ").strip()
            current_lines = []
        elif current_heading and stripped:
            current_lines.append(stripped)
    if current_heading:
        sections.append((current_heading, current_lines))
    if not sections:
        non_empty = [l.strip() for l in lines if l.strip()]
        return "\n".join(non_empty[:4])
    root_keywords = ["根本原因", "根因", "root cause", "conclusion", "结论"]
    for heading, content in sections:
        if any(kw in heading.lower() for kw in root_keywords):
            return f"**{heading}**\n\n" + "\n".join(content)
    for heading, content in sections:
        if len(content) >= 2:
            return f"**{heading}**\n\n" + "\n".join(content)
    heading, content = sections[0]
    return f"**{heading}**\n\n" + "\n".join(content)


# ── DingTalk sending ───────────────────────────────────────────────────────────
def _send_dingtalk_markdown(title: str, text: str):
    chat_id = DINGTALK_CHAT_ID
    if not chat_id:
        logger.warning("DINGTALK_CHAT_ID not configured")
        return
    token = _get_access_token()
    if not token:
        return
    creds = _get_dingtalk_creds()
    robot_code = creds.get("DINGTALK_APP_KEY", "")

    url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
    payload = json.dumps({
        "robotCode": robot_code,
        "openConversationId": chat_id,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }, ensure_ascii=False).encode()

    req = Request(url, data=payload, headers={
        "Content-Type": "application/json; charset=utf-8",
        "x-acs-dingtalk-access-token": token,
    })
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            logger.info("DingTalk sent: %s", result)
    except URLError as exc:
        logger.error("Failed to send DingTalk: %s", exc)


# ── Entry point ────────────────────────────────────────────────────────────────
def handler(event, context):
    event_id = event.get("id", "")
    if event_id in _processed_events:
        return {"statusCode": 200, "body": "duplicate"}
    _processed_events.add(event_id)
    if len(_processed_events) > 100:
        _processed_events.clear()

    logger.info("Event: %s", json.dumps(event))

    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})
    metadata = detail.get("metadata", {})
    data = detail.get("data", {})

    if not (detail_type.startswith("Investigation") or detail_type.startswith("Mitigation")):
        return {"statusCode": 200, "body": "ignored"}

    task_id = metadata.get("task_id", "")
    execution_id = metadata.get("execution_id", "")
    priority = data.get("priority", "UNKNOWN")

    if detail_type in ("Investigation In Progress", "Investigation Linked"):
        return {"statusCode": 200, "body": "skipped"}

    if detail_type == "Mitigation Completed":
        summary = _get_summary(execution_id, "mitigation_summary_md")
        if summary:
            _send_dingtalk_markdown("修复建议", f"### 🛠 DevOps Agent 修复建议\n\n{_format_summary(summary)}")
        return {"statusCode": 200, "body": "mitigation handled"}

    if detail_type.startswith("Mitigation"):
        return {"statusCode": 200, "body": f"mitigation: {detail_type}"}

    icon = {
        "Investigation Created": "🔍",
        "Investigation Completed": "✅",
        "Investigation Failed": "❌",
        "Investigation Timed Out": "⏱",
    }.get(detail_type, "📋")

    text = (
        f"### {icon} DevOps Agent 调查更新\n\n"
        f"- **状态**: {detail_type}\n"
        f"- **优先级**: {priority}\n"
    )

    if detail_type == "Investigation Completed":
        summary = _get_summary(execution_id) or ""
        if summary:
            text += f"\n{_format_summary(summary)}\n"
        text += f"\n> 💡 如需修复建议，在群里 @Bot 发送：请为调查 {task_id} 生成缓解计划"

    _send_dingtalk_markdown(f"{icon} 调查更新", text)
    return {"statusCode": 200, "body": "ok"}
