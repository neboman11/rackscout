FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS dependencies

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp/rackscout
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.13.6 && uv sync --frozen --no-dev --no-cache

FROM dependencies AS runtime
COPY --chown=pwuser:pwuser rackscout/ ./rackscout/
ENV PATH="/app/.venv/bin:$PATH"
USER pwuser
EXPOSE 8080
CMD ["uvicorn", "rackscout.api:app", "--host", "0.0.0.0", "--port", "8080"]
