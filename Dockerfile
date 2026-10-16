FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy MC_DASH_HOST=0.0.0.0 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
VOLUME /app/data
EXPOSE 18006
CMD ["uv", "run", "--no-sync", "python", "app.py"]
