import type { DeploymentStatus, DeploymentTarget } from "./types";

// backend/deploy/decision_engine.py::TARGETS
export const TARGET_LABELS: Record<DeploymentTarget, string> = {
  static_site: "Static site · S3 + CloudFront",
  lambda_http: "Function · Lambda + HTTPS URL",
  ecs_service: "Container · ECS Fargate + ALB",
  fullstack_app: "Full stack · CloudFront + ECS + RDS",
};

// backend/deploy/config.py::MAX_UPLOAD_BYTES default
export const MAX_UPLOAD_MB = 25;

export const DEPLOY_REGIONS = [
  "us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
  "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ca-central-1", "sa-east-1",
];

// backend/deploy/store.py::IN_PROGRESS
export const IN_PROGRESS_STATUSES: DeploymentStatus[] = [
  "SOURCE_RECEIVED",
  "ANALYZING",
  "BUILDING",
  "VERIFYING",
  "PLANNING",
  "APPLYING",
];

