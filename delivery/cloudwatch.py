"""CloudWatch delivery for alerts (docs/sentinel-plan.md section 10).

Puts log events to /sentinel/alerts, Embedded Metric Format lines for per-service and
per-component error rate, a --dry-run mode, and boto3 configured to use LocalStack via
an endpoint override.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# CloudWatch configuration
LOG_GROUP_NAME = "/sentinel/alerts"
LOG_STREAM_NAME_PREFIX = "sentinel-"
METRIC_NAMESPACE = "Sentinel"


@dataclass
class CloudWatchConfig:
    """Configuration for CloudWatch delivery."""

    log_group_name: str = LOG_GROUP_NAME
    log_stream_name_prefix: str = LOG_STREAM_NAME_PREFIX
    metric_namespace: str = METRIC_NAMESPACE
    dry_run: bool = False
    localstack_endpoint: str | None = None
    region_name: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None


class CloudWatchDelivery:
    """Delivers alerts to CloudWatch Logs and CloudWatch Metrics."""

    def __init__(self, config: CloudWatchConfig) -> None:
        self.config = config
        self._logs_client = self._create_logs_client()
        self._sequence_token: str | None = None
        self._ensure_log_group()

    def _create_logs_client(self) -> Any:
        """Create a CloudWatch Logs client, optionally pointing to LocalStack."""
        session = boto3.Session(
            region_name=self.config.region_name,
            aws_access_key_id=self.config.aws_access_key_id,
            aws_secret_access_key=self.config.aws_secret_access_key,
            aws_session_token=self.config.aws_session_token,
        )

        if self.config.localstack_endpoint:
            # Configure for LocalStack
            endpoint_url = self.config.localstack_endpoint
            logger.info(f"Using LocalStack endpoint: {endpoint_url}")
            return session.client(
                "logs",
                endpoint_url=endpoint_url,
                verify=False,  # LocalStack uses self-signed certs
            )

        return session.client("logs")

    def _ensure_log_group(self) -> None:
        """Ensure the log group exists."""
        if self.config.dry_run:
            logger.info("Dry run: skipping log group creation")
            return

        try:
            self._logs_client.create_log_group(logGroupName=self.config.log_group_name)
            logger.info(f"Created log group: {self.config.log_group_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
                logger.debug(f"Log group already exists: {self.config.log_group_name}")
            else:
                logger.error(f"Failed to create log group: {e}")
                raise

    def _get_or_create_log_stream(self) -> str:
        """Get or create a log stream for this instance."""
        # Use a timestamp-based stream name for this session
        stream_name = f"{self.config.log_stream_name_prefix}{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        if self.config.dry_run:
            logger.info(f"Dry run: would create log stream: {stream_name}")
            return stream_name

        try:
            self._logs_client.create_log_stream(
                logGroupName=self.config.log_group_name,
                logStreamName=stream_name,
            )
            logger.info(f"Created log stream: {stream_name}")
            return stream_name
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
                logger.debug(f"Log stream already exists: {stream_name}")
                return stream_name
            else:
                logger.error(f"Failed to create log stream: {e}")
                raise

    def _get_sequence_token(self, stream_name: str) -> str | None:
        """Get the sequence token for a log stream."""
        if self.config.dry_run:
            return None

        try:
            response = self._logs_client.describe_log_streams(
                logGroupName=self.config.log_group_name,
                logStreamNamePrefix=stream_name,
                limit=1,
            )
            streams = response.get("logStreams", [])
            if streams:
                return streams[0].get("uploadSequenceToken")
        except ClientError as e:
            logger.error(f"Failed to get sequence token: {e}")
        return None

    def put_log_event(self, alert: dict[str, Any]) -> bool:
        """Put a single alert as a log event to CloudWatch Logs."""
        if self.config.dry_run:
            logger.info(f"Dry run: would put log event for alert: {alert.get('alert_id', 'unknown')}")
            return True

        try:
            # Get or create log stream
            stream_name = self._get_or_create_log_stream()

            # Get sequence token
            if self._sequence_token is None:
                self._sequence_token = self._get_sequence_token(stream_name)

            # Create log event
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            message = json.dumps(alert)

            params = {
                "logGroupName": self.config.log_group_name,
                "logStreamName": stream_name,
                "logEvents": [
                    {
                        "timestamp": timestamp,
                        "message": message,
                    }
                ],
            }

            if self._sequence_token:
                params["sequenceToken"] = self._sequence_token

            # Put log event
            response = self._logs_client.put_log_events(**params)
            self._sequence_token = response.get("nextSequenceToken")
            logger.debug(f"Put log event for alert: {alert.get('alert_id', 'unknown')}")
            return True

        except ClientError as e:
            logger.error(f"Failed to put log event: {e}")
            # Reset sequence token on error
            self._sequence_token = None
            return False

    def put_emf_metric(self, metric_name: str, value: float, dimensions: dict[str, str]) -> bool:
        """Put a metric using Embedded Metric Format (EMF)."""
        if self.config.dry_run:
            logger.info(f"Dry run: would put EMF metric: {metric_name}={value}")
            return True

        try:
            # EMF format (single line JSON)
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            emf_message = {
                "_aws": {
                    "Timestamp": timestamp,
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self.config.metric_namespace,
                            "Dimensions": [["Service", "Component"], ["Service"]],
                            "Metrics": [
                                {"Name": metric_name, "Unit": "Count"},
                            ],
                        }
                    ],
                },
                "Service": dimensions.get("service", "unknown"),
                "Component": dimensions.get("component", "unknown"),
                metric_name: value,
            }

            # Put as a log event
            stream_name = self._get_or_create_log_stream()
            if self._sequence_token is None:
                self._sequence_token = self._get_sequence_token(stream_name)

            params = {
                "logGroupName": self.config.log_group_name,
                "logStreamName": stream_name,
                "logEvents": [
                    {
                        "timestamp": timestamp,
                        "message": json.dumps(emf_message),
                    }
                ],
            }

            if self._sequence_token:
                params["sequenceToken"] = self._sequence_token

            response = self._logs_client.put_log_events(**params)
            self._sequence_token = response.get("nextSequenceToken")
            logger.debug(f"Put EMF metric: {metric_name}={value}")
            return True

        except ClientError as e:
            logger.error(f"Failed to put EMF metric: {e}")
            self._sequence_token = None
            return False

    def put_error_rate_metrics(self, service: str, component: str, error_rate: float) -> bool:
        """Put error rate metrics for a service/component."""
        dimensions = {"service": service, "component": component}
        return self.put_emf_metric("ErrorRate", error_rate, dimensions)

    def put_alert_metrics(self, alert: dict[str, Any]) -> bool:
        """Put metrics derived from an alert."""
        service = alert.get("service", "unknown")
        component = alert.get("key", "unknown")
        severity = alert.get("severity", "INFO")

        # Error rate metric
        error_rate = alert.get("observed", 0.0)
        self.put_error_rate_metrics(service, component, error_rate)

        # Alert count metric (by severity)
        return self.put_emf_metric(
            f"AlertCount_{severity}",
            1.0,
            {"service": service, "component": component},
        )


def create_cloudwatch_delivery(
    dry_run: bool = False,
    localstack_endpoint: str | None = None,
) -> CloudWatchDelivery:
    """Create a CloudWatch delivery instance with standard configuration."""
    # Check for LocalStack endpoint in environment
    if localstack_endpoint is None:
        localstack_endpoint = os.environ.get("LOCALSTACK_ENDPOINT")

    config = CloudWatchConfig(
        dry_run=dry_run,
        localstack_endpoint=localstack_endpoint,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
    )

    return CloudWatchDelivery(config)


def create_alarm(
    metric_name: str,
    namespace: str = METRIC_NAMESPACE,
    threshold: float = 0.05,
    comparison: str = "GreaterThanThreshold",
    evaluation_periods: int = 1,
    period: int = 60,
) -> dict[str, Any]:
    """Create a CloudWatch alarm configuration (for Terraform)."""
    return {
        "alarm_name": f"sentinel-{metric_name}-alarm",
        "alarm_description": f"Alert when {metric_name} exceeds threshold",
        "metric_name": metric_name,
        "namespace": namespace,
        "statistic": "Average",
        "period": period,
        "evaluation_periods": evaluation_periods,
        "threshold": threshold,
        "comparison_operator": comparison,
        "treat_missing_data": "notBreaching",
    }
