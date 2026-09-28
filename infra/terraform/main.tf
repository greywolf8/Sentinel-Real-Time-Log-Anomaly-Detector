# Terraform module for CloudWatch resources (docs/sentinel-plan.md section 10)
# Creates log group, alarm, and least-privilege IAM policy for Sentinel

terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # For LocalStack testing
  endpoints {
    cloudwatch = var.localstack_endpoint
    logs       = var.localstack_endpoint
    iam        = var.localstack_endpoint
  }

  # Skip credentials validation for LocalStack
  skip_credentials_validation = var.use_localstack
  skip_metadata_api_check     = var.use_localstack
  skip_requesting_account_id  = var.use_localstack
}

# CloudWatch Logs log group for alerts
resource "aws_cloudwatch_log_group" "sentinel_alerts" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days

  tags = {
    Name        = "sentinel-alerts"
    Environment = var.environment
    Project     = "sentinel"
  }
}

# CloudWatch alarm for high error rate
resource "aws_cloudwatch_metric_alarm" "high_error_rate" {
  alarm_name          = "${var.alarm_prefix}-high-error-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = var.alarm_evaluation_periods
  metric_name         = "ErrorRate"
  namespace           = var.metric_namespace
  period              = var.alarm_period
  statistic           = "Average"
  threshold           = var.error_rate_threshold
  alarm_description   = "Alert when error rate exceeds threshold"
  treat_missing_data  = "notBreaching"

  dimensions {
    Service = var.service_name
  }

  tags = {
    Name        = "sentinel-high-error-rate"
    Environment = var.environment
    Project     = "sentinel"
  }
}

# IAM policy for least-privilege access
resource "aws_iam_policy" "sentinel_delivery" {
  name        = "${var.policy_prefix}-sentinel-delivery"
  description = "Least-privilege IAM policy for Sentinel alert delivery"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = [
          aws_cloudwatch_log_group.sentinel_alerts.arn,
          "${aws_cloudwatch_log_group.sentinel_alerts.arn}:log-stream:*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
        ]
        Resource = aws_cloudwatch_metric_alarm.high_error_rate.arn
      },
    ]
  })
}


