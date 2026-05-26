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

## 注意事项

* 钉钉自定义机器人需要配置「加签」安全设置，Secret 以 `SEC` 开头
* Webhook URL 和 Secret 从 Agent Space 控制台获取
* `investigation-notifier` 需要包含 DevOps Agent service model 的 boto3
