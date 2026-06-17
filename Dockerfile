FROM python:3.12-slim

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + all system deps (single command)
RUN playwright install --with-deps chromium

# Copy only the source files needed at runtime
COPY agent.py .
COPY agente_villas/ agente_villas/

ENV GOOGLE_CLOUD_PROJECT=abahanaweb \
    GOOGLE_CLOUD_LOCATION=europe-west1 \
    GOOGLE_GENAI_USE_VERTEXAI=true

# Cloud Run injects $PORT; default 8080 for local docker run
CMD ["sh", "-c", "adk web agente_villas --host 0.0.0.0 --port ${PORT:-8080} --session_service_uri memory://"]
