"""Shared DingTalk messaging utilities."""
import json
import logging
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3

logger = logging.getLogger(__name__)

_secrets_client = None
_dingtalk_creds = None
_access_token = ""
_token_expires = 0


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def get_dingtalk_creds(secret_name: str) -> dict:
    global _dingtalk_creds
    if _dingtalk_creds is None and secret_name:
        resp = _secrets().get_secret_value(SecretId=secret_name)
        _dingtalk_creds = json.loads(resp["SecretString"])
    return _dingtalk_creds or {}


def get_access_token(secret_name: str) -> str:
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires:
        return _access_token
    creds = get_dingtalk_creds(secret_name)
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


def send_dingtalk_markdown(secret_name: str, chat_id: str, title: str, text: str):
    """Send markdown via DingTalk OpenAPI to one or more groups (comma-separated chat_ids)."""
    if not chat_id:
        logger.warning("DINGTALK_CHAT_ID not configured")
        return
    token = get_access_token(secret_name)
    if not token:
        return
    creds = get_dingtalk_creds(secret_name)
    robot_code = creds.get("DINGTALK_APP_KEY", "")

    for cid in chat_id.split(","):
        cid = cid.strip()
        if not cid:
            continue
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        payload = json.dumps({
            "robotCode": robot_code,
            "openConversationId": cid,
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
                logger.info("DingTalk sent to %s: %s", cid, result)
        except URLError as exc:
            logger.error("Failed to send DingTalk to %s: %s", cid, exc)
