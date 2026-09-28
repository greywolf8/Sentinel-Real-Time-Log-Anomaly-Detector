# Outputs for the Sentinel Terraform module

output "log_group_arn" {
  description = "ARN of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.sentinel_alerts.arn
}

output "alarm_arn" {
  description = "ARN of the CloudWatch alarm"
  value       = aws_cloudwatch_metric_alarm.high_error_rate.arn
}

output "iam_policy_arn" {
  description = "ARN of the IAM policy for Sentinel delivery"
  value       = aws_iam_policy.sentinel_delivery.arn
}

output "log_group_name" {
  description = "Name of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.sentinel_alerts.name
}

output "alarm_name" {
  description = "Name of the CloudWatch alarm"
  value       = aws_cloudwatch_metric_alarm.high_error_rate.alarm_name
}
