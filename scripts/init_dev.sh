#!/usr/bin/env bash
set -e

echo "=== Initializing TerraAgent Development Environment ==="

COMPOSE_FILE="docker-compose.yml"

# --- Version checks -----------------------------------------------------
command -v docker >/dev/null 2>&1 || { echo "Docker is required but not installed. Aborting." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required but not installed. Aborting." >&2; exit 1; }

DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
echo "Docker version: ${DOCKER_VERSION}"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_VERSION=$(python3 --version 2>&1)
    echo "Python version: ${PYTHON_VERSION}"
else
    echo "Warning: python3 not found on host (only required for running the backend outside Docker)." >&2
fi

if command -v node >/dev/null 2>&1; then
    NODE_VERSION=$(node --version)
    echo "Node version: ${NODE_VERSION}"
else
    echo "Warning: node not found on host (only required for running the frontend outside Docker)." >&2
fi

# --- Environment file ----------------------------------------------------
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
else
    echo ".env already exists, leaving it as-is."
fi

# --- Build and start services --------------------------------------------
echo "Building and launching Docker containers..."
docker compose -f "${COMPOSE_FILE}" up --build -d

# --- Wait for healthchecks (not a blind sleep) ---------------------------
echo "Waiting for services to become healthy..."
SERVICES_WITH_HEALTHCHECKS="terraagent-api terraagent-redis"
TIMEOUT_SECONDS=180
ELAPSED=0

for svc in ${SERVICES_WITH_HEALTHCHECKS}; do
    echo "  Waiting on ${svc}..."
    while true; do
        STATUS=$(docker inspect --format '{{.State.Health.Status}}' "${svc}" 2>/dev/null || echo "unknown")
        if [ "${STATUS}" = "healthy" ]; then
            echo "  ${svc} is healthy."
            break
        fi
        if [ "${ELAPSED}" -ge "${TIMEOUT_SECONDS}" ]; then
            echo "  Timed out waiting for ${svc} (status: ${STATUS}). Continuing anyway - check 'docker compose logs ${svc}'." >&2
            break
        fi
        sleep 3
        ELAPSED=$((ELAPSED + 3))
    done
done

docker compose -f "${COMPOSE_FILE}" ps

# --- Pull the local LLM model ---------------------------------------------
echo "Pulling codellama model inside Ollama container (this can take a while on first run)..."
docker exec terraagent-ollama ollama pull codellama || echo "Warning: model pull failed - Ollama may still be starting up. Retry with: docker exec terraagent-ollama ollama pull codellama" >&2

echo ""
echo "=== TerraAgent Stack Ready ==="
echo "Frontend Dashboard: http://localhost:3000 (or via Nginx on http://localhost)"
echo "FastAPI Docs:       http://localhost:8000/docs"
echo "FastAPI Health:     http://localhost:8000/api/health"
echo "Prometheus Metrics: http://localhost:8000/metrics"
echo "Ollama LLM:         http://localhost:11434"
echo "Redis:              localhost:6379"
echo "PostgreSQL:         localhost:5432 (db=terraagent, user=terraagent)"
echo ""
echo "Optional profiles (not started by default):"
echo "  docker compose --profile test up -d terraagent-localstack   # integration tests"
echo "  docker compose --profile dev up -d terraagent-pgadmin       # pgAdmin at http://localhost:5050"
echo "  docker compose --profile monitoring up -d terraagent-prometheus terraagent-grafana"
echo "                                                                # Prometheus http://localhost:9090, Grafana http://localhost:3001"
echo ""
echo "Optional GPU passthrough for Ollama (requires NVIDIA Container Toolkit, unverified on this host):"
echo "  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d"
