"""Lambda: Forward CloudWatch Alarm alerts to DingTalk and trigger DevOps Agent investigation."""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
from dingtalk_utils import send_dingtalk_markdown

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DINGTALK_SECRET_NAME = os.environ.get("DINGTALK_SECRET_NAME", "")
WEBHOOK_SECRET_NAME = os.environ.get("WEBHOOK_SECRET_NAME", "")
DINGTALK_CHAT_ID = os.environ.get("DINGTALK_CHAT_ID", "")

_secrets_client = None
_webhook_creds = None


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_webhook_creds():
    global _webhook_creds
    if _webhook_creds is None and WEBHOOK_SECRET_NAME:
        resp = _secrets().get_secret_value(SecretId=WEBHOOK_SECRET_NAME)
        _webhook_creds = json.loads(resp["SecretString"])
    return _webhook_creds or {}


# ── Entry point ────────────────────────────────────────────────────────────────
def handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    if "Records" in event and event["Records"][0].get("EventSource") == "aws:sns":
        message = event["Records"][0]["Sns"]["Message"]
        try:
            body = json.loads(message)
            if "AlarmName" in body:
                return _handle_cloudwatch_alarm(body)
        except (json.JSONDecodeError, ValueError):
            pass

    return {"statusCode": 200, "body": "unhandled event type"}


# ── CloudWatch Alarm ───────────────────────────────────────────────────────────
def _handle_cloudwatch_alarm(body):
    alarm_name = body.get("AlarmName", "CloudWatch Alarm")
    new_state = body.get("NewStateValue", "ALARM")
    reason = body.get("NewStateReason", "")
    timestamp = body.get("StateChangeTime", "")
    namespace = body.get("Trigger", {}).get("Namespace", "")
    metric = body.get("Trigger", {}).get("MetricName", "")

    summary = f"{namespace}/{metric}: {reason[:200]}" if metric else reason[:300]

    alarm_arn = body.get("AlarmArn", "")
    arn_parts = alarm_arn.split(":")
    alarm_region = arn_parts[3] if len(arn_parts) > 3 else "us-east-1"
    source_url = (
        f"https://{alarm_region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={alarm_region}#alarmsV2:alarm/{alarm_name}"
    )

    if new_state == "ALARM":
        markdown = (
            f"### 🔴 ALARM {alarm_name}\n\n"
            f"- **时间**: {timestamp}\n"
            f"- **来源**: CloudWatch ({namespace})\n"
            f"- **摘要**: {summary}\n\n"
            f"[查看监控]({source_url})"
        )
        send_dingtalk_markdown(DINGTALK_SECRET_NAME, DINGTALK_CHAT_ID, f"[ALARM] {alarm_name}", markdown)
        _trigger_investigation(alarm_name, summary)
    elif new_state == "OK":
        markdown = (
            f"### 🟢 恢复 {alarm_name}\n\n"
            f"- **时间**: {timestamp}\n"
            f"- **来源**: CloudWatch ({namespace})\n"
            f"- **摘要**: {summary}\n\n"
            f"[查看监控]({source_url})"
        )
        send_dingtalk_markdown(DINGTALK_SECRET_NAME, DINGTALK_CHAT_ID, f"[OK] {alarm_name}", markdown)

    return {"statusCode": 200, "body": json.dumps({"message": f"Processed: {alarm_name}"})}


# ── Trigger DevOps Agent Investigation ─────────────────────────────────────────
def _trigger_investigation(title, description):
    creds = _get_webhook_creds()
    webhook_url = creds.get("WEBHOOK_URL", "")
    webhook_secret = creds.get("WEBHOOK_SECRET", "")

    if not webhook_url or not webhook_secret:
        logger.warning("Webhook credentials not configured, skipping investigation")
        return

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    incident_id = str(uuid.uuid4())

    payload = json.dumps({
        "eventType": "incident",
        "incidentId": incident_id,
        "action": "created",
        "priority": "CRITICAL",
        "title": title,
        "description": description[:2000],
        "timestamp": timestamp,
    })

    # HMAC-SHA256(timestamp:payload, secret) base64 encoded — matches Agent Space spec
    signed_content = f"{timestamp}:{payload}".encode()
    signature = base64.b64encode(
        hmac.new(webhook_secret.encode(), signed_content, hashlib.sha256).digest()
    ).decode()

    req = Request(webhook_url, data=payload.encode(), headers={
        "Content-Type": "application/json",
        "x-amzn-event-timestamp": timestamp,
        "x-amzn-event-signature": signature,
    })

    try:
        with urlopen(req, timeout=15) as resp:
            logger.info("Investigation triggered: %s (status %d)", incident_id, resp.status)
    except URLError as exc:
        logger.error("Failed to trigger investigation: %s", exc)
