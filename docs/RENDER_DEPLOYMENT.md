# Deploying TerraAgent to Render (render.com)

This guide walks you through deploying **TerraAgent** to [Render](https://render.com) using either the **1-Click Blueprint (Recommended)** or **Manual Setup**.

---

## Architecture Overview on Render

When deployed to Render, TerraAgent runs 5 interconnected services:

1. **`terraagent-postgres`** (Managed PostgreSQL Database) — Stores scan audit history, job records, and PR tracking.
2. **`terraagent-redis`** (Managed Redis) — Celery task queue broker and real-time SSE log streamer.
3. **`terraagent-api`** (FastAPI Web Service) — Handles REST API requests, validation endpoints, and authentication.
4. **`terraagent-celery`** (Background Worker) — Runs the 8-agent LangGraph execution pipeline asynchronously.
5. **`terraagent-frontend`** (Next.js 14 Web Service) — Modern web UI with live SSE progress, DAG dependency graphs, and download bundle actions.

---

## Method 1: Deploy with Render Blueprint (Recommended 1-Click Setup)

Render Blueprints let you spin up the entire multi-service stack with a single configuration file (`render.yaml`).

### Step 1: Push Code to GitHub / GitLab
Ensure this repository (including `render.yaml`, `backend/`, and `frontend/`) is pushed to your Git repository:
```bash
git add .
git commit -m "Add Render deployment configuration"
git push origin main
```

### Step 2: Create a New Blueprint on Render
1. Log into your [Render Dashboard](https://dashboard.render.com).
2. Click **New +** in the top navigation bar.
3. Select **Blueprint**.
4. Connect your GitHub/GitLab repository containing TerraAgent.
5. Render will detect `render.yaml` and list all 5 components:
   - `terraagent-postgres`
   - `terraagent-redis`
   - `terraagent-api`
   - `terraagent-celery`
   - `terraagent-frontend`
6. Click **Apply**.

### Step 3: Configure Secret Environment Variables
In the Render dashboard:
1. Go to **`terraagent-api`** -> **Environment**:
   - Set **`GEMINI_API_KEY`**: Your Google Gemini API Key (used for fast, high-performance cloud LLM generation on Render without needing local GPU containers).
2. Go to **`terraagent-celery`** -> **Environment**:
   - Set **`GEMINI_API_KEY`**: Same Google Gemini API Key.
   - (Optional) Set **`INFRACOST_API_KEY`** if you want automated monthly infrastructure cost estimates.
3. Click **Save Changes**. Render will automatically redeploy the services.

---

## Method 2: Manual Deployment via Render UI

If you prefer to configure services manually in the Render dashboard:

### 1. Create Managed PostgreSQL
- **New +** -> **PostgreSQL**
- Name: `terraagent-postgres`
- Database: `terraagent`
- User: `terraagent`
- Plan: Free or Starter

### 2. Create Managed Redis
- **New +** -> **Redis**
- Name: `terraagent-redis`
- Plan: Free or Starter

### 3. Create FastAPI Backend Web Service
- **New +** -> **Web Service**
- Runtime: **Docker**
- Dockerfile Path: `backend/Dockerfile`
- Docker Context: `backend`
- Health Check Path: `/api/health`
- Environment Variables:
  - `PORT`: `8000`
  - `ENVIRONMENT`: `production`
  - `DATABASE_URL`: *[Internal Database URL from PostgreSQL service]*
  - `REDIS_URL`: *[Internal Redis Connection String]*
  - `CELERY_BROKER_URL`: *[Internal Redis Connection String]*
  - `CELERY_RESULT_BACKEND`: *[Internal Redis Connection String]*
  - `GEMINI_API_KEY`: *[Your Gemini API Key]*
  - `GEMINI_MODEL`: `gemini-2.5-flash`
  - `TERRAAGENT_API_KEY`: *[Random secure 32+ character string]*
  - `CORS_ORIGINS`: `*`
  - `OUTPUT_DIR`: `/tmp/terraagent`

### 4. Create Celery Worker Background Service
- **New +** -> **Background Worker**
- Runtime: **Docker**
- Dockerfile Path: `backend/Dockerfile`
- Docker Context: `backend`
- Start Command: `celery -A services.celery_app worker --beat --loglevel=info`
- Environment Variables: Same `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `GEMINI_API_KEY`, `TERRAAGENT_API_KEY` as above.

### 5. Create Next.js Frontend Web Service
- **New +** -> **Web Service**
- Runtime: **Docker**
- Dockerfile Path: `frontend/Dockerfile`
- Docker Context: `frontend`
- Environment Variables:
  - `PORT`: `3000`
  - `NEXT_PUBLIC_API_URL`: `/api`
  - `BACKEND_API_INTERNAL_URL`: `http://terraagent-api:8000/api/:path*` (or your backend service's internal/external Render URL)
  - `TERRAAGENT_API_KEY`: *[Same API key configured on the backend]*

---

## Verifying the Deployment

1. **Check Backend Health**:
   Visit `https://<your-backend-service>.onrender.com/api/health` in your browser.
   Expected response:
   ```json
   {
     "status": "healthy",
     "service": "terraagent-api",
     "version": "1.0.0"
   }
   ```
2. **Access Frontend**:
   Open `https://<your-frontend-service>.onrender.com`.
   The UI will load, show a healthy backend connection status indicator, and allow you to initiate AWS scans and synthesis jobs.
