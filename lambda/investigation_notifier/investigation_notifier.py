"""Lambda: Forward DevOps Agent investigation results to DingTalk."""
import json
import logging
import os
import time

import boto3
from dingtalk_utils import send_dingtalk_markdown

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DINGTALK_SECRET_NAME = os.environ.get("DINGTALK_SECRET_NAME", "")
DINGTALK_CHAT_ID = os.environ.get("DINGTALK_CHAT_ID", "")
DEVOPS_AGENT_SPACE_ID = os.environ.get("DEVOPS_AGENT_SPACE_ID", "")

_devops_client = None
_processed_events = set()


def _devops():
    global _devops_client
    if _devops_client is None:
        _devops_client = boto3.client("devops-agent", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _devops_client


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
    # Find root cause section by keywords
    root_keywords = ["根本原因", "根因", "root cause", "conclusion", "结论"]
    for heading, content in sections:
        if any(kw in heading.lower() for kw in root_keywords):
            return f"**{heading}**\n\n" + "\n".join(content)

    # Fallback: take the last section (typically the conclusion)
    heading, content = sections[-1]
    return f"**{heading}**\n\n" + "\n".join(content)


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
            send_dingtalk_markdown(
                DINGTALK_SECRET_NAME, DINGTALK_CHAT_ID,
                "修复建议", f"### 🛠 DevOps Agent 修复建议\n\n{_format_summary(summary)}")
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

    send_dingtalk_markdown(DINGTALK_SECRET_NAME, DINGTALK_CHAT_ID, f"{icon} 调查更新", text)
    return {"statusCode": 200, "body": "ok"}
