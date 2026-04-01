FROM mirror.gcr.io/library/python:3.10-slim

LABEL project="ori"

# Install git, gosu, curl and Node.js
RUN apt-get update && apt-get install -y --no-install-recommends git gosu curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.10

WORKDIR /code

# Copy dependencies first for cache layer
COPY ./pyproject.toml ./README.md ./uv.lock* ./

# Copy core architecture
COPY ./app ./app
COPY ./skills ./skills
COPY ./interfaces ./interfaces
COPY ./tests ./tests
COPY ./run_bot.py ./

# Sync dependencies 
RUN uv sync --frozen

# Set default env vars for headless operation
ENV DOTENV_PATH="/code/data/.env"
ENV PYTHONUNBUFFERED=1
# Force non-interactive git commands
ENV GIT_TERMINAL_PROMPT=0

# Create non-root user and set default permissions
RUN groupadd -r agentgroup && useradd -m -r -g agentgroup agentuser \
    && mkdir -p /code/data \
    && chown -R agentuser:agentgroup /code 

# Place caches in agentuser's home dir — outside /code to avoid bind-mount overlay issues
ENV UV_CACHE_DIR=/home/agentuser/.cache/uv
ENV NPM_CONFIG_CACHE=/home/agentuser/.cache/npm
RUN mkdir -p $UV_CACHE_DIR $NPM_CONFIG_CACHE \
    && chown -R agentuser:agentgroup /home/agentuser/.cache

# Add and configure entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Note: We do NOT use "USER agentuser" here so the container starts as root, 
# runs entrypoint.sh to map the UID/GID, and then drops to agentuser via gosu.

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uv", "run", "python", "run_bot.py"]
