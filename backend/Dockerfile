# ==============================================================================
# TerraAgent Backend Dockerfile (FastAPI & Agent Environment)
# ==============================================================================
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TERRAFORM_VERSION=1.8.5 \
    OPENTOFU_VERSION=1.8.5 \
    TFSEC_VERSION=v1.28.1 \
    TRIVY_VERSION=0.74.0 \
    CONFTEST_VERSION=0.69.0 \
    CHECKOV_VERSION=3.3.16 \
    INFRACOST_VERSION=0.10.39 \
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/local/bin \
    TF_PLUGIN_CACHE_DIR=/home/agent/.terraform.d/plugin-cache

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Terraform CLI
RUN curl -sSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o terraform.zip \
    && unzip terraform.zip -d /usr/local/bin \
    && rm terraform.zip \
    && terraform --version

# Install OpenTofu CLI - a drop-in-compatible fork of Terraform, installed
# alongside it so ScanRequest.terraform_binary (models/scan.py) can select
# either at request time; tools/terraform_runner.py just shells out to
# whichever binary name it's given.
RUN curl -sSL "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_amd64.zip" -o tofu.zip \
    && unzip tofu.zip -d /usr/local/bin \
    && rm tofu.zip \
    && tofu --version

# Install tfsec binary
RUN curl -sSL "https://github.com/aquasecurity/tfsec/releases/download/${TFSEC_VERSION}/tfsec-linux-amd64" -o /usr/local/bin/tfsec \
    && chmod +x /usr/local/bin/tfsec \
    && tfsec --version

# Install Trivy binary
RUN curl -sSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" -o trivy.tar.gz \
    && tar -xzf trivy.tar.gz -C /usr/local/bin trivy \
    && rm trivy.tar.gz \
    && trivy --version

# Install Conftest (OPA) binary
RUN curl -sSL "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_x86_64.tar.gz" -o conftest.tar.gz \
    && tar -xzf conftest.tar.gz -C /usr/local/bin conftest \
    && rm conftest.tar.gz \
    && conftest --version

# Install Checkov in an isolated pipx venv (its own boto3/networkx pins would
# otherwise conflict with the app's dependency versions)
RUN pip install --no-cache-dir pipx \
    && pipx install "checkov==${CHECKOV_VERSION}" \
    && checkov --version

# Install Infracost binary - estimates the monthly cost of generated HCL
# (tools/infracost_runner.py). Needs INFRACOST_API_KEY at runtime for real
# pricing data; without it, cost_agent reports "not estimated", never a
# fabricated $0.00 (see infracost_runner.py's tool_skipped handling).
RUN curl -sSL "https://github.com/infracost/infracost/releases/download/v${INFRACOST_VERSION}/infracost-linux-amd64.tar.gz" -o infracost.tar.gz \
    && tar -xzf infracost.tar.gz -C /usr/local/bin \
    && mv /usr/local/bin/infracost-linux-amd64 /usr/local/bin/infracost \
    && rm infracost.tar.gz \
    && chmod +x /usr/local/bin/infracost \
    && infracost --version

# Set work directory
WORKDIR /app

# Create non-root user for security compliance
RUN groupadd -g 1001 agent && \
    useradd -u 1001 -g agent -m -s /bin/bash agent && \
    mkdir -p /tmp/terraagent "${TF_PLUGIN_CACHE_DIR}" && \
    chown -R agent:agent /app /tmp/terraagent /home/agent/.terraform.d

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend codebase
COPY . .
RUN chown -R agent:agent /app

# Switch to non-root agent user
USER agent

# Expose API port
EXPOSE 8000

# No --reload: it forces a single worker and restarts on any file write - a
# dev-server behavior that has no place in the image this Dockerfile builds.
# Local development gets --reload back via docker-compose.dev.yml's command:
# override, not by baking it into the image itself.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
