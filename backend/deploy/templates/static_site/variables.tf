variable "deployment_id" {
  description = "TerraAgent deployment id; prefixes every resource name."
  type        = string
  validation {
    condition     = can(regex("^dep-[0-9a-f]{12}$", var.deployment_id))
    error_message = "deployment_id must look like dep-<12 hex characters>."
  }
}

variable "region" {
  description = "AWS region for the bucket (CloudFront itself is global)."
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

variable "price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

variable "spa_mode" {
  description = "Serve index.html for unknown paths (single-page apps with client-side routing)."
  type        = bool
  default     = false
}

variable "release_id" {
  description = "Release identifier / prefix for zero-downtime versioning and rollback."
  type        = string
  default     = "initial"
}

variable "site_files" {
  description = "Object key -> local source path, content type and MD5, written by TerraAgent's builder."
  type = map(object({
    source       = string
    content_type = string
    md5          = string
  }))
}

