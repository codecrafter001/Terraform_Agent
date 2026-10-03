# Design: faster ZIP → AWS deployments and service selection

Follow-up to `code-to-aws-deployment.md`. Covers two things: cutting deployment time, and letting
the user choose which AWS services an uploaded project gets (frontend, backend, database, cache,
storage, workers) instead of a single fixed stack per target.

Baseline: the `ecs_service` and `fullstack_app` templates on `feat/fullstack-deploy`.

## 1. Where the time goes today

First deploy of a full-stack app (CloudFront + ECS + RDS): **~25–35 min**.

| Step | Time | Cause |
|---|---|---|
| RDS instance | 8–12 min | AWS creation time; on the critical path |
| CloudFront distribution | 4–8 min | Global propagation |
| ECS service | waits for both | Task definition references the DB address and the CloudFront domain (`PUBLIC_URL`), so creation is serial |
| Image build (CodeBuild) | 5–10 min | Pipeline `depends_on` the ECS service, so it starts last; no Docker/npm caching |
| Service healthy | 2–3 min | 30 s health-check interval, 60 s grace, 30 s deregistration delay |
| Code-only redeploy | 25–35 min | Full plan → approve → apply cycle even when only source changed |

## 2. Speed optimizations

| # | Change | Saves | Where |
|---|---|---|---|
| 1 | Build the image in parallel with RDS: the pipeline no longer depends on the ECS service; CodeBuild rolls the service only if it exists | 5–10 min | `templates/{ecs_service,fullstack_app}/main.tf` |
| 2 | Containers no longer wait for CloudFront: `PUBLIC_URL` removed from the task definition | 3–6 min | `templates/fullstack_app/main.tf` |
| 3 | Build caching: `LOCAL_DOCKER_LAYER_CACHE` + `LOCAL_SOURCE_CACHE`, `BUILD_GENERAL1_MEDIUM` for every container target | 2–5 min per build | `aws_codebuild_project` |
| 4 | Health checks: 10 s interval, 30 s grace, 10 s deregistration delay | 1–2 min | target group, service |
| 5 | Fast code redeploy: a source-only change plans as one `aws_s3_object` update; apply takes seconds and the pipeline rolls the app. Stays inside the approved-plan rule | 25 min → 5–7 min | `routers/deployments.py`, UI |
| 6 | Dev/QA preset without CloudFront (load balancer URL) | 4–8 min | template toggle |
| 7 | Terraform: `-parallelism=20` on plan/apply, `-refresh=false` on a deployment's first plan (no state yet), shared provider cache | 30–60 s per run | `tools/terraform_runner.py`, `deploy/apply_runner.py` |
| 8 | Run Checkov, Trivy and Conftest concurrently in Verify | 1–2 min | `deploy/verify.py` |
| 9 | One shared TerraAgent VPC per account/region instead of one per deployment (also avoids the default 5-VPCs-per-region quota) | ~1 min, no quota failures | bootstrap + templates |

Expected results:

| Scenario | Today | After |
|---|---|---|
| First deploy, Production (CloudFront + RDS) | 25–35 min | 15–20 min (RDS is the floor) |
| First deploy, Dev (no CloudFront, small DB) | 25–35 min | 11–14 min |
| First deploy, no database | 15–20 min | 8–10 min |
| Code-only redeploy | 25–35 min | 5–7 min |

## 3. Service selection after upload

A **Choose your services** step between Analysis and Build. Every option is pre-filled from what
the analyzer detected, shows the evidence ("found `pg` in package.json"), and updates a live cost
and time estimate.

| Layer | Options | Detected from |
|---|---|---|
| Environment preset | Dev (fastest/cheapest: no CloudFront, 1-day backups, smallest sizes) · Staging · Production (CloudFront, 7-day backups, optional Multi-AZ) | Upload environment |
| Frontend | S3 + CloudFront · Served by the backend · None | Own build in `client/`/`frontend/`; Vite/CRA/Angular/Next export; backend serving static files |
| Backend compute | ECS Fargate · Lambda · None | `listen()`, `uvicorn`, handler exports |
| Backend size | CPU/memory presets, min/max tasks, CPU autoscaling (new) | — |
| Database | RDS PostgreSQL · RDS MySQL · Aurora Serverless v2 (new) · Database I host (empty `DATABASE_URL` secret) · None | `pg`, `mysql2`, Prisma/Drizzle config, SQLAlchemy drivers |
| DB size and safety | Instance class, storage, Multi-AZ, run migrations on start | `db:push`, `prisma migrate`, `alembic`, Django |
| Cache (new) | ElastiCache Valkey/Redis · None | `redis`, `ioredis`, `bull`, `celery[redis]` |
| File storage (new) | Private S3 uploads bucket, granted to the task role · None | `multer-s3`, `@aws-sdk/client-s3`, `boto3` |
| Background worker (new) | Second ECS service, same image, different command · None | `worker` script, Celery, BullMQ |
| Secrets | Detected env-var names → empty Secrets Manager secrets | `process.env.X`, `os.getenv("X")` |
| Domain (new) | Route 53 + ACM · Default CloudFront/ALB URL | — |

Rules (unchanged from the deployment-mode design): choices only ever become values in
`terraform.tfvars.json`; every service is a vetted template block behind a `count` toggle; the
plan-policy allowlist and bootstrap permissions grow with each service; no secret value ever
reaches TerraAgent, the plan or the state.

## 4. Phases

| Phase | Scope | Effort | Status |
|---|---|---|---|
| 1. Speed quick wins | #1–4, #7 | 1–2 days | Done (see below) |
| 2. Fast code redeploy | #5 | 2–3 days | Done (see below) |
| 3. Service selection UI | Presets, frontend/compute/DB choices, live estimate, #6 | 3–4 days | |
| 4. New services | Aurora Serverless v2, cache, S3 uploads, worker, domain, autoscaling | 5–7 days | |
| 5. Hardening | #8, #9, CloudFormation bootstrap mirror, sandbox end-to-end run per service | 2–3 days | |

### Phase 1 as built

- **#1** The pipeline and CodeBuild project no longer reference the ECS service: cluster and
  service names come from `local.name`, the service ARN is built from account/region, and the
  `post_build` step rolls the service only if it already exists. Checked with `terraform graph`:
  neither node depends on `aws_db_instance`, `aws_cloudfront_distribution` or `aws_ecs_service`.
- **#2** `PUBLIC_URL` is gone from the task definition, so the ECS service waits only for the
  database. The frontend build finds its CloudFront distribution at run time
  (`cloudfront:ListDistributions`, filtered by the distribution comment) and sets the
  `frontend_api_env` variables from its domain. The invalidation permission is account-wide
  (`distribution/*`) for the same reason; the workload boundary gained `ListDistributions`.
- **#3** CodeBuild uses `BUILD_GENERAL1_MEDIUM` and the LOCAL Docker-layer, source and custom
  caches (`/root/.npm` for the frontend build). Still open: ordering the generated Node
  Dockerfile so `npm ci` gets its own cached layer (needs care with postinstall scripts that
  read project files, e.g. `prisma generate`).
- **#4** Target group: 10 s health-check interval, 10 s deregistration delay; service: 30 s
  grace period.
- **#7** `-parallelism=20` on the deployment plan and on the pinned apply argv
  (`APPLY_ARGV_COMMON`). `-refresh=false` was dropped from the plan: a first plan has no state
  to refresh, so it would save nothing.

### Phase 2 as built

- `deploy/code_update.py`; API `POST /deployments/{id}/update-source` (ZIP) and
  `POST /deployments/{id}/update-source/github` (re-downloads the repo the deployment came from;
  token used once, never stored); UI card "Deploy a new version" on deployed apps.
- Allowed from DEPLOYED, or from FAILED/REJECTED/EXPIRED when `applied_at` shows a stack exists.
  The update keeps the deployment id (so the same Terraform state and resource names), target,
  settings, AWS account and outputs, clears the previous plan and approval, and stores
  `code_update` (previous source/image/`.tf` fingerprint/verification) in the new
  `code_update_json` column (added automatically by `init_db`).
- The pipeline chains analyze -> build -> verify -> plan with no clicks and stops at
  AWAITING_APPROVAL; approval and `apply_approved` are unchanged. If the new source is no
  longer eligible for the deployed target, it fails before building (nothing changes in AWS).
- Verification is reused when the rendered `.tf` files are byte-identical to the deployed
  release's (a code change only touches `terraform.tfvars.json`) and that verification didn't fail.
- A plan whose changes are only `aws_s3_object.source`, `aws_codebuild_project.builder`
  (update), `aws_ecs_task_definition.app` (update/replace) and `aws_ecs_service.app` (update) is
  `plan_summary.code_only`; it is not marked destructive (a task definition is a new ECS
  revision) and the approval card says so. Anything else in the plan keeps the normal rules.
- Apply clears `code_update`. Known behaviour: the apply updates the service to the new image
  tag while CodeBuild is still building it; the old tasks keep serving (ECS drains them only
  when new tasks are healthy) and CodeBuild's force-new-deployment rolls the service once the
  image is pushed.
- Not yet: destroying a stack whose code update failed (destroy still requires DEPLOYED,
  FAILED_PARTIAL or NEEDS_RECONCILIATION); retrying the update works.
