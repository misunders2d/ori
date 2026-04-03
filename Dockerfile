FROM mirror.gcr.io/library/python:3.11-slim

LABEL project="ori"

# --- 1. SYSTEM DEPENDENCIES ---
# Install git, gosu, curl, sqlite3, Node.js, and Docker CLI (for health stats + spawning)
RUN apt-get update && apt-get install -y --no-install-recommends git gosu curl sqlite3 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.10

WORKDIR /code

# --- 2. LAYER OPTIMIZATION: DEPENDENCIES ---
# Copy dependency files first to leverage Docker build cache
COPY ./pyproject.toml ./README.md ./uv.lock* ./

# --- 3. CORE ARCHITECTURE ---
# Explicitly copy architecture files to keep the base image functional even without mounts
COPY ./app ./app
COPY ./skills ./skills
COPY ./interfaces ./interfaces
COPY ./tests ./tests
COPY ./run_bot.py ./
COPY ./entrypoint.sh ./

# Sync dependencies (frozen ensures we respect the lockfile)
RUN uv sync --frozen

# --- 4. ENVIRONMENT CONFIGURATION ---
ENV DOTENV_PATH="/code/data/.env"
ENV PYTHONUNBUFFERED=1
ENV GIT_TERMINAL_PROMPT=0

# Create non-root user for the daemon
RUN groupadd -r agentgroup && useradd -m -r -g agentgroup agentuser \
    && mkdir -p /code/data \
    && chown -R agentuser:agentgroup /code 

# Caches stay in home to avoid overlay conflicts with bind-mounted volumes
ENV UV_CACHE_DIR=/home/agentuser/.cache/uv
ENV NPM_CONFIG_CACHE=/home/agentuser/.cache/npm
RUN mkdir -p $UV_CACHE_DIR $NPM_CONFIG_CACHE \
    && chown -R agentuser:agentgroup /home/agentuser/.cache

# --- 5. EXECUTION PROTOCOL ---
# Add and configure entrypoint
# This entrypoint handles host UID/GID remapping for perfect permission alignment.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Starts as root, drops to agentuser via gosu in entrypoint.sh after UID/GID sync
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/code/.venv/bin/python", "run_bot.py"]
