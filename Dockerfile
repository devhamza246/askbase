FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY benchmark ./benchmark
COPY ui ./ui
RUN pip install ".[ui]" && useradd --create-home --uid 10002 askbase
USER askbase
EXPOSE 8000 8501
# Schema, roles and seed data are created by the one-shot `bootstrap` service (python -m app.bootstrap), not here.
CMD ["uvicorn", "app.api:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
