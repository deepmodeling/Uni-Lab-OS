FROM python:3.11-slim AS base
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY src/ src/
EXPOSE 8090
CMD ["uvicorn", "scheduler.main:app", "--host", "0.0.0.0", "--port", "8090"]
