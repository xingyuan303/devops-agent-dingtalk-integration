// =============================================================================
// Core Interfaces & Types for CloudWatch Alarm Auto RCA (DingTalk)
// =============================================================================

// -----------------------------------------------------------------------------
// CloudWatch Alarm Event Types
// -----------------------------------------------------------------------------

/** CloudWatch Alarm State Change event detail structure. */
export interface CloudWatchAlarmDetail {
  alarmName: string;
  state: {
    value: string;
    reason: string;
    reasonData?: string;
    timestamp: string;
    actionsSuppressedBy?: string;
  };
  previousState: {
    value: string;
    reason: string;
    reasonData?: string;
    timestamp: string;
  };
  configuration: {
    description?: string;
    metrics?: Array<{
      id: string;
      metricStat?: {
        metric: {
          namespace: string;
          name: string;
          dimensions: Record<string, string>;
        };
        period: number;
        stat: string;
      };
      expression?: string;
      returnData: boolean;
    }>;
  };
}

// -----------------------------------------------------------------------------
// AlarmRouter Interfaces
// -----------------------------------------------------------------------------

/** Input: EventBridge CloudWatch Alarm State Change event. */
export interface AlarmRouterInput {
  version: string;
  id: string;
  'detail-type': 'CloudWatch Alarm State Change';
  source: 'aws.cloudwatch';
  account: string;
  time: string;
  region: string;
  resources: string[];
  detail: CloudWatchAlarmDetail;
}

/** Output: Structured alarm information with filtering status. */
export interface AlarmRouterOutput {
  alarmId: string;
  alarmName: string;
  namespace: string;
  metricName: string;
  dimensions: Record<string, string>;
  threshold: number;
  currentValue: number;
  stateChangeTimestamp: string;
  previousState: string;
  accountId: string;
  region: string;
  resourceArn: string;
  filtered: boolean;
  filterReason?: string;
}

// -----------------------------------------------------------------------------
// AlarmGrouper Interfaces
// -----------------------------------------------------------------------------

export interface AlarmGrouperInput {
  alarm: AlarmRouterOutput;
}

export interface AlarmGrouperOutput {
  groupId: string;
  alarms: AlarmRouterOutput[];
  isNewGroup: boolean;
  shouldWait: boolean;
  waitUntil?: string;
}

// -----------------------------------------------------------------------------
// RCAAnalyzer Interfaces
// -----------------------------------------------------------------------------

export interface RCAAnalyzerInput {
  groupId: string;
  alarms: AlarmRouterOutput[];
}

export interface RCAAnalyzerOutput {
  rcaReport: RCAReport;
  status: 'completed' | 'partial' | 'failed';
  duration: number;
}

// -----------------------------------------------------------------------------
// RCA Report Model
// -----------------------------------------------------------------------------

export interface RCAReport {
  reportId: string;
  groupId: string;
  generatedAt: string;
  status: 'completed' | 'partial' | 'timeout';

  alarmSummary: {
    alarmCount: number;
    alarms: Array<{
      alarmName: string;
      namespace: string;
      metricName: string;
      currentValue: number;
      threshold: number;
      resource: string;
    }>;
    firstAlarmTime: string;
    lastAlarmTime: string;
  };

  investigation: {
    timeline: Array<{
      timestamp: string;
      action: string;
      finding: string;
    }>;
    dataSourcesConsulted: string[];
    hypothesesExplored: string[];
  };

  rootCause: {
    summary: string;
    category:
      | 'system_change'
      | 'input_anomaly'
      | 'resource_limit'
      | 'component_failure'
      | 'dependency_issue'
      | 'unknown';
    details: string;
    confidence: 'high' | 'medium' | 'low';
    affectedResources: string[];
  };

  remediation: {
    immediateMitigation: string;
    longTermFix: string;
    steps: string[];
    rollbackPlan?: string;
  };

  /** 高层业务/用户影响描述（来自 DevOps Agent 调查输出的 Impact 模块）。 */
  impact?: string;

  /** 调查过程中按时间顺序整理的关键发现（指标变化、日志异常、配置变更等）。 */
  keyFindings?: string[];

  /** 调查过的假设列表。 */
  hypothesesDetailed?: Array<{
    hypothesis: string;
    supported: boolean;
    reasoning: string;
  }>;

  /** 全部识别出的根因。当数组非空时，UI 应使用本字段渲染多根因；否则回退到 rootCause 单字段。 */
  rootCauses?: Array<{
    summary: string;
    details: string;
    evidence?: string;
  }>;

  /** 完整的修复计划（步骤、命令、回滚方案）。 */
  mitigationPlan?: Array<{
    step: string;
    command?: string;
    rollback?: string;
  }>;

  /** DevOps Agent investigation execution ID（用于 console deep link）。 */
  executionId?: string;
  /** DevOps Agent investigation task ID。 */
  taskId?: string;
  /** 触发本次调查时由我们生成的 incidentId。 */
  incidentId?: string;
  /**
   * 报告阶段标记：
   * - 'investigation'（默认）：第一条卡片，包含 root cause + investigation timeline
   * - 'mitigation'：第二条卡片，仅包含 mitigation plan 内容
   */
  reportPhase?: 'investigation' | 'mitigation';
  /** DevOps Agent 调查的原始 markdown 输出（fallback）。 */
  agentRawText?: string;
}

// -----------------------------------------------------------------------------
// DingTalk Message Types
// -----------------------------------------------------------------------------

/** DingTalk text message. */
export interface DingTalkTextMessage {
  msgtype: 'text';
  text: { content: string };
  at?: { atMobiles?: string[]; isAtAll?: boolean };
}

/** DingTalk markdown message. */
export interface DingTalkMarkdownMessage {
  msgtype: 'markdown';
  markdown: {
    title: string;
    text: string;
  };
  at?: { atMobiles?: string[]; isAtAll?: boolean };
}

/**
 * DingTalk ActionCard message — supports buttons.
 *
 * 两种形式：
 *   1. singleTitle + singleURL（整张卡片单按钮）
 *   2. btns 数组（多按钮）
 *
 * btnOrientation: '0' = 按钮竖直排列（默认），'1' = 横向排列
 */
export interface DingTalkActionCardMessage {
  msgtype: 'actionCard';
  actionCard: {
    title: string;
    text: string;
    btnOrientation?: '0' | '1';
    singleTitle?: string;
    singleURL?: string;
    btns?: Array<{
      title: string;
      actionURL: string;
    }>;
  };
}

export type DingTalkMessage =
  | DingTalkTextMessage
  | DingTalkMarkdownMessage
  | DingTalkActionCardMessage;

// -----------------------------------------------------------------------------
// DingTalkNotifier Interfaces
// -----------------------------------------------------------------------------

/**
 * Webhook 凭据：URL + 加签密钥（钉钉自定义机器人安全设置开启「加签」时必填）。
 */
export interface DingTalkWebhookCredential {
  url: string;
  /** HMAC-SHA256 加签密钥，钉钉创建机器人时配置 */
  secret?: string;
}

export interface DingTalkNotifierInput {
  rcaReport: RCAReport;
  /** 多组 webhook 凭据，notifier 会按路由规则筛选后批量发送 */
  webhookCredentials: DingTalkWebhookCredential[];
  notificationType: 'rca_complete' | 'rca_timeout' | 'rca_partial';
}

export interface DingTalkNotifierOutput {
  success: boolean;
  sentTo: string[];
  failedTo: string[];
  retryCount: number;
}

// -----------------------------------------------------------------------------
// Data Models (DynamoDB)
// -----------------------------------------------------------------------------

export interface WorkflowExecution {
  executionId: string;
  createdAt: string;
  status:
    | 'pending'
    | 'analyzing'
    | 'completed'
    | 'failed'
    | 'timed_out'
    | 'notified'
    | 'investigation_completed'
    | 'mitigation_completed'
    | 'mitigation_failed';
  groupId: string;
  alarmArns: string[];
  resourceArns: string[];
  startedAt: string;
  completedAt?: string;
  rcaReportId?: string;
  notificationStatus?: 'sent' | 'partial' | 'failed';

  // ── webhook-driven flow extension ───────────────────────────────────────
  /** SFN .waitForTaskToken 注入的 token（phase-1 SendTaskSuccess 用） */
  taskToken?: string;
  /** 来自 EventBridge 'Investigation Created' 事件的 task_id；phase-2 用它精确反查 */
  taskId?: string;
  /** 完整的 AlarmRouterOutput 数组，event handler 重新合成 RCAReport 时用 */
  alarms?: AlarmRouterOutput[];

  stateTransitions: Array<{
    from: string;
    to: string;
    timestamp: string;
    reason?: string;
  }>;
  ttl: number;
}

export interface AlarmGroup {
  resourceArn: string;
  groupId: string;
  alarms: AlarmRouterOutput[];
  windowStart: string;
  windowEnd: string;
  status: 'collecting' | 'processing' | 'done';
  ttl: number;
}

// -----------------------------------------------------------------------------
// Configuration Models (SSM Parameter Store)
// -----------------------------------------------------------------------------

export interface AlarmFilterRule {
  type: 'namespace' | 'name_pattern' | 'tag';
  value: string;
  action: 'include' | 'exclude';
}

export interface WebhookRoutingRule {
  field: 'namespace' | 'tag';
  pattern: string;
  match: 'equals' | 'contains' | 'regex';
}

/**
 * DingTalk webhook config with routing rules.
 *
 * 与飞书的 WebhookConfig 不同点：钉钉自定义机器人启用「加签」安全设置时，
 * 每个 webhook 还需要带一个独立的 secret。
 */
export interface DingTalkWebhookConfig {
  url: string;
  /** HMAC-SHA256 加签密钥；如机器人没启用加签则留空 */
  secret?: string;
  name: string;
  routingRules: WebhookRoutingRule[];
}

export interface RetryPolicy {
  maxRetries: number;
  initialDelay: number;
  backoffMultiplier: number;
}

export interface SystemConfig {
  version: string;
  alarmSelectionMode: 'all' | 'custom';
  selectedAlarmNames: string[];
  alarmFilters: AlarmFilterRule[];
  /** 钉钉 webhook 路由配置（替换原 feishuWebhooks 字段） */
  dingtalkWebhooks: DingTalkWebhookConfig[];
  rcaTimeout: number;
  retryPolicy: RetryPolicy;
  groupingWindow: number;
  enabledNamespaces: string[];
  retentionDays: number;
}
