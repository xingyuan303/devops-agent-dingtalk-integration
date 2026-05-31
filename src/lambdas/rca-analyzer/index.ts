/**
 * RCA Analyzer Lambda — webhook trigger 版本。
 *
 * 这一步在 Step Functions 里以 .waitForTaskToken 模式被调用。Lambda 收到的
 * 输入会在 RCAAnalyzerInput 之上额外携带 SFN 注入的 `taskToken`：
 *
 *   {
 *     groupId: string;
 *     alarms: AlarmRouterOutput[];
 *     taskToken: string;       // SFN 自动注入
 *   }
 *
 * 流程：
 *   1. 拿配置（拿 retry/timeout 参数）
 *   2. 调 buildRCAContext / triggerDevOpsAgentInvestigation 触发 webhook
 *   3. 把 incidentId、taskToken、alarms、groupId 写到
 *      WorkflowExecutionTable（PK=incidentId,SK=createdAt），由
 *      InvestigationEventHandler Lambda 在收到 EventBridge 事件后查这张表
 *      反查 taskToken 并 SendTaskSuccess。
 *   4. **同步阶段**到此为止，Lambda 不返回任何业务结果，只 throw 一个特殊
 *      Error 让 SFN 进入 waitForTaskToken（CDK 的 LambdaInvoke 在
 *      .waitForTaskToken 模式下，Lambda 函数 return 的内容会被忽略，因为
 *      最终输出由后续 SendTaskSuccess(token, payload) 决定）。
 *
 *   失败场景（webhook 不通、Secrets Manager 读不到等）：直接 SendTaskFailure
 *   让 SFN 走 partial 通知分支，避免 SFN 卡住直到 task timeout。
 */

import { CloudWatchClient, PutMetricDataCommand } from '@aws-sdk/client-cloudwatch';
import {
  SFNClient,
  SendTaskFailureCommand,
} from '@aws-sdk/client-sfn';
import { ConfigManager } from '../../shared/config-manager';
import { buildRCAContext } from './context-builder';
import {
  triggerDevOpsAgentInvestigation,
  AgentClientOptions,
} from './agent-client';
import { writePendingInvestigation } from './pending-store';
import { AlarmRouterOutput } from '../../shared/types';

const METRIC_NAMESPACE = 'CloudWatchAlarmAutoRCA';

const cloudWatchClient = new CloudWatchClient({});
const configManager = new ConfigManager();
const sfnClient = new SFNClient({});

/**
 * SFN .waitForTaskToken 注入的 input 形态。
 */
export interface RCAAnalyzerWebhookInput {
  groupId: string;
  alarms: AlarmRouterOutput[];
  taskToken: string;
}

async function emitMetrics(
  metricName:
    | 'RCAAnalysesInitiated'
    | 'RCAAnalysesCompleted'
    | 'RCAAnalysesFailed'
    | 'RCAWebhookSucceeded'
    | 'RCAWebhookFailed',
  value: number
): Promise<void> {
  try {
    await cloudWatchClient.send(
      new PutMetricDataCommand({
        Namespace: METRIC_NAMESPACE,
        MetricData: [
          {
            MetricName: metricName,
            Value: value,
            Unit: 'Count',
            Timestamp: new Date(),
          },
        ],
      })
    );
  } catch (error) {
    console.warn('[RCAAnalyzer] Failed to emit CloudWatch metrics', error);
  }
}

/**
 * RCAAnalyzer Lambda handler.
 *
 * 注意：这个 Lambda 在 SFN 里通过 .waitForTaskToken 集成调用。它本身的
 * 返回值不会被 SFN 当作步骤输出消费——SFN 一直挂起，直到收到匹配的
 * SendTaskSuccess(token, payload) 或 SendTaskFailure(token, error)。
 *
 * 因此这里：
 *   - 触发 webhook 成功：Lambda return 一个简单 ack，让 Lambda 自己干净退出，
 *     SFN 仍然在挂起态等回调。
 *   - 触发 webhook 失败：Lambda 调 SendTaskFailure 让 SFN 立刻沿
 *     partial 分支前进，避免卡死到 12 分钟 task timeout。
 */
export const handler = async (
  event: RCAAnalyzerWebhookInput
): Promise<{ ack: 'webhook_triggered' | 'webhook_failed_failure_sent' }> => {
  const startTime = Date.now();
  const { groupId, alarms, taskToken } = event;

  console.log(
    JSON.stringify({
      level: 'INFO',
      message: 'RCAAnalyzer invoked (webhook trigger mode)',
      groupId,
      alarmCount: alarms.length,
      hasTaskToken: !!taskToken,
      timestamp: new Date().toISOString(),
    })
  );

  if (!taskToken) {
    // 不应该发生：CDK 里 SFN task 一定会注入 taskToken。
    // 仍然防御一下，避免静默吞掉错误。
    throw new Error(
      'RCAAnalyzer must be invoked with SFN .waitForTaskToken so that taskToken is injected into the input.'
    );
  }

  await emitMetrics('RCAAnalysesInitiated', 1);

  try {
    const config = await configManager.getConfig();

    const agentOptions: AgentClientOptions = {
      maxRetries: config.retryPolicy.maxRetries,
      initialDelayMs: config.retryPolicy.initialDelay * 1000,
      backoffMultiplier: config.retryPolicy.backoffMultiplier,
      timeoutMs: 15000, // 单次 HTTP 调用 15s 已够用
    };

    const agentRequest = buildRCAContext(alarms);

    const triggerResult = await triggerDevOpsAgentInvestigation(
      agentRequest,
      groupId,
      agentOptions
    );

    if (!triggerResult.success || !triggerResult.incidentId || !triggerResult.triggeredAt) {
      // 触发失败：让 SFN 立刻进入 partial 路径
      await emitMetrics('RCAWebhookFailed', 1);
      await sendTaskFailure(
        taskToken,
        'WebhookTriggerFailed',
        triggerResult.error ?? 'unknown webhook trigger error'
      );
      console.log(
        JSON.stringify({
          level: 'ERROR',
          message: 'Webhook trigger failed; sent SendTaskFailure to SFN',
          groupId,
          error: triggerResult.error,
          statusCode: triggerResult.statusCode,
        })
      );
      return { ack: 'webhook_failed_failure_sent' };
    }

    // 成功触发 → 把 (incidentId, taskToken, groupId, alarms, triggeredAt)
    // 写到 WorkflowExecutionTable，等 EventBridge 事件回流时反查 token。
    await writePendingInvestigation({
      incidentId: triggerResult.incidentId,
      triggeredAt: triggerResult.triggeredAt,
      taskToken,
      groupId,
      alarms,
    });

    await emitMetrics('RCAWebhookSucceeded', 1);

    console.log(
      JSON.stringify({
        level: 'INFO',
        message: 'Webhook triggered; SFN task is now waiting for callback',
        groupId,
        incidentId: triggerResult.incidentId,
        triggeredAt: triggerResult.triggeredAt,
        duration: Date.now() - startTime,
      })
    );

    // 注意：SFN 仍然在挂起态。本 Lambda 干净退出。
    return { ack: 'webhook_triggered' };
  } catch (err: unknown) {
    const errorMessage = err instanceof Error ? err.message : String(err);
    await emitMetrics('RCAAnalysesFailed', 1);

    console.log(
      JSON.stringify({
        level: 'ERROR',
        message: 'RCAAnalyzer unexpected error; sending SendTaskFailure to SFN',
        groupId,
        error: errorMessage,
      })
    );

    try {
      await sendTaskFailure(taskToken, 'RCAAnalyzerError', errorMessage);
    } catch (sfErr) {
      console.warn('Failed to SendTaskFailure', sfErr);
    }
    // 不再 rethrow：SendTaskFailure 已经把错误透回 SFN，Lambda 干净退出。
    return { ack: 'webhook_failed_failure_sent' };
  }
};

async function sendTaskFailure(
  taskToken: string,
  errorCode: string,
  causeMessage: string
): Promise<void> {
  await sfnClient.send(
    new SendTaskFailureCommand({
      taskToken,
      error: errorCode,
      cause: causeMessage.substring(0, 32 * 1024 - 1),
    })
  );
}
