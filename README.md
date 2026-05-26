# DevOps Agent → 钉钉集成（Stream 双向对话 + 告警通知）

CloudWatch 告警自动触发 AWS DevOps Agent 调查，调查结果推送到钉钉群；同时支持在群里 @Bot 与 DevOps Agent 进行**双向 SRE 对话**。

## ✨ 特性

- 🔴 **告警通知**：CloudWatch ALARM/OK 状态变更 → 钉钉 Markdown 卡片 + CloudWatch 监控链接
- 🤖 **自动调查**：ALARM 时自动触发 DevOps Agent 调查，根因分析推送到钉钉群
- 💬 **双向对话**：群里 @Bot 提问，DevOps Agent 实时分析回复
- 🛡 **可靠性**：DLQ 死信队列、Fargate 健康检查、自动重连、LRU 会话淘汰
- 🚀 **一键部署**：`cdk deploy` 自动构建镜像、部署所有 AWS 资源
- 🐳 **零本地依赖**：本地无 Docker？打开 CodeBuild flag 远程构建

## 架构

### 整体架构图

```mermaid
graph TB
    subgraph DingTalk["📱 钉钉"]
        DTUser[用户/群聊]
        DTApp[钉钉应用<br/>Stream 模式]
    end

    subgraph AWS["☁️ AWS Account"]
        subgraph VPC["VPC（CDK 新建或复用）"]
            subgraph Private["私有子网"]
                Bot["🤖 ECS Fargate<br/>dingtalk-bot<br/>(单实例)"]
            end
            NAT["🌐 NAT Gateway"]
        end

        subgraph Lambda["⚡ Lambda"]
            L1["dingtalk-notifier<br/>(SNS 触发)"]
            L2["investigation-notifier<br/>(EventBridge 触发)"]
        end

        subgraph Storage["🔐 凭证 & 队列"]
            SM1["Secrets Manager<br/>dingtalk-bot"]
            SM2["Secrets Manager<br/>webhook"]
            DLQ["SQS DLQ<br/>(14 天保留)"]
        end

        subgraph Events["📬 事件路由"]
            CW["CloudWatch Alarm"]
            SNS["SNS Topic"]
            EB["EventBridge<br/>aws.aidevops"]
        end

        subgraph Agent["🧠 AWS DevOps Agent"]
            AS["Agent Space"]
            WH["Webhook"]
        end
    end

    %% 告警通知链路
    CW -->|ALARM/OK| SNS
    SNS -->|invoke| L1
    L1 -->|HMAC POST| WH
    WH -->|create investigation| AS
    L1 -->|markdown 卡片| DTApp

    %% 调查结果链路
    AS -->|Investigation events| EB
    EB -->|invoke| L2
    L2 -->|ListJournalRecords| AS
    L2 -->|根因摘要| DTApp

    %% 双向对话链路
    DTUser -->|@Bot 提问| DTApp
    DTApp -->|Stream wss<br/>CALLBACK| Bot
    Bot -->|CreateChat<br/>SendMessage| AS
    Bot -->|OpenAPI 回复| DTApp
    DTApp -->|markdown| DTUser

    %% 凭证读取
    L1 -.读取.-> SM1
    L1 -.读取.-> SM2
    L2 -.读取.-> SM1
    Bot -.注入.-> SM1

    %% 失败兜底
    L1 -.失败.-> DLQ
    L2 -.失败.-> DLQ

    %% 出网
    Bot -->|出站流量| NAT
    NAT -->|api.dingtalk.com| DTApp

    classDef aws fill:#FF9900,stroke:#232F3E,color:#fff
    classDef dingtalk fill:#3296FA,stroke:#1A5DAB,color:#fff
    classDef storage fill:#7AA116,stroke:#3F6611,color:#fff
    class Bot,L1,L2,SNS,EB,CW,AS,WH,NAT aws
    class DTUser,DTApp dingtalk
    class SM1,SM2,DLQ storage
```

### 数据流详解

#### 链路 1：CloudWatch 告警 → 钉钉通知 + 自动调查

```
┌─────────────────┐    ┌──────────┐    ┌────────────────────┐    ┌──────────┐
│ CloudWatch      │───▶│ SNS      │───▶│ Lambda             │───▶│ 钉钉群   │
│ Alarm (ALARM)   │    │ Topic    │    │ dingtalk-notifier  │    │ 红色卡片 │
└─────────────────┘    └──────────┘    └─────────┬──────────┘    └──────────┘
                                                 │
                                                 │ HMAC-SHA256 签名
                                                 ▼
                                       ┌────────────────────┐
                                       │ Agent Space        │
                                       │ Webhook            │
                                       └─────────┬──────────┘
                                                 │
                                                 │ 创建 Investigation
                                                 ▼
                                       ┌────────────────────┐
                                       │ DevOps Agent       │
                                       │ 自动调查           │
                                       └────────────────────┘
```

#### 链路 2：DevOps Agent 调查结果 → 钉钉

```
┌──────────────────┐    ┌──────────────┐    ┌────────────────────────┐    ┌──────────┐
│ DevOps Agent     │───▶│ EventBridge  │───▶│ Lambda                 │───▶│ 钉钉群   │
│ Investigation    │    │ aws.aidevops │    │ investigation-notifier │    │ 调查结果 │
│ Created/Done/... │    └──────────────┘    └────────────┬───────────┘    └──────────┘
└──────────────────┘                                     │
                                                         │ ListJournalRecords
                                                         ▼
                                                ┌────────────────┐
                                                │ 提取根因摘要   │
                                                │ 格式化 markdown│
                                                └────────────────┘
```

#### 链路 3：钉钉 @Bot → DevOps Agent 双向对话

```
┌──────────┐                                                        ┌────────────────┐
│ 用户     │                                                        │ DevOps Agent   │
│ @Bot 提问│                                                        │ Agent Space    │
└────┬─────┘                                                        └────────┬───────┘
     │                                                                       ▲
     ▼                                                                       │
┌────────────┐  Stream wss   ┌──────────────────┐  CreateChat/SendMessage   │
│ 钉钉 App   │◀─────────────▶│ ECS Fargate Bot  │───────────────────────────┘
│ Stream 网关│   CALLBACK     │ (单实例)         │
└────────────┘                │                  │
     ▲                        │ 1. 即时 ACK      │
     │                        │ 2. 调用 Agent    │
     │   OpenAPI 回复          │ 3. EventStream  │
     └────────────────────────│    解析 + 拆分   │
        markdown              │ 4. 多群广播      │
                              └──────────────────┘
                                       │
                                       │ 通过 NAT Gateway 出网
                                       ▼
                              ┌──────────────────┐
                              │ api.dingtalk.com │
                              └──────────────────┘
```

### 部署拓扑

```
┌──────────────────────────────────────────────────────────────────┐
│                      AWS Account / Region                         │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      VPC (CDK 创建/复用)                    │ │
│  │                                                              │ │
│  │  ┌─────────────────┐         ┌─────────────────┐           │ │
│  │  │  Public Subnet  │         │ Public Subnet   │           │ │
│  │  │  ┌─────────┐    │         │  ┌─────────┐    │           │ │
│  │  │  │  IGW    │    │         │  │  NAT    │    │           │ │
│  │  │  └─────────┘    │         │  │ Gateway │    │           │ │
│  │  │                 │         │  └─────────┘    │           │ │
│  │  └─────────────────┘         └─────────────────┘           │ │
│  │           │                          │                       │ │
│  │  ┌────────┴─────────┐        ┌──────┴──────────┐           │ │
│  │  │ Private Subnet   │        │ Private Subnet  │           │ │
│  │  │                  │        │                 │           │ │
│  │  │  ┌────────────┐  │        │                 │           │ │
│  │  │  │ ECS Task   │  │        │                 │           │ │
│  │  │  │ Bot (1 个) │  │        │                 │           │ │
│  │  │  └────────────┘  │        │                 │           │ │
│  │  │       AZ-1       │        │      AZ-2       │           │ │
│  │  └──────────────────┘        └─────────────────┘           │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────────┐ │
│  │  SNS     │  │EventBridge│  │ Lambda   │  │ Secrets Manager │ │
│  │  Topic   │  │   Rule    │  │ x 2      │  │   x 2           │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────────────┘ │
│                                                                   │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐             │
│  │  SQS DLQ │  │CloudWatch Logs│  │ ECR Repository │             │
│  └──────────┘  └──────────────┘  └────────────────┘             │
└──────────────────────────────────────────────────────────────────┘
```

## 组件

| 组件 | 运行方式 | 用途 |
|------|----------|------|
| `lambda/dingtalk_notifier/` | Lambda（SNS 触发） | 告警通知 + 触发调查 |
| `lambda/investigation_notifier/` | Lambda（EventBridge 触发） | 调查结果通知 |
| `dingtalk-bot/` | ECS Fargate（长驻进程） | Stream 双向对话 |

## 前置条件

- ✅ AWS DevOps Agent Space 已创建并配置数据源
- ✅ 钉钉企业内部应用（Stream 模式启用，[配置指南](#钉钉应用配置指南)）
- ✅ Python 3.12+, Node.js 18+, AWS CDK CLI v2
- ⚪ Docker（可选；不装则用 CodeBuild 远程构建）

## 快速部署

### 1. 安装依赖

```bash
pip install -r requirements.txt
npm install -g aws-cdk  # 如未安装
```

### 2. 配置

编辑 `cdk.json`：

```json
{
  "app": "python3 app.py",
  "context": {
    "agent_space_id": "your-agent-space-id",
    "dingtalk_chat_id": "your-open-conversation-id",
    "resource_prefix": "devops-agent",
    "vpc_id": "",
    "destroy_secrets": false
  }
}
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `agent_space_id` | ✅ | AWS DevOps Agent Space ID |
| `dingtalk_chat_id` | ✅ | 钉钉群的 `openConversationId`（支持逗号分隔多群广播） |
| `resource_prefix` | ❌ | 资源名前缀，默认 `devops-agent`。多套部署到同一账号时改为不同值避免冲突 |
| `vpc_id` | ❌ | 现有 VPC ID。**留空则新建 VPC + NAT Gateway**（Bot 走 NAT 出网，无公网 IP；约 $32-65/月） |
| `destroy_secrets` | ❌ | 默认 `false`（保护凭证）。设为 `true` 时 `cdk destroy` 会删除 Secrets Manager 中的凭证 |

### 3. 预构建 Lambda 依赖 ⚠️ 必需

DevOps Agent 是新服务，标准 boto3 还不识别它。需要预先把自定义 service model 和指定版本的 boto3 打包：

```bash
bash prebuild.sh
```

这会在 `lambda/investigation_notifier/.bundled/` 中创建：
- `boto3==1.43.9` + `botocore==1.43.9`（已知支持 devops-agent service stub 的版本）
- `botocore/data/devops-agent/2026-01-01/`（DevOps Agent service model）
- `investigation_notifier.py` + `dingtalk_utils.py`

> **何时重跑：** 修改了 `investigation_notifier.py` 或 `dingtalk_utils.py` 后重新执行 `bash prebuild.sh`。
> **Bot 镜像无需手动 prebuild**：Dockerfile 中已 `pip install boto3==1.43.9` 并 `COPY botocore-ext/devops-agent` 注入。

### 4. 部署

**方式 A：本地 Docker 构建**
```bash
cdk bootstrap   # 仅首次
cdk deploy
```

**方式 B：CodeBuild 远程构建（无需本地 Docker）**

在 `cdk.json` context 中加：
```json
"@aws-cdk/aws-ecr-assets:buildWithCodeBuild": true
```
然后正常 `cdk deploy`，Bot 镜像会在 AWS CodeBuild 中构建并推送到 ECR。

CDK 会自动创建：
- SNS Topic + 2 个 Lambda + DLQ
- EventBridge 规则订阅 `aws.aidevops` 事件
- ECS Fargate 集群 + Service（单 Task + 健康检查 + 自动重启）
- VPC（如未指定 `vpc_id`）+ NAT Gateway
- Secrets Manager 凭证仓库

### 5. 填写 Secrets

```bash
# 钉钉应用凭证（同时供 Bot 和 Lambda 使用）
aws secretsmanager put-secret-value \
  --secret-id devops-agent/dingtalk-bot \
  --secret-string '{
    "DINGTALK_APP_KEY": "your-app-key",
    "DINGTALK_APP_SECRET": "your-app-secret"
  }'

# Agent Space Webhook（用于触发自动调查）
aws secretsmanager put-secret-value \
  --secret-id devops-agent/webhook \
  --secret-string '{
    "WEBHOOK_URL": "your-agent-space-webhook-url",
    "WEBHOOK_SECRET": "your-webhook-hmac-secret"
  }'
```

> Webhook URL 和 Secret 从 AWS Console → DevOps Agent → Agent Space → Webhook 配置中获取。

### 6. 接入 CloudWatch Alarm

将 SNS Topic ARN 设为 Alarm 的 AlarmActions（CDK 输出的 `SNSTopicArn`）：

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name your-alarm \
  --alarm-actions <SNSTopicArn> \
  --ok-actions <SNSTopicArn> \
  ...
```

### 7. 验证

```bash
# 测试告警通知链路
aws cloudwatch set-alarm-state \
  --alarm-name your-alarm \
  --state-value ALARM \
  --state-reason "Integration test"

# 测试双向对话：在钉钉群里 @Bot 发送 "/help"
```

## Bot 命令

| 命令 | 说明 |
|------|------|
| `@Bot <问题>` | 调用 DevOps Agent 进行分析 |
| `/help` 或 `帮助` | 查看使用说明 |
| `/reset` 或 `重置` | 重置当前群聊的对话上下文 |

## 钉钉应用配置指南

### 1. 创建应用

1. 前往 [钉钉开放平台](https://open-dev.dingtalk.com) → 创建企业内部应用
2. 添加「机器人」能力
3. 「机器人与消息推送」→ 启用 **Stream 模式**
4. 记录 **App Key** 和 **App Secret**
5. 申请权限：`qyapi_robot_sendmsg`
6. 发布应用上线，将机器人添加到目标群聊
7. 获取群的 `openConversationId`：群设置 → 群机器人 → 该机器人 → 群 ID

### 2. Stream 协议

```
Bot POST /v1.0/gateway/connections/open → 获取 WebSocket endpoint + ticket
  → 连接 wss://...
  → 接收 SYSTEM / CALLBACK / PING 事件
  → CALLBACK 立即 ACK，异步调用 DevOps Agent
  → 通过 OpenAPI /v1.0/robot/groupMessages/send 回复
```

Gateway 订阅：`{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}`

> 类型是 **CALLBACK** 而非 EVENT。

### 3. 告警通知机制

告警通知和双向对话**共用同一个钉钉应用**：
- Lambda 通过 Secrets Manager 拿 App Key/Secret → 换取 `access_token` → 调用 `/v1.0/robot/groupMessages/send`
- 不再需要钉钉自定义 Webhook 机器人（加签那种）

## 可靠性设计

| 机制 | 实现 |
|------|------|
| **死信队列（DLQ）** | Lambda 失败超过重试次数后消息进入 SQS DLQ，保留 14 天 |
| **重试** | EventBridge → Lambda 重试 2 次；Lambda async retry 内置 |
| **健康检查** | Fargate 容器 `pgrep` 检查 Bot 进程，失败 3 次自动重启 |
| **优雅退出** | Bot 捕获 SIGTERM → WebSocket 关闭 → 干净退出（避免 Fargate 强制 kill） |
| **断线重连** | WebSocket 断开后指数退避（最多 60s）自动重连 |
| **会话隔离** | LRU 缓存（200 条），超出自动淘汰最旧；调用失败时重置当前会话 |
| **多群广播** | `dingtalk_chat_id` 支持逗号分隔，单条消息发到多个群 |
| **去重** | EventBridge 投递的事件按 `event.id` 在进程内去重（at-most-once 处理） |

## 网络与安全

- **Bot 网络**：默认创建私有子网 + NAT Gateway，Bot Task 不暴露公网 IP，所有出站流量经 NAT
- **凭证管理**：所有密钥存 Secrets Manager，CDK 自动配置 Lambda/ECS 读取权限
- **IAM 最小权限**：
  - Lambda：`aidevops:ListJournalRecords/GetBacklogTask` + Secrets 只读
  - Bot Task：`aidevops:CreateChat/SendMessage/ListChats` + Secrets 只读
- **Secret 保护**：默认 `cdk destroy` 不删除 Secret（保留 7 天后才能彻底清除），需要清理时设 `destroy_secrets=true`

## 文件结构

```
.
├── app.py                              CDK 入口
├── stack.py                            CDK Stack 定义
├── cdk.json                            CDK 配置 + context 参数
├── requirements.txt                    CDK Python 依赖
├── README.md
├── dingtalk-bot/                       Stream Bot（ECS Fargate 长驻进程）
│   ├── app.py                          Stream 客户端 + DevOps Agent 集成
│   ├── Dockerfile                      Python 3.12-slim + procps
│   ├── requirements.txt                websockets + boto3
│   └── .dockerignore
└── lambda/
    ├── dingtalk_notifier/              告警 → 钉钉 + 触发调查
    │   ├── dingtalk_notifier.py
    │   └── dingtalk_utils.py           共享工具（access_token / 消息发送）
    └── investigation_notifier/         调查结果 → 钉钉
        ├── investigation_notifier.py
        └── dingtalk_utils.py           （同上，避开 Lambda Layer 路径问题）
```

## 故障排查

### Bot

| 症状 | 原因 | 修复方法 |
|------|------|----------|
| Gateway 返回 `incomplete response` | App Key/Secret 错误或应用未发布 | 检查 Secrets Manager 凭证；确认钉钉应用已发布上线 |
| WebSocket 连接后立即断开 | Stream 模式未启用 | 钉钉「机器人与消息推送」→ 启用 Stream 模式 |
| Bot 收不到消息 | 机器人未加入群聊 | 群设置 → 群机器人 → 添加该机器人 |
| 群消息发送失败 `403` | `access_token` 过期或权限不足 | 确认已申请 `qyapi_robot_sendmsg` 权限 |
| ECS Task 反复重启 | 健康检查失败 | 检查 CloudWatch Logs；确认 Bot 进程能正常启动 |
| WS 断开后迟迟不重连 | DNS 故障或出站阻断 | 确认 NAT Gateway 到 `api.dingtalk.com:443` 通畅 |

### Lambda

| 症状 | 排查方向 |
|------|----------|
| 告警没发到钉钉 | 检查 Lambda CloudWatch Logs；查看 DLQ 是否有失败消息 |
| 调查没触发 | 确认 `devops-agent/webhook` Secret 已正确填写 |
| Lambda 超时 | 多群广播太慢？检查 `DINGTALK_CHAT_ID` 群数量 |

### 查看日志

```bash
# Bot 日志（实时）
aws logs tail /aws/ecs/devops-agent-bot-cluster --follow

# Lambda 日志
aws logs tail /aws/lambda/devops-agent-dingtalk-notifier --follow
aws logs tail /aws/lambda/devops-agent-investigation-notifier --follow

# DLQ 检查
aws sqs receive-message --queue-url <DLQUrl from cdk output>
```

## 卸载

```bash
cdk destroy
```

默认会保留 Secrets Manager 中的凭证（7 天后才能彻底删除）。如需立即清理：

```bash
cdk destroy -c destroy_secrets=true
```

## 成本估算（us-east-1）

| 资源 | 月度估算 |
|------|----------|
| Fargate Task（0.25 vCPU + 512MB，常驻） | ~$10 |
| NAT Gateway（如 CDK 新建 VPC） | ~$32 + 流量费 |
| Lambda（低频调用） | <$1 |
| SNS / EventBridge / SQS | <$1 |
| Secrets Manager（2 个 Secret） | $0.80 |
| CloudWatch Logs | <$1 |
| **合计** | **约 $45-50/月** |

复用现有 VPC（`-c vpc_id=vpc-xxx`）可省去 NAT Gateway 费用，约 $13/月。

## License

MIT
