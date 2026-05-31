/**
 * AWS DevOps Agent webhook client.
 *
 * 替代旧的 CreateChat + SendMessage 方案：
 *   - 旧方案：直接打开 chat session 让 agent 流式返回 RCA markdown，
 *     但 chat session 的 executionId 不属于 investigation 命名空间，
 *     无法用于拼 DevOps Agent 控制台 /home/activity/{id} 这个 deep link
 *     （拼上去会 404）。
 *   - 新方案：调用 DevOps Agent 控制台 Capabilities 里配置的 Generic Webhook
 *     触发一次真正的 investigation。webhook 走 HMAC-SHA256 鉴权。
 *     返回的 200 不带 task_id；后续由 EventBridge 上 'aws.aidevops' 源的
 *     'Investigation Created' / 'Investigation Completed' 事件携带
 *     execution_id / task_id，再由 InvestigationEventHandler Lambda 通过
 *     SFN SendTaskSuccess 唤醒挂起的 Step Function，把 RCAReport 透传给
 *     DingTalkNotifier。
 *
 * Reference: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-capabilities-for-aws-devops-agent-invoking-devops-agent-through-webhook.html
 */

import * as crypto from 'crypto';
import { request as httpsRequest } from 'https';
import { request as httpRequest } from 'http';
import { URL } from 'url';
import {
  SecretsManagerClient,
  GetSecretValueCommand,
} from '@aws-sdk/client-secrets-manager';
import { DevOpsAgentRequest } from './context-builder';

// -----------------------------------------------------------------------------
// Public types
// -----------------------------------------------------------------------------

export interface AgentClientOptions {
  /** 单次 HTTP 调用的最大重试次数（包含首次）。默认 3。 */
  maxRetries?: number;
  /** 首次失败后的退避基数（毫秒）。默认 5000。 */
  initialDelayMs?: number;
  /** 退避倍率。默认 2。 */
  backoffMultiplier?: number;
  /** 单次 HTTP 调用的超时（毫秒）。默认 15000。 */
  timeoutMs?: number;
}

/**
 * Webhook 触发结果。注意：此处的 success=true 仅表示 webhook 成功提交
 * （DevOps Agent 已接受并排队 investigation），并不代表 investigation 已经
 * 完成。Investigation 的真实结果会通过 EventBridge 异步回流。
 */
export interface AgentTriggerResponse {
  success: boolean;
  /** 我们生成并发给 webhook 的 incidentId（去重 + correlation）。 */
  incidentId?: string;
  /** ISO 时间戳，用于事件 correlation 时的窗口判断。 */
  triggeredAt?: string;
  error?: string;
  timedOut?: boolean;
  /** HTTP 状态码，便于排查。 */
  statusCode?: number;
}

// -----------------------------------------------------------------------------
// Defaults & module-level state
// -----------------------------------------------------------------------------

const DEFAULT_OPTIONS: Required<AgentClientOptions> = {
  maxRetries: 3,
  initialDelayMs: 5000,
  backoffMultiplier: 2,
  timeoutMs: 15000,
};

const AWS_REGION_NAME = process.env.AWS_REGION ?? 'us-east-1';

let secretsClient: SecretsManagerClient = new SecretsManagerClient({ region: AWS_REGION_NAME });

/** 进程内缓存（Lambda 容器复用时避免每次 invoke 都拉 Secrets Manager）。 */
let cachedCreds: { url: string; secret: string } | undefined;

/**
 * 测试钩子：注入 mock SecretsManagerClient。
 */
export function setSecretsManagerClient(client: SecretsManagerClient): void {
  secretsClient = client;
  cachedCreds = undefined;
}

/**
 * 测试钩子：清空凭据缓存。
 */
export function resetCredentialCache(): void {
  cachedCreds = undefined;
}

// -----------------------------------------------------------------------------
// Backoff helper (kept for backwards-compatible exports + tests)
// -----------------------------------------------------------------------------

/**
 * 指数退避：initialDelayMs × backoffMultiplier^(attempt - 1)。
 */
export function calculateBackoffDelay(
  attempt: number,
  initialDelayMs: number,
  multiplier: number
): number {
  return initialDelayMs * Math.pow(multiplier, attempt - 1);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// -----------------------------------------------------------------------------
// Credentials loading
// -----------------------------------------------------------------------------

/**
 * 从 Secrets Manager 读 webhook 凭据。Secret 内容期望是 JSON：
 *   { "url": "https://event-ai...", "secret": "<HMAC-secret>" }
 * 也兼容 SecretString 直接是 URL+secret 的简单 JSON。
 */
export async function loadWebhookCredentials(): Promise<{ url: string; secret: string }> {
  if (cachedCreds) return cachedCreds;

  const secretId = process.env.DEVOPS_AGENT_WEBHOOK_SECRET_ID;
  if (!secretId) {
    throw new Error('DEVOPS_AGENT_WEBHOOK_SECRET_ID environment variable is not configured');
  }

  const resp = await secretsClient.send(new GetSecretValueCommand({ SecretId: secretId }));
  const raw = resp.SecretString;
  if (!raw) {
    throw new Error(`Secret ${secretId} has no SecretString`);
  }

  let parsed: any;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Secret ${secretId} is not valid JSON`);
  }

  if (typeof parsed.url !== 'string' || typeof parsed.secret !== 'string') {
    throw new Error(`Secret ${secretId} must contain { "url": string, "secret": string }`);
  }

  cachedCreds = { url: parsed.url, secret: parsed.secret };
  return cachedCreds;
}

// -----------------------------------------------------------------------------
// HMAC signing
// -----------------------------------------------------------------------------

/**
 * 按 DevOps Agent webhook v1 的规则计算 HMAC-SHA256 签名。
 * 签名输入：`${timestamp}:${payload}`，secret 作为 HMAC key，
 * 输出 base64 编码。客户端把 timestamp 放 `x-amzn-event-timestamp`，
 * 签名放 `x-amzn-event-signature`。
 *
 * Reference: webhook docs "Version 1 (HMAC authentication)"。
 */
export function computeHmacSignature(
  payload: string,
  timestamp: string,
  secret: string
): string {
  const hmac = crypto.createHmac('sha256', secret);
  hmac.update(`${timestamp}:${payload}`, 'utf8');
  return hmac.digest('base64');
}

// -----------------------------------------------------------------------------
// Payload builder
// -----------------------------------------------------------------------------

/**
 * 把 buildRCAContext 出来的 DevOpsAgentRequest 转成 webhook payload。
 *
 * incidentId 必须是稳定且唯一的字符串。我们用
 *   `cw-alarm-${groupId}-${ms}` 形式，其中 groupId 是 SFN 透传过来的
 *   alarm group id，ms 是 wall-clock 时间戳。这样：
 *     - 不同告警组之间唯一（groupId 是 UUID）
 *     - 同一组内重试时 ms 会变 → 不会被 DevOps Agent 当成重复事件丢弃
 */
export interface WebhookPayload {
  eventType: 'incident';
  incidentId: string;
  action: 'created';
  priority: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'MINIMAL';
  title: string;
  description: string;
  timestamp: string;
  service: string;
  data: {
    metadata: {
      groupId: string;
      region: string;
      alarmArns: string[];
      resourceArns: string[];
      timeRange: { start: string; end: string };
    };
  };
}

export function buildWebhookPayload(
  request: DevOpsAgentRequest,
  groupId: string,
  triggeredAt: string
): WebhookPayload {
  const { context } = request;
  const incidentId = `cw-alarm-${groupId}-${Date.parse(triggeredAt)}`;

  // Title: 用第一条告警 ARN 末尾的 alarm name 作摘要
  const firstArn = context.alarmArns[0] ?? '';
  const alarmName = firstArn.split(':alarm:')[1] ?? firstArn ?? 'CloudWatch Alarm';
  const titlePrefix =
    context.alarmArns.length > 1
      ? `[${context.alarmArns.length} alarms] `
      : '';
  const title = `${titlePrefix}${alarmName}`;

  return {
    eventType: 'incident',
    incidentId,
    action: 'created',
    priority: 'HIGH',
    title: title.substring(0, 256),
    description: context.additionalContext.substring(0, 4000),
    timestamp: triggeredAt,
    service: 'CloudWatchAlarmAutoRCA',
    data: {
      metadata: {
        groupId,
        region: AWS_REGION_NAME,
        alarmArns: context.alarmArns,
        resourceArns: context.resourceArns,
        timeRange: context.timeRange,
      },
    },
  };
}

// -----------------------------------------------------------------------------
// HTTP POST
// -----------------------------------------------------------------------------

interface HttpResult {
  statusCode: number;
  body: string;
}

/**
 * 发起 HTTP POST 请求,带 timeout。允许测试通过 setHttpTransport 注入 mock。
 */
export type HttpTransport = (
  url: URL,
  body: string,
  headers: Record<string, string>,
  timeoutMs: number
) => Promise<HttpResult>;

const defaultHttpTransport: HttpTransport = (url, body, headers, timeoutMs) =>
  new Promise<HttpResult>((resolve, reject) => {
    const isHttps = url.protocol === 'https:';
    const reqFn = isHttps ? httpsRequest : httpRequest;
    const requestOptions = {
      hostname: url.hostname,
      port: url.port || (isHttps ? 443 : 80),
      path: url.pathname + url.search,
      method: 'POST',
      headers: {
        ...headers,
        'Content-Length': Buffer.byteLength(body).toString(),
      },
      timeout: timeoutMs,
    };

    const req = reqFn(requestOptions, (res) => {
      let chunks = '';
      res.on('data', (c) => {
        chunks += c;
      });
      res.on('end', () => {
        resolve({ statusCode: res.statusCode ?? 0, body: chunks });
      });
    });

    req.on('timeout', () => {
      req.destroy(new Error('TIMEOUT'));
    });
    req.on('error', (err) => {
      reject(err);
    });
    req.write(body);
    req.end();
  });

let httpTransport: HttpTransport = defaultHttpTransport;

/** 测试钩子：注入 mock HTTP 客户端。 */
export function setHttpTransport(transport: HttpTransport): void {
  httpTransport = transport;
}

/** 测试钩子：还原默认 HTTP 客户端。 */
export function resetHttpTransport(): void {
  httpTransport = defaultHttpTransport;
}

// -----------------------------------------------------------------------------
// Public API
// -----------------------------------------------------------------------------

/**
 * 触发一次 DevOps Agent investigation。
 *
 * 行为：
 *   1. 从 Secrets Manager 读凭据（结果缓存到容器生命周期）
 *   2. 构造 payload 与 HMAC 签名
 *   3. POST 到 webhook URL；2xx 视为成功
 *   4. 失败时按 maxRetries / initialDelayMs / backoffMultiplier 退避重试
 *
 * 返回值里的 success=true **不代表 investigation 完成**，仅表示 webhook 已接收。
 * 调用方（Lambda）拿到 { success, incidentId, triggeredAt } 后应把这些字段
 * 传回 SFN，由 SFN 的 .waitForTaskToken 模式挂起，等 EventBridge 事件触发的
 * InvestigationEventHandler Lambda 再 SendTaskSuccess 唤醒后续步骤。
 */
export async function triggerDevOpsAgentInvestigation(
  request: DevOpsAgentRequest,
  groupId: string,
  options?: AgentClientOptions
): Promise<AgentTriggerResponse> {
  const config: Required<AgentClientOptions> = { ...DEFAULT_OPTIONS, ...options };

  let creds: { url: string; secret: string };
  try {
    creds = await loadWebhookCredentials();
  } catch (err: unknown) {
    return {
      success: false,
      error: `Failed to load webhook credentials: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  let lastError: string | undefined;
  let lastStatusCode: number | undefined;

  for (let attempt = 1; attempt <= config.maxRetries; attempt++) {
    const triggeredAt = new Date().toISOString();
    const payload = buildWebhookPayload(request, groupId, triggeredAt);
    const body = JSON.stringify(payload);
    const signature = computeHmacSignature(body, triggeredAt, creds.secret);

    try {
      const result = await httpTransport(
        new URL(creds.url),
        body,
        {
          'Content-Type': 'application/json',
          'x-amzn-event-timestamp': triggeredAt,
          'x-amzn-event-signature': signature,
        },
        config.timeoutMs
      );

      lastStatusCode = result.statusCode;

      if (result.statusCode >= 200 && result.statusCode < 300) {
        return {
          success: true,
          incidentId: payload.incidentId,
          triggeredAt,
          statusCode: result.statusCode,
        };
      }

      lastError = `HTTP ${result.statusCode}: ${result.body.substring(0, 500)}`;
      // 4xx 不该重试（除了 429）
      if (result.statusCode >= 400 && result.statusCode < 500 && result.statusCode !== 429) {
        return {
          success: false,
          incidentId: payload.incidentId,
          triggeredAt,
          statusCode: result.statusCode,
          error: lastError,
        };
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      if (errMsg === 'TIMEOUT') {
        if (attempt >= config.maxRetries) {
          return {
            success: false,
            incidentId: payload.incidentId,
            triggeredAt,
            timedOut: true,
            error: `DevOps Agent webhook call timed out after ${config.timeoutMs}ms`,
          };
        }
        lastError = `TIMEOUT after ${config.timeoutMs}ms`;
      } else {
        lastError = errMsg;
      }
    }

    if (attempt < config.maxRetries) {
      const delay = calculateBackoffDelay(
        attempt,
        config.initialDelayMs,
        config.backoffMultiplier
      );
      console.log(
        `DevOps Agent webhook call failed (attempt ${attempt}/${config.maxRetries}): ${lastError}. Retrying in ${delay}ms...`
      );
      await sleep(delay);
    }
  }

  return {
    success: false,
    statusCode: lastStatusCode,
    error: `DevOps Agent webhook call failed after ${config.maxRetries} attempts. Last error: ${lastError}`,
  };
}
