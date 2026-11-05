FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir '.[runtime]'
USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "ai_dlc.adapters.orchestrator.host:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--lifespan", "off"]
