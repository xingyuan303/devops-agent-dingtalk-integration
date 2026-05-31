#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { CloudwatchAlarmAutoRcaStack } from '../lib/cloudwatch-alarm-auto-rca-stack';

const app = new cdk.App();

new CloudwatchAlarmAutoRcaStack(app, 'CloudwatchAlarmAutoRcaStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },

  // ── DingTalk webhook (custom robot) for alarm notifications ─────────────
  // Required. Get from DingTalk group → Settings → Group Robots → Add
  // Custom Robot → enable HMAC signing → copy Webhook URL.
  dingtalkWebhookUrl: app.node.tryGetContext('dingtalkWebhookUrl') || process.env.DINGTALK_WEBHOOK_URL,
  // Required if HMAC signing enabled on the custom robot.
  dingtalkWebhookSecret: app.node.tryGetContext('dingtalkWebhookSecret') || process.env.DINGTALK_WEBHOOK_SECRET,

  // ── DingTalk enterprise app (for bot conversation, optional) ─────────────
  // Get from DingTalk Open Platform → 企业内部应用 → 凭证与基础信息.
  dingtalkAppKey: app.node.tryGetContext('dingtalkAppKey') || process.env.DINGTALK_APP_KEY,
  dingtalkAppSecret: app.node.tryGetContext('dingtalkAppSecret') || process.env.DINGTALK_APP_SECRET,
  // 钉钉事件订阅的回调签名 token (event subscription Aes Key/Token)
  dingtalkAppToken: app.node.tryGetContext('dingtalkAppToken') || process.env.DINGTALK_APP_TOKEN,
  dingtalkAppAesKey: app.node.tryGetContext('dingtalkAppAesKey') || process.env.DINGTALK_APP_AES_KEY,

  // ── DevOps Agent ─────────────────────────────────────────────────────────
  agentSpaceId: app.node.tryGetContext('agentSpaceId') || process.env.AGENT_SPACE_ID,
  devopsAgentWebhookSecretName: app.node.tryGetContext('devopsAgentWebhookSecretName') ||
    'cloudwatch-alarm-auto-rca/devops-agent-webhook',

  // ── Feature flags ────────────────────────────────────────────────────────
  // Disable bot deployment if you only need alarm push (saves cost).
  deployDingtalkBot: app.node.tryGetContext('deployDingtalkBot') !== 'false',
});
