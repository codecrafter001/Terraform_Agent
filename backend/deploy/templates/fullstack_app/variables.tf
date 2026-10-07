variable "deployment_id" {
  description = "TerraAgent deployment id; prefixes every resource name."
  type        = string
  validation {
    condition     = can(regex("^dep-[0-9a-f]{12}$", var.deployment_id))
    error_message = "deployment_id must look like dep-<12 hex characters>."
  }
}

variable "region" {
  description = "AWS region for the whole stack."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region name."
  }
}

variable "environment" {
  description = "Free-form environment label, used as a tag; \"production\" also keeps 7 days of database backups and a final snapshot."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.environment))
    error_message = "environment must be 1-32 letters, digits, '-' or '_'."
  }
}

variable "container_port" {
  description = "Port the backend container listens on."
  type        = number
  default     = 8080
  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be between 1 and 65535."
  }
}

variable "cpu" {
  description = "Fargate CPU units (256, 512, 1024, 2048, 4096)."
  type        = number
  default     = 256
  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.cpu)
    error_message = "cpu must be a valid Fargate CPU size (256, 512, 1024, 2048, 4096)."
  }
}

variable "memory_mb" {
  description = "Fargate memory in MB (512, 1024, 2048, 4096, 8192)."
  type        = number
  default     = 512
  validation {
    condition     = contains([512, 1024, 2048, 4096, 8192], var.memory_mb)
    error_message = "memory_mb must be a valid Fargate memory allocation."
  }
}

variable "desired_count" {
  description = "Number of backend task replicas to run."
  type        = number
  default     = 1
  validation {
    condition     = var.desired_count >= 1 && var.desired_count <= 10
    error_message = "desired_count must be between 1 and 10."
  }
}

variable "image_tag" {
  description = "Backend image tag in ECR (content-addressed from source.zip)."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,128}$", var.image_tag))
    error_message = "image_tag must be 1-128 characters (letters, digits, _, ., -)."
  }
}

variable "log_retention_days" {
  description = "CloudWatch logs retention period in days."
  type        = number
  default     = 30
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch retention value (1-365)."
  }
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for every role this stack creates; defaults to the account's TerraAgentWorkloadBoundary."
  type        = string
  default     = null
  validation {
    condition     = var.permissions_boundary_arn == null || can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:policy/", var.permissions_boundary_arn))
    error_message = "permissions_boundary_arn must be an IAM policy ARN."
  }
}

variable "vpc_cidr" {
  description = "CIDR block of the deployment's own VPC."
  type        = string
  default     = "10.42.0.0/16"
  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be an IPv4 CIDR block of /20 or larger."
  }
}

variable "source_file" {
  description = "Path (relative to this module) of the source.zip CodeBuild builds from."
  type        = string
  validation {
    condition     = can(regex("^artifacts/[A-Za-z0-9_.-]+\\.zip$", var.source_file))
    error_message = "source_file must be a zip under artifacts/."
  }
}

variable "health_check_path" {
  description = "Path the load balancer health check requests on the backend."
  type        = string
  default     = "/"
  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]{0,200}$", var.health_check_path))
    error_message = "health_check_path must start with / and contain only URL path characters."
  }
}

variable "backend_dir" {
  description = "Folder (relative to the project root) the backend image is built from; empty for the root."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^([A-Za-z0-9_-]{1,64})?$", var.backend_dir))
    error_message = "backend_dir must be empty or a single folder name."
  }
}

variable "frontend_enabled" {
  description = "Whether the project has a separately built frontend served from S3."
  type        = bool
  default     = false
}

variable "frontend_dir" {
  description = "Folder (relative to the project root) of the frontend; empty for the root."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^([A-Za-z0-9_-]{1,64})?$", var.frontend_dir))
    error_message = "frontend_dir must be empty or a single folder name."
  }
}

variable "frontend_build" {
  description = "Run npm ci/install and npm run build for the frontend in CodeBuild."
  type        = bool
  default     = false
}

variable "frontend_output" {
  description = "Committed frontend output folder, relative to frontend_dir; empty to look for dist/, build/ or out/."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^([A-Za-z0-9_.-]{1,64}(/[A-Za-z0-9_.-]{1,64})*)?$", var.frontend_output)) && !strcontains(var.frontend_output, "..")
    error_message = "frontend_output must be a relative folder path."
  }
}

variable "frontend_api_env" {
  description = "Build-time frontend variables (VITE_*, REACT_APP_*, NEXT_PUBLIC_*, VUE_APP_*) set to the deployed API URL."
  type        = list(string)
  default     = []
  validation {
    condition     = length(var.frontend_api_env) <= 10 && alltrue([for k in var.frontend_api_env : can(regex("^(VITE|REACT_APP|NEXT_PUBLIC|VUE_APP)_[A-Z0-9_]{1,60}$", k))])
    error_message = "frontend_api_env entries must be VITE_/REACT_APP_/NEXT_PUBLIC_/VUE_APP_ variable names."
  }
}

variable "frontend_api_suffix" {
  description = "Appended to the site URL for frontend_api_env: /api, or empty when the frontend adds /api itself."
  type        = string
  default     = "/api"
  validation {
    condition     = contains(["", "/api"], var.frontend_api_suffix)
    error_message = "frontend_api_suffix must be empty or /api."
  }
}

variable "api_strip_prefix" {
  description = "Remove the /api prefix before requests reach a backend whose routes don't use it."
  type        = bool
  default     = false
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

variable "database_engine" {
  description = "RDS engine to create: postgres, mysql, or null for no database."
  type        = string
  default     = null
  validation {
    condition     = var.database_engine == null || contains(["postgres", "mysql"], coalesce(var.database_engine, "none"))
    error_message = "database_engine must be postgres, mysql or null."
  }
}

variable "database_url_scheme" {
  description = "Scheme of the DATABASE_URL the generated entrypoint builds (postgresql, mysql+pymysql, ...)."
  type        = string
  default     = null
  validation {
    condition     = var.database_url_scheme == null || can(regex("^(postgresql|mysql)(\\+[a-z0-9]{1,20})?$", coalesce(var.database_url_scheme, "postgresql")))
    error_message = "database_url_scheme must be postgresql or mysql with an optional +driver."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
  validation {
    condition     = contains(["db.t4g.micro", "db.t4g.small", "db.t4g.medium", "db.t4g.large", "db.m7g.large"], var.db_instance_class)
    error_message = "db_instance_class must be one of db.t4g.micro/small/medium/large or db.m7g.large."
  }
}

variable "db_allocated_storage_gb" {
  description = "Initial database storage in GB (autoscaling up to 5x)."
  type        = number
  default     = 20
  validation {
    condition     = var.db_allocated_storage_gb >= 20 && var.db_allocated_storage_gb <= 500
    error_message = "db_allocated_storage_gb must be between 20 and 500."
  }
}

variable "db_multi_az" {
  description = "Run the database with a standby in a second availability zone (doubles its cost)."
  type        = bool
  default     = false
}

variable "secret_env_keys" {
  description = "Environment variable names that get an empty Secrets Manager secret, injected into the container."
  type        = list(string)
  default     = []
  validation {
    condition = length(var.secret_env_keys) <= 30 && alltrue([
      for k in var.secret_env_keys : can(regex("^[A-Z][A-Z0-9_]{0,63}$", k)) && !startswith(k, "AWS_")
    ])
    error_message = "secret_env_keys must be up to 30 UPPER_CASE names, none starting with AWS_."
  }
}

variable "run_migrations" {
  description = "Run the detected schema command (npm run db:push, prisma migrate deploy, alembic upgrade head...) before the server starts."
  type        = bool
  default     = false
}

variable "cdn_enabled" {
  description = "Put CloudFront (HTTPS, caching) in front of the app. Always on when there is a separate frontend."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Days of automated database backups (0 turns them off)."
  type        = number
  default     = 7
  validation {
    condition     = var.db_backup_retention_days >= 0 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be between 0 and 35."
  }
}

variable "db_final_snapshot" {
  description = "Take a final database snapshot when the stack is destroyed."
  type        = bool
  default     = true
}

variable "database_kind" {
  description = "rds: a single RDS instance; aurora: Aurora Serverless v2 (database_engine picks PostgreSQL or MySQL)."
  type        = string
  default     = "rds"
  validation {
    condition     = contains(["rds", "aurora"], var.database_kind)
    error_message = "database_kind must be rds or aurora."
  }
}

variable "aurora_min_acu" {
  description = "Aurora Serverless v2 minimum capacity (ACU); 0 pauses the database after 5 idle minutes."
  type        = number
  default     = 0.5
  validation {
    condition     = contains([0, 0.5, 1, 2, 4, 8, 16], var.aurora_min_acu)
    error_message = "aurora_min_acu must be 0, 0.5, 1, 2, 4, 8 or 16."
  }
}

variable "aurora_max_acu" {
  description = "Aurora Serverless v2 maximum capacity (ACU)."
  type        = number
  default     = 4
  validation {
    condition     = var.aurora_max_acu >= 1 && var.aurora_max_acu <= 128
    error_message = "aurora_max_acu must be between 1 and 128."
  }
}

variable "cache_enabled" {
  description = "Create an ElastiCache Serverless (Valkey) cache and pass REDIS_URL to the app."
  type        = bool
  default     = false
}

variable "cache_max_gb" {
  description = "Upper limit for the cache's stored data, in GB."
  type        = number
  default     = 1
  validation {
    condition     = var.cache_max_gb >= 1 && var.cache_max_gb <= 100
    error_message = "cache_max_gb must be between 1 and 100."
  }
}

variable "uploads_bucket_enabled" {
  description = "Create a private S3 bucket for file uploads that the app's task role can use."
  type        = bool
  default     = false
}

variable "worker_command" {
  description = "Command of the background worker service (same image); empty for no worker."
  type        = list(string)
  default     = []
  validation {
    condition     = length(var.worker_command) <= 8 && alltrue([for t in var.worker_command : can(regex("^[A-Za-z0-9:_.=-]{1,200}$", t))])
    error_message = "worker_command must be up to 8 plain tokens."
  }
}

variable "autoscaling_max_count" {
  description = "Most app tasks autoscaling may run (equal to desired_count: no scaling)."
  type        = number
  default     = 1
  validation {
    condition     = var.autoscaling_max_count >= 1 && var.autoscaling_max_count <= 20
    error_message = "autoscaling_max_count must be between 1 and 20."
  }
}

variable "autoscaling_cpu_target" {
  description = "Average CPU (%) autoscaling keeps the app tasks at."
  type        = number
  default     = 60
  validation {
    condition     = var.autoscaling_cpu_target >= 20 && var.autoscaling_cpu_target <= 90
    error_message = "autoscaling_cpu_target must be between 20 and 90."
  }
}
