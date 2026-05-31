import { AlarmRouterOutput } from '../../shared/types';

/**
 * Request structure for the AWS DevOps Agent Event Response API.
 */
export interface DevOpsAgentRequest {
  investigationType: 'alarm_response';
  context: {
    alarmArns: string[];
    resourceArns: string[];
    timeRange: { start: string; end: string };
    additionalContext: string;
  };
}

/**
 * Builds the RCA context request for the AWS DevOps Agent.
 *
 * Assembles all alarm ARNs, resource ARNs, a time range covering at least
 * 1 hour before the earliest alarm, and a descriptive additional context string.
 *
 * @param alarms - Array of parsed alarm outputs from the AlarmRouter
 * @returns A DevOpsAgentRequest ready to be sent to the DevOps Agent API
 */
export function buildRCAContext(alarms: AlarmRouterOutput[]): DevOpsAgentRequest {
  // 1. Collect all unique alarm ARNs
  const alarmArns = [...new Set(alarms.map((a) => a.alarmId))];

  // 2. Collect all unique resource ARNs, filtering out empty strings
  const resourceArns = [
    ...new Set(alarms.map((a) => a.resourceArn).filter((arn) => arn !== '')),
  ];

  // 3. Determine time range
  const timestamps = alarms
    .map((a) => a.stateChangeTimestamp)
    .filter((ts) => ts !== '')
    .map((ts) => new Date(ts).getTime())
    .filter((t) => !isNaN(t));

  const now = Date.now();

  const earliestTime = timestamps.length > 0 ? Math.min(...timestamps) : now;
  const latestTime = timestamps.length > 0 ? Math.max(...timestamps) : now;

  // Set start = earliest alarm time - 1 hour
  const oneHourMs = 60 * 60 * 1000;
  const timeRangeStart = new Date(earliestTime - oneHourMs).toISOString();
  const timeRangeEnd = new Date(latestTime).toISOString();

  // 4. Build additional context describing all alarms
  const alarmDescriptions = alarms.map((a) => {
    const parts: string[] = [];
    parts.push(`Alarm: ${a.alarmName}`);
    if (a.namespace) {
      parts.push(`Namespace: ${a.namespace}`);
    }
    if (a.metricName) {
      parts.push(`Metric: ${a.metricName}`);
    }
    parts.push(`Current Value: ${a.currentValue}`);
    parts.push(`Threshold: ${a.threshold}`);
    if (a.resourceArn) {
      parts.push(`Resource: ${a.resourceArn}`);
    }
    return parts.join(', ');
  });

  const additionalContext =
    alarmDescriptions.length > 0
      ? `Alarms triggering investigation:\n${alarmDescriptions.join('\n')}`
      : 'No alarm details available';

  return {
    investigationType: 'alarm_response',
    context: {
      alarmArns,
      resourceArns,
      timeRange: {
        start: timeRangeStart,
        end: timeRangeEnd,
      },
      additionalContext,
    },
  };
}
