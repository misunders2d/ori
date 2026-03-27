FROM python:3.10-slim

# Install git so the agent can safely check its own footprints during self-evolution
RUN apt-get update && apt-get install -y --no-install-recommends git \
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

# Create non-root user and set permissions. 
# Explicitly ensuring /code/data exists before mounting
RUN groupadd -r agentgroup && useradd -m -r -g agentgroup agentuser \
    && mkdir -p /code/data \
    && chown -R agentuser:agentgroup /code 

# Ensure uv has a writable cache directory
ENV UV_CACHE_DIR=/code/.cache/uv
RUN mkdir -p $UV_CACHE_DIR && chown -R agentuser:agentgroup /code/.cache

USER agentuser

# Self evolution git requirements
RUN git config --global user.name "Ori Autonomous Daemon" \
    && git config --global user.email "bot@ori-agent.local" \
    && git config --global --add safe.directory /code

ENTRYPOINT ["uv", "run", "python", "run_bot.py"]
