# CloudWatch Alarm Auto RCA — DingTalk

> 🚧 **重构中**：本项目正在迁移到全新架构（TypeScript + CDK + Step Functions + DynamoDB），参考自 [aws-devops-agent](https://github.com/xitingy1123/aws-devops-agent) 的飞书版本。
>
> **旧版（Python + ECS Fargate Stream Bot）**：见 `python-stream-impl` 分支。

基于 AWS DevOps Agent 的 CloudWatch 告警自动根因分析系统。当 CloudWatch 告警触发时，系统自动调用 DevOps Agent 进行根因调查，生成结构化 RCA 报告，通过钉钉自定义机器人推送给团队。同时提供钉钉企业应用 Bot，支持在群里 @ 机器人与 DevOps Agent 对话以及触发改善计划。

## 重构进度

| 批次 | 内容 | 状态 |
|------|------|------|
| 1 | TypeScript + CDK 项目骨架 | ✅ |
| 2 | shared 模块（types/config/dynamodb/workflow） | ⏳ |
| 3 | alarm-router + alarm-grouper | ⏳ |
| 4 | rca-analyzer + investigation-event-handler | ⏳ |
| 5 | dingtalk-notifier（替换 feishu-notifier） | ⏳ |
| 6 | dingtalk-bot（替换 feishu-bot，API Gateway 模式） | ⏳ |

## 架构（目标）

```
告警链路:
  CloudWatch Alarm ──→ EventBridge ──→ Step Functions ──→ Lambda 链
                                              │
                                              └─→ DevOps Agent webhook
                                              ⋮ 异步执行 ⋮
                              EventBridge (aws.aidevops) ──→ Lambda
                                              │
                                              ├─ phase 1: 根因卡片 → 钉钉
                                              └─ phase 2: 修复计划卡片 → 钉钉

Bot 链路:
  用户 @Bot ──→ 钉钉云端 ──→ POST API Gateway ──→ Lambda ──→ DevOps Agent
                                                      │
                                                      └─→ 钉钉 OpenAPI 回复
```

## 钉钉 vs 飞书的实现差异

| 维度 | 飞书 | 钉钉 |
|------|------|------|
| 告警接收方 | 自定义机器人 Webhook | 自定义机器人 Webhook（HMAC 加签） |
| Bot 事件接收 | 企业自建应用 → 事件回调 | 企业自建应用 → **事件订阅 HTTP 模式** |
| 签名验证 | Verification Token + Encrypt Key | `timestamp + sign` HMAC-SHA256 头 |
| 卡片格式 | `interactive` lark_md | `actionCard` markdown |
| 按钮交互 | `card.action.trigger` 事件 | `chatbot_action` 事件回调 |

## License

ISC
