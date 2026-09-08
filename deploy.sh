#!/usr/bin/env bash
# deploy.sh — despliega el agente Abahana en Cloud Run
# Requisito previo: gcloud auth login (una sola vez)
#
# Uso:
#   ./deploy.sh             # primer deploy o redeploy completo
#   ./deploy.sh --skip-iam  # solo redeploy (SA y roles ya existen)

set -euo pipefail

PROJECT="abahanaweb"
REGION="europe-west1"
SERVICE="abahana-agent"
SA_NAME="abahana-agent-sa"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

SKIP_IAM=0
for arg in "$@"; do
    case "$arg" in
        --skip-iam|-s)
            SKIP_IAM=1
            ;;
        -h|--help)
            sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Opción desconocida: $arg (usa --help)" >&2
            exit 1
            ;;
    esac
done

command -v gcloud >/dev/null 2>&1 || {
    echo "gcloud no está instalado o no está en el PATH." >&2
    exit 1
}

cd "$(dirname "$0")"

# ── 1. Habilitar APIs necesarias ──────────────────────────────────────────────
echo "Habilitando APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    --project="$PROJECT"

# ── 2. Service Account + permisos (solo primer deploy) ───────────────────────
if [ "$SKIP_IAM" -eq 0 ]; then
    if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
        echo "Service account ya existe: $SA_EMAIL"
    else
        echo "Creando service account..."
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Abahana Agent - Cloud Run" \
            --project="$PROJECT"
    fi

    echo "Asignando roles IAM..."
    ROLES=(
        "roles/bigquery.dataViewer"   # leer tablas silver_clean
        "roles/bigquery.jobUser"      # ejecutar queries
        "roles/aiplatform.user"       # llamar a Gemini via Vertex AI
    )
    for role in "${ROLES[@]}"; do
        gcloud projects add-iam-policy-binding "$PROJECT" \
            --member="serviceAccount:$SA_EMAIL" \
            --role="$role" \
            --condition=None \
            --quiet >/dev/null
        echo "  · $role"
    done
fi

# ── 3. Build + deploy en Cloud Run ───────────────────────────────────────────
echo "Desplegando en Cloud Run (puede tardar)..."
gcloud run deploy "$SERVICE" \
    --source . \
    --project="$PROJECT" \
    --region="$REGION" \
    --service-account="$SA_EMAIL" \
    --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=true" \
    --allow-unauthenticated \
    --memory=2Gi \
    --cpu=2 \
    --concurrency=10 \
    --timeout=120 \
    --min-instances=1 \
    --max-instances=3 \
    --session-affinity

# Nota: --allow-unauthenticated en la capa Cloud Run.
# La autenticación real la gestiona Streamlit via Google OAuth (secrets.toml).
# Subir a --no-allow-unauthenticated + Cloud IAP cuando se formalice la infra.

echo ""
echo "Deploy completado. La URL del servicio aparece arriba."
echo "Acceso controlado por Streamlit (Google OAuth). Dominios: @abahanavillas.com + @inferia.io"
