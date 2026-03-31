FROM python:3.10-slim

# Install git and gosu for least-privilege user mapping
RUN apt-get update && apt-get install -y --no-install-recommends git gosu \
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

# Ensure uv has a writable cache directory
ENV UV_CACHE_DIR=/code/.cache/uv
RUN mkdir -p $UV_CACHE_DIR && chown -R agentuser:agentgroup /code/.cache

# Add and configure entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Note: We do NOT use "USER agentuser" here so the container starts as root, 
# runs entrypoint.sh to map the UID/GID, and then drops to agentuser via gosu.

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uv", "run", "python", "run_bot.py"]
