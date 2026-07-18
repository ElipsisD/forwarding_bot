FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bot.py excel_parser.py image_renderer.py ./

CMD ["uv", "run", "--no-sync", "python", "bot.py"]
