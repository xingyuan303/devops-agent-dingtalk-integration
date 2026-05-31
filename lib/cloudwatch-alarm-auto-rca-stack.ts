import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface CloudwatchAlarmAutoRcaStackProps extends cdk.StackProps {
  /** DingTalk custom robot webhook URL (required) */
  readonly dingtalkWebhookUrl?: string;
  /** DingTalk custom robot HMAC signing secret */
  readonly dingtalkWebhookSecret?: string;

  /** DingTalk enterprise app credentials (for bot) */
  readonly dingtalkAppKey?: string;
  readonly dingtalkAppSecret?: string;
  readonly dingtalkAppToken?: string;
  readonly dingtalkAppAesKey?: string;

  /** DevOps Agent Space ID */
  readonly agentSpaceId?: string;
  /** Secret name in Secrets Manager holding DevOps Agent webhook url + secret */
  readonly devopsAgentWebhookSecretName?: string;

  /** Whether to deploy DingTalk bot stack (API Gateway + Lambda) */
  readonly deployDingtalkBot?: boolean;
}

/**
 * Skeleton — will be filled in subsequent batches.
 */
export class CloudwatchAlarmAutoRcaStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: CloudwatchAlarmAutoRcaStackProps) {
    super(scope, id, props);

    if (!props.agentSpaceId) {
      throw new Error('agentSpaceId is required. Pass via -c agentSpaceId=xxx or AGENT_SPACE_ID env var');
    }
    if (!props.dingtalkWebhookUrl) {
      throw new Error('dingtalkWebhookUrl is required. Pass via -c dingtalkWebhookUrl=xxx');
    }

    new cdk.CfnOutput(this, 'StackStatus', {
      value: 'skeleton — implementation in progress',
      description: 'See README for batch progress',
    });
  }
}
