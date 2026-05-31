import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

/**
 * Lambda functions required by the workflow.
 */
export interface WorkflowLambdas {
  alarmRouter: lambda.IFunction;
  alarmGrouper: lambda.IFunction;
  rcaAnalyzer: lambda.IFunction;
  /** DingTalk notifier — replaces the feishu notifier from the upstream reference. */
  dingtalkNotifier: lambda.IFunction;
}

// -----------------------------------------------------------------------------
// Workflow State Transition Logic
// -----------------------------------------------------------------------------

export type WorkflowStatus =
  | 'pending'
  | 'analyzing'
  | 'completed'
  | 'failed'
  | 'timed_out'
  | 'notified';

export const ALL_WORKFLOW_STATUSES: readonly WorkflowStatus[] = [
  'pending',
  'analyzing',
  'completed',
  'failed',
  'timed_out',
  'notified',
] as const;

/**
 * Allowed paths:
 *   pending → analyzing → completed → notified
 *   pending → analyzing → failed
 *   pending → analyzing → timed_out → notified
 */
export const VALID_WORKFLOW_TRANSITIONS: Readonly<Record<WorkflowStatus, readonly WorkflowStatus[]>> = {
  pending: ['analyzing'],
  analyzing: ['completed', 'failed', 'timed_out'],
  completed: ['notified'],
  failed: [],
  timed_out: ['notified'],
  notified: [],
};

export const WORKFLOW_INITIAL_STATE: WorkflowStatus = 'pending';

export function isValidWorkflowTransition(from: WorkflowStatus, to: WorkflowStatus): boolean {
  return VALID_WORKFLOW_TRANSITIONS[from]?.includes(to) ?? false;
}

export function isValidWorkflowTransitionSequence(sequence: readonly WorkflowStatus[]): boolean {
  if (sequence.length === 0) return true;
  if (sequence[0] !== WORKFLOW_INITIAL_STATE) return false;

  for (let i = 0; i < sequence.length - 1; i++) {
    if (!isValidWorkflowTransition(sequence[i], sequence[i + 1])) {
      return false;
    }
  }
  return true;
}

/**
 * Builds the Step Functions state machine definition for the
 * CloudWatch Alarm Auto RCA workflow.
 *
 * Workflow:
 *   [Start] → InvokeAlarmRouter → CheckFiltered?
 *     → Yes (filtered=true) → RecordFiltered → [End]
 *     → No → InvokeAlarmGrouper → CheckShouldWait?
 *       → Yes → WaitForGroupWindow → InvokeRCAAnalyzer
 *       → No → InvokeRCAAnalyzer
 *   InvokeRCAAnalyzer (.waitForTaskToken) → CheckRCAStatus?
 *     → "completed" → InvokeDingTalkNotifier(rca_complete) → RecordSuccess
 *     → "partial"/"failed" → InvokeDingTalkNotifier(rca_partial) → RecordPartial
 */
export function buildWorkflowDefinition(
  scope: Construct,
  lambdas: WorkflowLambdas
): sfn.StateMachine {
  // --- Terminal states ---
  const recordFiltered = new sfn.Pass(scope, 'RecordFiltered', {
    result: sfn.Result.fromObject({ outcome: 'filtered' }),
    resultPath: '$.workflowResult',
  });

  const recordSuccess = new sfn.Pass(scope, 'RecordSuccess', {
    result: sfn.Result.fromObject({ outcome: 'success' }),
    resultPath: '$.workflowResult',
  });

  const recordPartial = new sfn.Pass(scope, 'RecordPartial', {
    result: sfn.Result.fromObject({ outcome: 'partial' }),
    resultPath: '$.workflowResult',
  });

  const recordFailure = new sfn.Pass(scope, 'RecordFailure', {
    result: sfn.Result.fromObject({ outcome: 'failure' }),
    resultPath: '$.workflowResult',
  });

  // --- Step 1: Invoke AlarmRouter ---
  const invokeAlarmRouter = new tasks.LambdaInvoke(scope, 'InvokeAlarmRouter', {
    lambdaFunction: lambdas.alarmRouter,
    outputPath: '$.Payload',
    retryOnServiceExceptions: true,
  });

  // --- Step 2: Check if alarm was filtered ---
  const checkFiltered = new sfn.Choice(scope, 'CheckFiltered');

  // --- Step 3: Invoke AlarmGrouper ---
  const invokeAlarmGrouper = new tasks.LambdaInvoke(scope, 'InvokeAlarmGrouper', {
    lambdaFunction: lambdas.alarmGrouper,
    payload: sfn.TaskInput.fromObject({
      alarm: sfn.JsonPath.entirePayload,
    }),
    outputPath: '$.Payload',
    retryOnServiceExceptions: true,
  });

  // --- Step 4: Check if should wait for grouping window ---
  const checkShouldWait = new sfn.Choice(scope, 'CheckShouldWait');

  // --- Step 5: Wait for grouping window ---
  const waitForGroupWindow = new sfn.Wait(scope, 'WaitForGroupWindow', {
    time: sfn.WaitTime.secondsPath('$.waitSeconds'),
  });

  const prepareWaitSeconds = new sfn.Pass(scope, 'PrepareWaitSeconds', {
    parameters: {
      'groupId.$': '$.groupId',
      'alarms.$': '$.alarms',
      'isNewGroup.$': '$.isNewGroup',
      'shouldWait.$': '$.shouldWait',
      'waitUntil.$': '$.waitUntil',
      'waitSeconds': 120,
    },
  });

  // --- Step 6: Invoke RCAAnalyzer (.waitForTaskToken pattern) ---
  // Lambda 触发 DevOps Agent webhook 后立即返回；SFN 在这一步挂起，
  // 直到 InvestigationEventHandler 调用 SendTaskSuccess(taskToken, ...)
  // 把调查结果回传，SFN 才继续走 CheckRCAStatus 分支。
  const invokeRCAAnalyzer = new tasks.LambdaInvoke(scope, 'InvokeRCAAnalyzer', {
    lambdaFunction: lambdas.rcaAnalyzer,
    integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
    payload: sfn.TaskInput.fromObject({
      groupId: sfn.JsonPath.stringAt('$.groupId'),
      alarms: sfn.JsonPath.listAt('$.alarms'),
      taskToken: sfn.JsonPath.taskToken,
    }),
    retryOnServiceExceptions: true,
    taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(13)),
  });

  // --- Step 7: Check RCA status ---
  const checkRCAStatus = new sfn.Choice(scope, 'CheckRCAStatus');

  // --- Step 8a: Invoke DingTalkNotifier for complete RCA ---
  const invokeDingTalkNotifierComplete = new tasks.LambdaInvoke(
    scope,
    'InvokeDingTalkNotifierComplete',
    {
      lambdaFunction: lambdas.dingtalkNotifier,
      payload: sfn.TaskInput.fromObject({
        rcaReport: sfn.JsonPath.objectAt('$.rcaReport'),
        webhookCredentials: [],
        notificationType: 'rca_complete',
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    }
  );

  // --- Step 8b: Invoke DingTalkNotifier for partial/timeout RCA ---
  const invokeDingTalkNotifierPartial = new tasks.LambdaInvoke(
    scope,
    'InvokeDingTalkNotifierPartial',
    {
      lambdaFunction: lambdas.dingtalkNotifier,
      payload: sfn.TaskInput.fromObject({
        rcaReport: sfn.JsonPath.objectAt('$.rcaReport'),
        webhookCredentials: [],
        notificationType: 'rca_partial',
      }),
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    }
  );

  const checkNotificationResult = new sfn.Choice(scope, 'CheckNotificationResult');

  // --- Wire up the workflow ---
  invokeAlarmRouter.next(checkFiltered);

  checkFiltered
    .when(sfn.Condition.booleanEquals('$.filtered', true), recordFiltered)
    .otherwise(invokeAlarmGrouper);

  invokeAlarmGrouper.next(checkShouldWait);

  checkShouldWait
    .when(
      sfn.Condition.booleanEquals('$.shouldWait', true),
      prepareWaitSeconds.next(waitForGroupWindow).next(invokeRCAAnalyzer)
    )
    .otherwise(invokeRCAAnalyzer);

  invokeRCAAnalyzer.next(checkRCAStatus);

  checkRCAStatus
    .when(
      sfn.Condition.stringEquals('$.status', 'completed'),
      invokeDingTalkNotifierComplete
    )
    .otherwise(invokeDingTalkNotifierPartial);

  invokeDingTalkNotifierComplete.next(checkNotificationResult);

  checkNotificationResult
    .when(sfn.Condition.booleanEquals('$.success', true), recordSuccess)
    .otherwise(recordFailure);

  invokeDingTalkNotifierPartial.next(recordPartial);

  // --- Create the state machine ---
  const stateMachine = new sfn.StateMachine(scope, 'AlarmRCAWorkflow', {
    definitionBody: sfn.DefinitionBody.fromChainable(invokeAlarmRouter),
    stateMachineType: sfn.StateMachineType.STANDARD,
    timeout: cdk.Duration.minutes(15),
    tracingEnabled: true,
    comment:
      'CloudWatch Alarm Auto RCA Workflow (DingTalk) — orchestrates alarm parsing, ' +
      'grouping, RCA analysis, and DingTalk notification',
  });

  return stateMachine;
}
