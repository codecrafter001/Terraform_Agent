variable "deployment_id" {
  description = "TerraAgent deployment id; prefixes every resource name."
  type        = string
  validation {
    condition     = can(regex("^dep-[0-9a-f]{12}$", var.deployment_id))
    error_message = "deployment_id must look like dep-<12 hex characters>."
  }
}

variable "region" {
  description = "AWS region for the function."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region name."
  }
}

variable "environment" {
  description = "Free-form environment label, used only as a tag."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.environment))
    error_message = "environment must be 1-32 letters, digits, '-' or '_'."
  }
}

variable "runtime" {
  description = "Lambda runtime, chosen by TerraAgent's builder."
  type        = string
  validation {
    condition     = contains(["python3.10", "python3.11", "python3.12", "python3.13", "nodejs20.x", "nodejs22.x"], var.runtime)
    error_message = "runtime is not a supported Lambda runtime."
  }
}

variable "handler" {
  description = "Handler detected by TerraAgent's analyzer (module.function)."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_./-]{1,128}$", var.handler))
    error_message = "handler must be a module path and function name."
  }
}

variable "memory_mb" {
  type    = number
  default = 256
  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_s" {
  type    = number
  default = 30
  validation {
    condition     = var.timeout_s >= 1 && var.timeout_s <= 900
    error_message = "timeout_s must be between 1 and 900."
  }
}

variable "public_url" {
  description = "true: anyone can call the function URL. false: callers must sign requests with IAM (SigV4)."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "package_file" {
  description = "Path of the function zip relative to this module, written by TerraAgent's builder."
  type        = string
}

variable "package_sha256_b64" {
  description = "base64(sha256(zip)), computed by the builder so redeploys only happen when the code changes."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for the execution role (required by the deploy role from Phase 3 on)."
  type        = string
  default     = null
}
