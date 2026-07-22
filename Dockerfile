# Claude Fleet dashboard image. Builds for both amd64 and arm64 (see
# .github/workflows/docker-publish.yml, which builds this with buildx).
#
# This image bundles everything the current spawn model needs on the host
# today: git, tmux, and the claude CLI. It does NOT bundle a Docker daemon --
# nested Docker access for spawned agent sessions is a separate, sandboxed
# component documented in docs/deployment-k3s.md, not yet wired into the app.
FROM python:3.12-slim AS base

# --- OS-level prerequisites -------------------------------------------------
# git/tmux: what manager.py shells out to. curl/gnupg/ca-certificates: needed
# to install Node (for the claude CLI) and the Docker CLI client below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        tmux \
        curl \
        ca-certificates \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node, for `claude` (npm-distributed). NodeSource's setup script picks the
# right arch automatically (amd64/arm64).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# Docker CLI *client only* (no daemon) -- lets a spawned session run `docker`
# against a remote DOCKER_HOST (see deployment docs). Official static binary,
# arch-aware.
ARG DOCKER_CLI_VERSION=27.3.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) docker_arch=x86_64 ;; \
        arm64) docker_arch=aarch64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

# --- App --------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py crypto.py db.py manager.py health.py auth_claude.py ./
COPY templates/ templates/
COPY static/ static/

# All app state (SQLite DB, per-instance clones, per-instance git credential
# files) lives under /data -- mount a volume there in production.
ENV FLEET_ROOT=/data/instances \
    FLEET_DB=/data/fleet.db \
    FLEET_SECRETS=/data/secrets \
    FLEET_HOST=0.0.0.0 \
    FLEET_PORT=8700

RUN useradd --create-home --uid 1000 fleet \
    && mkdir -p /data \
    && chown -R fleet:fleet /app /data
USER fleet

EXPOSE 8700
ENTRYPOINT ["python", "app.py"]
