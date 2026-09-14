variable "create_vpc" {
  description = "Whether to create a new VPC or use existing one"
  type        = bool
  default     = true
}

variable "main_cidr_block" {
  description = "CIDR block for the VPC (Class B - 172.168.0.0/16)"
  type        = string
  default     = "172.168.0.0/16"
}

variable "project_tag" {
  description = "Project name tag"
  type        = string
  default     = "demo-project"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = ["172.168.1.0/24", "172.168.2.0/24"]
}

variable "azs" {
  description = "Availability zones"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "enable_alb_access_logs" {
  description = "Whether to enable ALB access logging to S3"
  type        = bool
  default     = true
}

variable "alb_logs_bucket_name" {
  description = "Optional explicit S3 bucket name for ALB access logs. If empty, a name is derived from project_tag + account id."
  type        = string
  default     = ""
}

variable "alb_logs_prefix" {
  description = "Prefix (folder) within the S3 bucket for ALB access logs"
  type        = string
  default     = "alb"
}

variable "alb_logs_retention_days" {
  description = "Number of days to retain ALB access log objects before expiration"
  type        = number
  default     = 30
}
