# Variables for the Sentinel Terraform module

variable "aws_region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "log_group_name" {
  description = "Name of the CloudWatch log group"
  type        = string
  default     = "/sentinel/alerts"
}

variable "log_retention_days" {
  description = "Retention period for log group in days"
  type        = number
  default     = 7
}

variable "metric_namespace" {
  description = "CloudWatch metric namespace"
  type        = string
  default     = "Sentinel"
}

variable "alarm_prefix" {
  description = "Prefix for alarm names"
  type        = string
  default     = "sentinel"
}

variable "policy_prefix" {
  description = "Prefix for IAM policy names"
  type        = string
  default     = "sentinel"
}

variable "alarm_period" {
  description = "Period in seconds for alarm evaluation"
  type        = number
  default     = 60
}

variable "alarm_evaluation_periods" {
  description = "Number of periods to evaluate for alarm"
  type        = number
  default     = 1
}

variable "error_rate_threshold" {
  description = "Error rate threshold for alarm (0-1)"
  type        = number
  default     = 0.05
}

variable "service_name" {
  description = "Service name for alarm dimensions"
  type        = string
  default     = "system"
}

variable "use_localstack" {
  description = "Use LocalStack for testing"
  type        = bool
  default     = false
}

variable "localstack_endpoint" {
  description = "LocalStack endpoint URL"
  type        = string
  default     = ""
}
