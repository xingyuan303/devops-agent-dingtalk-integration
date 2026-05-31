import { AlarmRouterInput, AlarmRouterOutput } from '../../shared/types';

/**
 * Parse a CloudWatch Alarm State Change event into a structured AlarmRouterOutput.
 *
 * Supports:
 * - Single metric alarms
 * - Metric math expression alarms
 * - Anomaly detection alarms
 * - Composite alarms (no metric info)
 *
 * Returns filtered: true with filterReason for malformed events.
 */
export function parseAlarmEvent(event: AlarmRouterInput): AlarmRouterOutput {
  try {
    return extractAlarmFields(event);
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'Unknown parsing error';
    return buildFilteredOutput(event, reason);
  }
}

function extractAlarmFields(event: AlarmRouterInput): AlarmRouterOutput {
  const detail = event.detail;

  if (!detail || !detail.alarmName) {
    throw new Error('Missing required field: detail.alarmName');
  }

  if (!detail.state || !detail.state.timestamp) {
    throw new Error('Missing required field: detail.state.timestamp');
  }

  const alarmName = detail.alarmName;
  const stateChangeTimestamp = detail.state.timestamp;
  const previousState = detail.previousState?.value ?? 'UNKNOWN';
  const accountId = event.account ?? '';
  const region = event.region ?? '';
  const alarmId = event.resources?.[0] ?? '';

  // Extract metric info (namespace, metricName, dimensions)
  const metricInfo = extractMetricInfo(detail);

  // Extract threshold and current value from reasonData
  const { threshold, currentValue } = extractThresholdAndValue(detail);

  // Build resource ARN from dimensions
  const resourceArn = buildResourceArn(metricInfo.dimensions, accountId, region);

  return {
    alarmId,
    alarmName,
    namespace: metricInfo.namespace,
    metricName: metricInfo.metricName,
    dimensions: metricInfo.dimensions,
    threshold,
    currentValue,
    stateChangeTimestamp,
    previousState,
    accountId,
    region,
    resourceArn,
    filtered: false,
  };
}

interface MetricInfo {
  namespace: string;
  metricName: string;
  dimensions: Record<string, string>;
}

/**
 * Extract metric information from the alarm configuration.
 * Handles single metric, metric math, anomaly detection, and composite alarms.
 */
function extractMetricInfo(detail: AlarmRouterInput['detail']): MetricInfo {
  const metrics = detail.configuration?.metrics;

  // Composite alarms have no metrics array
  if (!metrics || metrics.length === 0) {
    return { namespace: '', metricName: '', dimensions: {} };
  }

  // Find the first metric with a metricStat (covers single metric and metric math)
  const metricWithStat = metrics.find((m) => m.metricStat != null);

  if (metricWithStat?.metricStat) {
    const metric = metricWithStat.metricStat.metric;
    return {
      namespace: metric.namespace ?? '',
      metricName: metric.name ?? '',
      dimensions: metric.dimensions ?? {},
    };
  }

  // All metrics are expressions (anomaly detection band or pure math)
  // Try to find any metric that has returnData: true and a metricStat
  const returnDataMetric = metrics.find((m) => m.returnData && m.metricStat != null);
  if (returnDataMetric?.metricStat) {
    const metric = returnDataMetric.metricStat.metric;
    return {
      namespace: metric.namespace ?? '',
      metricName: metric.name ?? '',
      dimensions: metric.dimensions ?? {},
    };
  }

  // No metricStat found at all (pure expression-based or anomaly detection without direct metric)
  return { namespace: '', metricName: '', dimensions: {} };
}

/**
 * Extract threshold and current value from the state reasonData JSON string.
 */
function extractThresholdAndValue(detail: AlarmRouterInput['detail']): {
  threshold: number;
  currentValue: number;
} {
  const reasonData = detail.state?.reasonData;

  if (!reasonData) {
    return { threshold: 0, currentValue: 0 };
  }

  try {
    const parsed = JSON.parse(reasonData);

    // Standard metric alarm reasonData format
    const threshold = typeof parsed.threshold === 'number' ? parsed.threshold : 0;

    // currentValue can be in different fields depending on alarm type
    let currentValue = 0;
    if (typeof parsed.recentDatapoints === 'object' && Array.isArray(parsed.recentDatapoints)) {
      // Use the most recent datapoint
      const datapoints = parsed.recentDatapoints.filter(
        (dp: unknown) => typeof dp === 'number'
      );
      if (datapoints.length > 0) {
        currentValue = datapoints[datapoints.length - 1];
      }
    } else if (typeof parsed.queryResultValue === 'number') {
      currentValue = parsed.queryResultValue;
    } else if (typeof parsed.evaluatedDatapoints === 'object' && Array.isArray(parsed.evaluatedDatapoints)) {
      // Anomaly detection or newer format
      const evaluated = parsed.evaluatedDatapoints;
      if (evaluated.length > 0 && typeof evaluated[0].value === 'number') {
        currentValue = evaluated[0].value;
      }
    }

    return { threshold, currentValue };
  } catch {
    // Invalid JSON in reasonData
    return { threshold: 0, currentValue: 0 };
  }
}

/**
 * Build a resource ARN from alarm dimensions.
 * Maps common dimension patterns to their corresponding AWS resource ARNs.
 */
function buildResourceArn(
  dimensions: Record<string, string>,
  accountId: string,
  region: string
): string {
  if (!dimensions || Object.keys(dimensions).length === 0) {
    return '';
  }

  // EC2 Instance
  if (dimensions['InstanceId']) {
    return `arn:aws:ec2:${region}:${accountId}:instance/${dimensions['InstanceId']}`;
  }

  // RDS Instance
  if (dimensions['DBInstanceIdentifier']) {
    return `arn:aws:rds:${region}:${accountId}:db:${dimensions['DBInstanceIdentifier']}`;
  }

  // Lambda Function
  if (dimensions['FunctionName']) {
    return `arn:aws:lambda:${region}:${accountId}:function:${dimensions['FunctionName']}`;
  }

  // ELB / ALB
  if (dimensions['LoadBalancer']) {
    return `arn:aws:elasticloadbalancing:${region}:${accountId}:loadbalancer/${dimensions['LoadBalancer']}`;
  }

  // SQS Queue
  if (dimensions['QueueName']) {
    return `arn:aws:sqs:${region}:${accountId}:${dimensions['QueueName']}`;
  }

  // DynamoDB Table
  if (dimensions['TableName']) {
    return `arn:aws:dynamodb:${region}:${accountId}:table/${dimensions['TableName']}`;
  }

  // S3 Bucket
  if (dimensions['BucketName']) {
    return `arn:aws:s3:::${dimensions['BucketName']}`;
  }

  // ECS Cluster/Service
  if (dimensions['ClusterName'] && dimensions['ServiceName']) {
    return `arn:aws:ecs:${region}:${accountId}:service/${dimensions['ClusterName']}/${dimensions['ServiceName']}`;
  }

  // SNS Topic
  if (dimensions['TopicName']) {
    return `arn:aws:sns:${region}:${accountId}:${dimensions['TopicName']}`;
  }

  // Fallback: return empty string if no known dimension pattern matches
  return '';
}

/**
 * Build a filtered output with safe defaults for all fields.
 */
function buildFilteredOutput(event: AlarmRouterInput, filterReason: string): AlarmRouterOutput {
  return {
    alarmId: event?.resources?.[0] ?? '',
    alarmName: event?.detail?.alarmName ?? '',
    namespace: '',
    metricName: '',
    dimensions: {},
    threshold: 0,
    currentValue: 0,
    stateChangeTimestamp: event?.detail?.state?.timestamp ?? event?.time ?? '',
    previousState: event?.detail?.previousState?.value ?? 'UNKNOWN',
    accountId: event?.account ?? '',
    region: event?.region ?? '',
    resourceArn: '',
    filtered: true,
    filterReason,
  };
}
