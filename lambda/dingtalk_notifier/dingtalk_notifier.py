"""Lambda: Forward CloudWatch Alarm alerts to DingTalk and trigger DevOps Agent investigation."""
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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DINGTALK_SECRET_NAME = os.environ.get("DINGTALK_SECRET_NAME", "")
WEBHOOK_SECRET_NAME = os.environ.get("WEBHOOK_SECRET_NAME", "")
DINGTALK_CHAT_ID = os.environ.get("DINGTALK_CHAT_ID", "")

_secrets_client = None
_dingtalk_creds = None
_webhook_creds = None
_access_token = ""
_token_expires = 0


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_dingtalk_creds():
    global _dingtalk_creds
    if _dingtalk_creds is None and DINGTALK_SECRET_NAME:
        resp = _secrets().get_secret_value(SecretId=DINGTALK_SECRET_NAME)
        _dingtalk_creds = json.loads(resp["SecretString"])
    return _dingtalk_creds or {}


def _get_webhook_creds():
    global _webhook_creds
    if _webhook_creds is None and WEBHOOK_SECRET_NAME:
        resp = _secrets().get_secret_value(SecretId=WEBHOOK_SECRET_NAME)
        _webhook_creds = json.loads(resp["SecretString"])
    return _webhook_creds or {}


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

    status = "🔴 ALARM" if new_state == "ALARM" else "🟢 OK"
    summary = f"{namespace}/{metric}: {reason[:200]}" if metric else reason[:300]

    alarm_arn = body.get("AlarmArn", "")
    arn_parts = alarm_arn.split(":")
    alarm_region = arn_parts[3] if len(arn_parts) > 3 else "us-east-1"
    source_url = (
        f"https://{alarm_region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={alarm_region}#alarmsV2:alarm/{alarm_name}"
    )

    markdown = (
        f"### {status} {alarm_name}\n\n"
        f"- **时间**: {timestamp}\n"
        f"- **来源**: CloudWatch ({namespace})\n"
        f"- **摘要**: {summary}\n\n"
        f"[查看监控]({source_url})"
    )
    _send_dingtalk_markdown(f"[ALARM] {alarm_name}", markdown)

    if new_state == "ALARM":
        _trigger_investigation(alarm_name, summary)

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

    signature = hmac.new(
        webhook_secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()

    req = Request(webhook_url, data=payload.encode(), headers={
        "Content-Type": "application/json",
        "X-Signature": signature,
    })

    try:
        with urlopen(req, timeout=15) as resp:
            logger.info("Investigation triggered: %s (status %d)", incident_id, resp.status)
    except URLError as exc:
        logger.error("Failed to trigger investigation: %s", exc)


# ── DingTalk OpenAPI Sending ───────────────────────────────────────────────────
def _send_dingtalk_markdown(title: str, text: str):
    """Send markdown via DingTalk OpenAPI (robot groupMessages)."""
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
            logger.info("DingTalk message sent: %s", result)
    except URLError as exc:
        logger.error("Failed to send DingTalk: %s", exc)
