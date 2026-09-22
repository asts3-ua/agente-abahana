FROM python:3.12-slim

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + all system deps (single command)
RUN playwright install --with-deps chromium

# Copy only the source files needed at runtime
COPY agent.py .
COPY chat_app.py .
COPY conversation_store.py .
COPY visualizaciones.py .
COPY filtros.py .
COPY assets/ assets/
COPY agente_villas/ agente_villas/
COPY .streamlit/ .streamlit/

ENV GOOGLE_CLOUD_PROJECT=abahanaweb \
    GOOGLE_CLOUD_LOCATION=global \
    GOOGLE_GENAI_USE_VERTEXAI=true

# Cloud Run injects $PORT; default 8080 for local docker run.
# Sin estadísticas de uso Streamlit no intenta escribir su machine_id en
# /root/.streamlit, que en Cloud Run es de solo lectura: fallaba en cada clic.
# (El config.toml que ya lo desactiva no se lee: el volumen de secretos tapa
# /app/.streamlit.)
CMD ["sh", "-c", "streamlit run chat_app.py --server.port ${PORT:-8080} --server.address 0.0.0.0 --browser.gatherUsageStats false"]
