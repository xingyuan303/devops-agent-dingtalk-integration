# DevOps Agent → 钉钉集成（Stream 双向对话 + 告警通知）

CloudWatch 告警自动触发 AWS DevOps Agent 调查，调查结果推送到钉钉群；同时支持在群里 @Bot 与 DevOps Agent 进行双向 SRE 对话。

## 架构

```
CloudWatch Alarm (state=ALARM) → SNS → Lambda(dingtalk-notifier) → 钉钉告警通知
                                                ↓
                                    Agent Space Webhook → 自动调查
                                                ↓
                              EventBridge → Lambda(investigation-notifier) → 钉钉调查结果

User (钉钉群聊)
      │  @dingtalk-bot <question>
      ▼
DingTalk Stream  wss://... (gateway/connections/open)
      │  CALLBACK event (bot message)
      ▼
dingtalk-bot (ECS/EC2/EKS, long-running)
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

## 组件

| 组件 | 运行方式 | 用途 |
|------|----------|------|
| `dingtalk-bot/` | 长驻进程（ECS/EKS/EC2） | Stream 双向对话 |
| `lambda/dingtalk_notifier/` | Lambda（SNS 触发） | 告警通知 + 触发调查 |
| `lambda/investigation_notifier/` | Lambda（EventBridge 触发） | 调查结果通知 |

## 前置条件

* AWS DevOps Agent Space 已创建并配置数据源
* 钉钉企业内部应用（Stream 模式，见下方配置指南）
* Python 3.12+, Node.js 18+, AWS CDK CLI
* Docker（用于构建 Bot 镜像）

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
    "agent_space_id": "your-agent-space-id"
  }
}
```

### 3. 部署（一键完成 Lambda + ECS Fargate Bot）

```bash
cdk bootstrap  # first time only
cdk deploy
```

CDK 会自动：
- 创建 SNS Topic + 2 个 Lambda（告警通知 + 调查结果通知）
- 构建 `dingtalk-bot/` Docker 镜像并推送到 ECR
- 创建 ECS Fargate 集群 + Service（1 个 Task，自动重启）
- 从 Secrets Manager 注入钉钉凭证到容器环境变量

### 4. 填写 Secrets

```bash
aws secretsmanager put-secret-value \
  --secret-id devops-agent/dingtalk-bot \
  --secret-string '{
    "DINGTALK_APP_KEY": "your-app-key",
    "DINGTALK_APP_SECRET": "your-app-secret",
    "DEVOPS_AGENT_SPACE_ID": "your-agent-space-id"
  }'

aws secretsmanager put-secret-value \
  --secret-id devops-agent/webhook \
  --secret-string '{
    "WEBHOOK_URL": "your-agent-space-webhook-url",
    "WEBHOOK_SECRET": "your-webhook-hmac-secret"
  }'
```

### 5. 接入 CloudWatch Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "your-alarm" \
  --alarm-actions "<SNSTopicArn from cdk deploy output>" \
  ...
```

### 6. 验证

```bash
# 测试告警通知链路
aws cloudwatch set-alarm-state \
  --alarm-name "your-alarm" \
  --state-value ALARM \
  --state-reason "Integration test"

# 测试双向对话
# 在钉钉群里 @Bot 发送任意消息
```

## 钉钉应用配置指南

### 1. 创建钉钉应用

1. 前往 [钉钉开放平台](https://open-dev.dingtalk.com) → 创建企业内部应用
2. 添加「机器人」能力
3. 在「机器人与消息推送」中启用 **Stream 模式**（长连接接收消息）
4. 记录 **App Key** 和 **App Secret**
5. 申请权限：`qyapi_robot_sendmsg`（主动发送消息）
6. 发布应用上线，将机器人添加到目标群聊

### 2. Stream 协议说明

```
Bot POST /v1.0/gateway/connections/open → 获取 WebSocket endpoint + ticket
  → 连接 WebSocket
  → 接收 SYSTEM/CALLBACK/PING 事件
  → ACK CALLBACK 后异步处理
  → 通过 OpenAPI /v1.0/robot/groupMessages/send 发送回复
```

Gateway 订阅配置：`{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}`

> 注意类型是 **CALLBACK** 而非 EVENT。

### 3. 告警通知

告警通知使用同一个钉钉应用的 `access_token` 通过 OpenAPI 发送群消息，不再需要单独的自定义 Webhook 机器人。Lambda 通过 Secrets Manager 获取 App Key/Secret → 换取 access_token → 调用 `/v1.0/robot/groupMessages/send`。

### 4. 故障排查

| 症状 | 原因 | 修复方法 |
|------|------|----------|
| Gateway 返回 `incomplete response` | App Key/Secret 错误或应用未发布 | 检查凭证；确认应用已发布上线 |
| WebSocket 连接后立即断开 | Stream 模式未启用 | 确认已启用 Stream 模式 |
| 群消息发送失败 `status=403` | access_token 过期或权限不足 | 确认已申请 `qyapi_robot_sendmsg` 权限 |
| Bot 收不到消息 | 机器人未加入群聊 | 群设置 → 机器人 → 添加该机器人 |
| WS 断开后迟迟不重连 | DNS 故障或出站阻断 | 确认网络到 `api.dingtalk.com:443` 通畅 |

## 文件结构

```
├── app.py                              CDK 入口
├── stack.py                            CDK Stack 定义
├── cdk.json                            配置文件
├── requirements.txt                    CDK Python 依赖
├── dingtalk-bot/                       双向对话 Bot（长驻进程）
│   ├── app.py                          Stream 客户端 + DevOps Agent 集成
│   ├── Dockerfile
│   └── requirements.txt
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

会自动清理 ECS 集群、Fargate Service、ECR 镜像、Lambda、SNS、EventBridge 规则。

## 注意事项

* `cdk deploy` 会自动构建 Docker 镜像（需要本地 Docker 运行）
* Bot 运行在 ECS Fargate（默认 VPC，公网 IP 用于出站连接）
* Bot 的 IAM Task Role 已自动配置 `aidevops:CreateChat/SendMessage/ListChats`
* Lambda 的 IAM Role 已自动配置 `aidevops:ListJournalRecords/GetBacklogTask`
* Webhook URL 和 Secret 从 Agent Space 控制台获取
* 超过 3500 字节的回复会自动分片发送
