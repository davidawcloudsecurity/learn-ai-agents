variable "create_vpc" {
  description = "Whether to create a new VPC or use existing one"
  type        = bool
  default     = true
}

variable "main_cidr_block" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "172.16.0.0/16"
}

variable "project_tag" {
  description = "Project name tag"
  type        = string
  default     = "demo-project"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = ["172.16.1.0/24", "172.16.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
  default     = ["172.16.10.0/24", "172.16.11.0/24"]
}

variable "azs" {
  description = "Availability zones"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}
