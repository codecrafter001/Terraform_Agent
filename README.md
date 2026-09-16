# TerraAgent 🌍⚡

> **Stateful Multi-Agent AI System for Converting AWS ClickOps Infrastructure into Production-Grade, Validated Terraform & OpenTofu Code.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111.0-green.svg)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14_App_Router-black.svg)](https://nextjs.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

---

## 📖 Description & Overview

**TerraAgent** is an enterprise-ready, multi-agent AI system built with **LangGraph**, **FastAPI**, **Next.js 14**, and LLMs (Google Gemini / Ollama). It systematically scans live AWS environments using strictly read-only APIs, maps resource dependency graphs (DAGs), classifies infrastructure adoption strategies, and synthesizes modular, battle-tested Terraform and OpenTofu HCL code.

TerraAgent replaces error-prone manual reverse-engineering and risky ClickOps workflows with automated, deterministic IaC generation accompanied by multi-layer policy checks, automated repair loops, and human-in-the-loop approvals.

---

## ✨ Key Features

- **🔍 Read-Only AWS Cloud Discovery**: Deep scan of VPCs, Subnets, EC2, S3, RDS, Security Groups, Route Tables, and IAM with zero resource mutations.
- **🕸️ Graph Topology & DAG Engine**: Generates interactive D3.js dependency graphs and computes topological waves for safe adoption and migration.
- **🤖 8-Agent LangGraph Pipeline**: Orchestrated workflow with intent routing, resource classification, code composition, validation, security policy auditing, self-repair loops, and documentation generation.
- **🛡️ Multi-Layer Security & Policy Auditing**: Automated scans with `tfsec`, `Checkov`, `Trivy`, and `Conftest` (OPA Rego policies).
- **🔁 Autonomous Self-Repair Loops**: Automatically analyzes syntax and policy violations, prompts targeted repairs, and re-validates up to 2 retry cycles.
- **🔒 Hard Safety Guardrails**:
  - Code-level chokepoints strictly prevent execution of `terraform apply`, `terraform destroy`, or `terraform import`.
  - Zero AWS credential leaks: all sensitive keys and tokens are scrubbed from logs, errors, and LLM prompts.
- **📦 Production-Ready Export Bundles**: Generates modular `.tf` files (`main.tf`, `variables.tf`, `outputs.tf`, `providers.tf`), import manifests, migration checklists, and downloadable ZIP archives.
- **🌐 Cloud & Local Deployment**: 1-Click deployment to Render (`render.yaml`) or local orchestration via Docker Compose.

---

## 🏗️ 8-Agent Architecture Pipeline

```mermaid
flowchart TD
    User([User Request / Scan]) --> IntentRouter[1. Intent Router]
    IntentRouter --> Discovery[2. Cloud Discovery Agent]
    Discovery --> GraphAgent[3. Dependency Graph Agent]
    GraphAgent --> Classifier[4. Resource Classifier & Adoption Planner]
    Classifier --> Composer[5. Terraform Composer Agent]
    Composer --> Validator[6. Validation Agent (fmt / init / validate)]
    Validator --> Security[7. Policy & Security Agent (tfsec, Checkov, OPA)]
    Security -->|Findings Detected| Repair[8. Repair Agent]
    Repair -->|Retry <= 2| Validator
    Security -->|Passed / Reviewed| DocAgent[9. Documentation & Packaging Agent]
    DocAgent --> Artifacts([Verified Terraform ZIP Bundle & Migration Report])
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | Next.js 14 (App Router), TypeScript, Tailwind CSS, D3.js, Lucide Icons |
| **Backend API** | FastAPI (Python 3.11+ async), Pydantic v2 (SecretStr), Uvicorn |
| **Agent Orchestration** | LangGraph (StateGraph), LangChain Core |
| **LLM Support** | Google Gemini API (Cloud) & Ollama (`codellama`, `llama3` for Local) |
| **Task Queue & Cache** | Celery + Redis 7 + Server-Sent Events (SSE) Streaming |
| **Database** | PostgreSQL (SQLAlchemy ORM audit log & job tracker) |
| **IaC & Security CLI** | Terraform CLI, OpenTofu CLI, `tfsec`, `Checkov`, `Trivy`, `Conftest` (OPA) |
| **Deployment** | Render Blueprint (`render.yaml`), Docker, Docker Compose |

---

## 🚀 Quick Start (Local Docker Compose)

### 1. Clone & Configure Environment
```bash
git clone https://github.com/yourusername/terraagent.git
cd terraagent
cp .env.example .env
```
Edit `.env` and set `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, and optionally `GEMINI_API_KEY`.

### 2. Run with Docker Compose
```bash
docker compose up -d --build
```

### 3. Access Services
- **Web UI**: [http://localhost:3000](http://localhost:3000)
- **API Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **API Health Check**: [http://localhost:8000/api/health](http://localhost:8000/api/health)

---

## ☁️ Deploy to Render

TerraAgent includes a native Render Blueprint ([`render.yaml`](render.yaml)) supporting 1-click deployment for all 5 services:

1. Push your repository to GitHub / GitLab.
2. In [Render Dashboard](https://dashboard.render.com), click **New +** $\rightarrow$ **Blueprint**.
3. Select this repository and click **Apply**.
4. Add your `GEMINI_API_KEY` under the Environment tab of `terraagent-api` and `terraagent-celery`.

👉 *For detailed setup, see the [Render Deployment Guide](docs/RENDER_DEPLOYMENT.md).*

---

## 🔒 Security & Safety Principles

1. **Read-Only AWS Actions**: Read-only APIs (`Describe*`, `Get*`, `List*`) are used exclusively.
2. **Execution Guardrail**: Subprocess execution is strictly blocked for mutating commands (`apply`/`destroy`).
3. **Zero Secret Storage**: AWS Access Keys, Secret Keys, and Session Tokens are masked with `SecretStr` and never stored or forwarded to LLMs.
4. **Sandboxed Sandboxing**: All Terraform CLI formatting and validation occur within isolated temporary sandbox directories.

---

## 📄 License
Distributed under the MIT License.
