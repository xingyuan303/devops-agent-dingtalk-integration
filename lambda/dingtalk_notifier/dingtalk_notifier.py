"""Lambda: Forward CloudWatch Alarm alerts to DingTalk and trigger DevOps Agent investigation."""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DINGTALK_SECRET_NAME = os.environ.get("DINGTALK_SECRET_NAME", "")
WEBHOOK_SECRET_NAME = os.environ.get("WEBHOOK_SECRET_NAME", "")

_secrets_client = None
_dingtalk_creds = None
_webhook_creds = None


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


def _sign_dingtalk(secret: str) -> tuple:
    """Generate DingTalk webhook signature (timestamp + sign)."""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


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

    # Build CloudWatch console URL
    alarm_arn = body.get("AlarmArn", "")
    arn_parts = alarm_arn.split(":")
    alarm_region = arn_parts[3] if len(arn_parts) > 3 else "us-east-1"
    source_url = (
        f"https://{alarm_region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={alarm_region}#alarmsV2:alarm/{alarm_name}"
    )

    # Send DingTalk notification
    markdown = (
        f"### {status} {alarm_name}\n\n"
        f"- **时间**: {timestamp}\n"
        f"- **来源**: CloudWatch ({namespace})\n"
        f"- **摘要**: {summary}\n\n"
        f"[查看监控]({source_url})"
    )
    _send_dingtalk_markdown(f"[ALARM] {alarm_name}", markdown)

    # Trigger investigation for ALARM state
    if new_state == "ALARM":
        _trigger_investigation(alarm_name, summary)

    return {"statusCode": 200, "body": json.dumps({"message": f"Processed: {alarm_name}"})}


# ── Trigger DevOps Agent Investigation ─────────────────────────────────────────
def _trigger_investigation(title, description):
    """Call Agent Space Webhook directly to trigger investigation."""
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
        webhook_secret.encode(),
        payload.encode(),
        hashlib.sha256,
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


# ── DingTalk Sending ───────────────────────────────────────────────────────────
def _send_dingtalk_markdown(title: str, text: str):
    """Send markdown message to DingTalk group via custom robot webhook."""
    creds = _get_dingtalk_creds()
    webhook_url = creds.get("DINGTALK_WEBHOOK_URL", "")
    secret = creds.get("DINGTALK_SECRET", "")

    if not webhook_url:
        logger.warning("DingTalk webhook URL not configured")
        return

    # Append signature if secret is configured
    if secret:
        timestamp, sign = _sign_dingtalk(secret)
        webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text,
        },
    }).encode()

    req = Request(webhook_url, data=payload, headers={
        "Content-Type": "application/json",
    })

    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") != 0:
                logger.error("DingTalk send failed: %s", result)
            else:
                logger.info("DingTalk message sent successfully")
    except URLError as exc:
        logger.error("Failed to send DingTalk: %s", exc)
