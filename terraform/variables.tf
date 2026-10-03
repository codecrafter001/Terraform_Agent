variable "aws_region" {
  type        = string
  description = "Target AWS deployment region"
  default     = "us-east-1"
}

variable "environment" {
  type        = string
  description = "Target deployment stage (e.g. production, staging, development)"
  default     = "production"
}

variable "project_name" {
  type        = string
  description = "Project identifier tag"
  default     = "TerraAgent-Adopted"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance size"
  default     = "t3.micro"
}

variable "instance_name" {
  type        = string
  description = "Name tag for the EC2 instance"
  default     = "terraagent-ubuntu-ec2"
}
