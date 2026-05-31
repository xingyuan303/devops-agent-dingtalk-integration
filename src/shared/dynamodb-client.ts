import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  PutCommand,
  GetCommand,
  UpdateCommand,
  QueryCommand,
} from '@aws-sdk/lib-dynamodb';
import {
  WorkflowExecution,
  AlarmGroup,
  AlarmRouterOutput,
} from './types';

// -----------------------------------------------------------------------------
// DynamoDB Client Setup
// -----------------------------------------------------------------------------

let dynamoClient: DynamoDBClient = new DynamoDBClient({});
let docClient: DynamoDBDocumentClient = DynamoDBDocumentClient.from(dynamoClient);

/**
 * Returns the current DynamoDBDocumentClient instance.
 */
export function getDocClient(): DynamoDBDocumentClient {
  return docClient;
}

/**
 * Allows injection of a custom DynamoDBDocumentClient (for testing).
 */
export function setDocClient(client: DynamoDBDocumentClient): void {
  docClient = client;
}

// -----------------------------------------------------------------------------
// Table Name Helpers
// -----------------------------------------------------------------------------

function getWorkflowExecutionTableName(): string {
  const name = process.env.WORKFLOW_EXECUTION_TABLE_NAME;
  if (!name) {
    throw new Error('WORKFLOW_EXECUTION_TABLE_NAME environment variable is not set');
  }
  return name;
}

function getAlarmGroupTableName(): string {
  const name = process.env.ALARM_GROUP_TABLE_NAME;
  if (!name) {
    throw new Error('ALARM_GROUP_TABLE_NAME environment variable is not set');
  }
  return name;
}

function getDeadLetterTableName(): string {
  const name = process.env.DEAD_LETTER_TABLE_NAME;
  if (!name) {
    throw new Error('DEAD_LETTER_TABLE_NAME environment variable is not set');
  }
  return name;
}

// -----------------------------------------------------------------------------
// TTL Calculation
// -----------------------------------------------------------------------------

/**
 * Calculates the DynamoDB TTL value.
 *
 * TTL = creation timestamp (Unix seconds) + retention period (days) × 86400
 *
 * @param createdAtUnixSeconds - The creation timestamp as Unix seconds
 * @param retentionDays - The retention period in days
 * @returns The TTL value as Unix seconds
 */
export function calculateTTL(createdAtUnixSeconds: number, retentionDays: number): number {
  return createdAtUnixSeconds + retentionDays * 86400;
}

// -----------------------------------------------------------------------------
// WorkflowExecution Table Operations
// -----------------------------------------------------------------------------

/**
 * Creates a new workflow execution record in DynamoDB.
 */
export async function createWorkflowExecution(execution: WorkflowExecution): Promise<void> {
  await docClient.send(
    new PutCommand({
      TableName: getWorkflowExecutionTableName(),
      Item: execution,
    })
  );
}

/**
 * Updates the status of a workflow execution and appends a state transition entry.
 *
 * @param executionId - The execution ID (partition key)
 * @param createdAt - The creation timestamp (sort key)
 * @param newStatus - The new status to set
 * @param reason - Optional reason for the state transition
 */
export async function updateWorkflowStatus(
  executionId: string,
  createdAt: string,
  newStatus: WorkflowExecution['status'],
  reason?: string
): Promise<void> {
  const transition = {
    from: '', // will be set by the caller or determined from current state
    to: newStatus,
    timestamp: new Date().toISOString(),
    ...(reason ? { reason } : {}),
  };

  await docClient.send(
    new UpdateCommand({
      TableName: getWorkflowExecutionTableName(),
      Key: {
        executionId,
        createdAt,
      },
      UpdateExpression:
        'SET #status = :newStatus, stateTransitions = list_append(if_not_exists(stateTransitions, :emptyList), :transition)',
      ExpressionAttributeNames: {
        '#status': 'status',
      },
      ExpressionAttributeValues: {
        ':newStatus': newStatus,
        ':transition': [transition],
        ':emptyList': [],
      },
    })
  );
}

/**
 * Retrieves a workflow execution record by its composite key.
 *
 * @param executionId - The execution ID (partition key)
 * @param createdAt - The creation timestamp (sort key)
 * @returns The WorkflowExecution record, or undefined if not found
 */
export async function getWorkflowExecution(
  executionId: string,
  createdAt: string
): Promise<WorkflowExecution | undefined> {
  const result = await docClient.send(
    new GetCommand({
      TableName: getWorkflowExecutionTableName(),
      Key: {
        executionId,
        createdAt,
      },
    })
  );

  return result.Item as WorkflowExecution | undefined;
}

// -----------------------------------------------------------------------------
// AlarmGroup Table Operations
// -----------------------------------------------------------------------------

/**
 * Creates a new alarm group record in DynamoDB.
 */
export async function createAlarmGroup(group: AlarmGroup): Promise<void> {
  await docClient.send(
    new PutCommand({
      TableName: getAlarmGroupTableName(),
      Item: group,
    })
  );
}

/**
 * Adds an alarm to an existing alarm group by appending it to the alarms list.
 *
 * @param resourceArn - The resource ARN (partition key)
 * @param groupId - The group ID (sort key)
 * @param alarm - The alarm to add to the group
 */
export async function addAlarmToGroup(
  resourceArn: string,
  groupId: string,
  alarm: AlarmRouterOutput
): Promise<void> {
  await docClient.send(
    new UpdateCommand({
      TableName: getAlarmGroupTableName(),
      Key: {
        resourceArn,
        groupId,
      },
      UpdateExpression: 'SET alarms = list_append(if_not_exists(alarms, :emptyList), :newAlarm)',
      ExpressionAttributeValues: {
        ':newAlarm': [alarm],
        ':emptyList': [],
      },
    })
  );
}

/**
 * Finds an active alarm group for a given resource ARN.
 *
 * An active group has status="collecting" and windowEnd > now.
 *
 * @param resourceArn - The resource ARN to query
 * @param now - The current time
 * @returns The active AlarmGroup, or undefined if none found
 */
export async function findActiveAlarmGroup(
  resourceArn: string,
  now: Date
): Promise<AlarmGroup | undefined> {
  const nowISO = now.toISOString();

  const result = await docClient.send(
    new QueryCommand({
      TableName: getAlarmGroupTableName(),
      KeyConditionExpression: 'resourceArn = :arn',
      FilterExpression: '#status = :collecting AND windowEnd > :now',
      ExpressionAttributeNames: {
        '#status': 'status',
      },
      ExpressionAttributeValues: {
        ':arn': resourceArn,
        ':collecting': 'collecting',
        ':now': nowISO,
      },
    })
  );

  if (result.Items && result.Items.length > 0) {
    return result.Items[0] as unknown as AlarmGroup;
  }
  return undefined;
}

// -----------------------------------------------------------------------------
// Dead Letter Table Operations
// -----------------------------------------------------------------------------

export interface DeadLetterNotification {
  notificationId: string;
  webhookUrl: string;
  message: any;
  failedAt: string;
  error: string;
}

/**
 * Writes a failed notification to the dead letter table for later retry.
 */
export async function writeDeadLetterNotification(
  notification: DeadLetterNotification
): Promise<void> {
  await docClient.send(
    new PutCommand({
      TableName: getDeadLetterTableName(),
      Item: notification,
    })
  );
}
