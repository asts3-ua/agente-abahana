FROM python:3.12-slim

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + all system deps (single command)
RUN playwright install --with-deps chromium

# Copy only the source files needed at runtime
COPY agent.py .
COPY guardrails.py .
COPY chat_app.py .
COPY conversation_store.py .
COPY assets/ assets/
COPY agente_villas/ agente_villas/
COPY .streamlit/ .streamlit/

ENV GOOGLE_CLOUD_PROJECT=abahanaweb \
    GOOGLE_CLOUD_LOCATION=europe-west1 \
    GOOGLE_GENAI_USE_VERTEXAI=true

# Cloud Run injects $PORT; default 8080 for local docker run
CMD ["sh", "-c", "streamlit run chat_app.py --server.port ${PORT:-8080} --server.address 0.0.0.0"]
