# DevOps Agent → 钉钉通知集成（精简版）

CloudWatch 告警自动触发 AWS DevOps Agent 调查，调查结果推送到钉钉群。

## 架构

```
CloudWatch Alarm (state=ALARM) → SNS → Lambda(dingtalk-notifier) → 钉钉告警通知
                                                ↓
                                    Agent Space Webhook → 自动调查
                                                ↓
                              EventBridge → Lambda(investigation-notifier) → 钉钉调查结果
```

## 前置条件

* AWS DevOps Agent Space 已创建并配置数据源
* 钉钉自定义机器人（群设置 → 智能群助手 → 添加机器人 → 自定义 Webhook）
* Python 3.12+, Node.js 18+, AWS CDK CLI

## 快速部署

### 1. 安装依赖

```bash
pip install -r requirements.txt
npm install -g aws-cdk  # if not installed
```

### 2. 配置

编辑 `cdk.json` 中的 context 值：

```json
{
  "context": {
    "agent_space_id": "your-agent-space-id",
    "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "dingtalk_secret": "SECxxx"
  }
}
```

### 3. 部署

```bash
cdk bootstrap  # first time only
cdk deploy
```

### 4. 填写 Secrets

部署后填写 Secrets Manager 中的凭证：

```bash
aws secretsmanager put-secret-value \
  --secret-id devops-agent/dingtalk-bot \
  --secret-string '{
    "DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "DINGTALK_SECRET": "SECxxx"
  }'

aws secretsmanager put-secret-value \
  --secret-id devops-agent/webhook \
  --secret-string '{
    "WEBHOOK_URL": "your-agent-space-webhook-url",
    "WEBHOOK_SECRET": "your-webhook-hmac-secret"
  }'
```

### 5. 接入 CloudWatch Alarm

将 Alarm 的 AlarmActions 指向部署输出的 SNS Topic ARN：

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "your-alarm" \
  --alarm-actions "<SNSTopicArn from cdk deploy output>" \
  ...
```

### 6. 验证

```bash
aws cloudwatch set-alarm-state \
  --alarm-name "your-alarm" \
  --state-value ALARM \
  --state-reason "Integration test"
```

## 钉钉通知效果

1. 🔴 **告警卡片** — 告警名、来源、摘要 + 监控链接
2. 🔍 **调查已创建** — Agent 开始调查
3. ✅ **调查完成** — 根本原因摘要 + 修复建议提示

## 文件结构

```
├── app.py                              CDK 入口
├── stack.py                            CDK Stack 定义
├── cdk.json                            配置文件
├── requirements.txt                    Python 依赖
└── lambda/
    ├── dingtalk_notifier/              告警通知 + 触发调查
    │   └── dingtalk_notifier.py
    └── investigation_notifier/         调查结果通知
        └── investigation_notifier.py
```

## 卸载

```bash
cdk destroy
```

## 钉钉机器人配置指南

### 方式一：自定义 Webhook 机器人（本项目使用）

用于单向告警通知，无需公网回调端点。

1. 打开目标钉钉群 → 群设置 → 智能群助手 → 添加机器人
2. 选择「自定义」机器人
3. 安全设置选择「加签」，记录生成的 **Secret**（以 `SEC` 开头）
4. 完成后记录 **Webhook URL**（`https://oapi.dingtalk.com/robot/send?access_token=xxx`）
5. 将 Webhook URL 和 Secret 填入 Secrets Manager（见上方步骤 4）

### 方式二：钉钉应用 + Stream 模式（双向对话，可选扩展）

如需在群里 @Bot 与 DevOps Agent 进行双向 SRE 对话，需创建钉钉企业内部应用：

#### 1. 创建钉钉应用

1. 前往 [钉钉开放平台](https://open-dev.dingtalk.com) → 创建企业内部应用
2. 添加「机器人」能力
3. 在「机器人与消息推送」中启用 **Stream 模式**（长连接接收消息）
4. 记录 **App Key** 和 **App Secret**
5. 需要申请权限：`qyapi_robot_sendmsg`（用于主动发送消息）
6. 发布应用上线，将机器人添加到目标群聊

> 钉钉 Stream 模式与企微 aibot 长连接类似：Bot 主动连接钉钉 WebSocket，无需公网回调 URL。

#### 2. Stream 协议流程

```
Bot POST /v1.0/gateway/connections/open → 获取 WebSocket endpoint + ticket
  → 连接 WebSocket
  → 接收 SYSTEM/CALLBACK/PING 事件
  → 通过 OpenAPI 发送回复消息
```

Gateway 订阅配置：`{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}`（注意类型是 CALLBACK 而非 EVENT）。

#### 3. 架构图

```
User (钉钉群聊)
      │  @dingtalk-bot <question>
      ▼
DingTalk Stream  wss://... (gateway/connections/open)
      │  CALLBACK event (bot message)
      ▼
dingtalk-bot Pod/Lambda  (IRSA or IAM Role)
      │  boto3.devops-agent.create_chat / send_message
      ▼
AWS DevOps Agent Space
      │  EventStream response
      ▼
dingtalk-bot  (解析 EventStream → 拆分 3500B)
      │  OpenAPI /v1.0/robot/groupMessages/send  markdown
      ▼
DingTalk ──▶ User
```

#### 4. 故障排查

| 症状 | 原因 | 修复方法 |
|------|------|----------|
| Gateway 返回 `incomplete response` | App Key/Secret 错误或应用未发布 | 检查凭证；确认钉钉应用已发布上线 |
| WebSocket 连接后立即断开 | Stream 模式未启用 | 在钉钉开放平台「机器人与消息推送」中确认已启用 Stream 模式 |
| 群消息发送失败 `status=403` | access_token 过期或权限不足 | 检查 token 刷新是否正常；确认已申请 `qyapi_robot_sendmsg` 权限 |
| WS 断开后迟迟不重连 | DNS 故障或出站阻断 | 确认网络出口到 `api.dingtalk.com:443` 未被策略拦截 |

> 双向对话的完整实现参考 [JoeShi/devops-agent-demo](https://github.com/JoeShi/devops-agent-demo) 中的 `k8s/dingtalk-bot/` 目录。

## 注意事项

* 钉钉自定义机器人需要配置「加签」安全设置，Secret 以 `SEC` 开头
* Webhook URL 和 Secret 从 Agent Space 控制台获取
* `investigation-notifier` 需要包含 DevOps Agent service model 的 boto3
