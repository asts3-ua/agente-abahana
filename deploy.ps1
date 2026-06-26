# deploy.ps1 — despliega el agente Abahana en Cloud Run
# Requisito previo: gcloud auth login (una sola vez)
#
# Uso:
#   .\deploy.ps1           # primer deploy o redeploy completo
#   .\deploy.ps1 -SkipIAM  # solo redeploy (SA ya existe)

param([switch]$SkipIAM)

$PROJECT  = "abahanaweb"
$REGION   = "europe-west1"
$SERVICE  = "abahana-agent"
$SA_NAME  = "abahana-agent-sa"
$SA_EMAIL = "$SA_NAME@$PROJECT.iam.gserviceaccount.com"

# ── 1. Habilitar APIs necesarias ──────────────────────────────────────────────
Write-Host "Habilitando APIs..."
gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    aiplatform.googleapis.com `
    bigquery.googleapis.com `
    --project=$PROJECT

# ── 2. Service Account + permisos (solo primer deploy) ───────────────────────
if (-not $SkipIAM) {
    Write-Host "Creando service account..."
    gcloud iam service-accounts create $SA_NAME `
        --display-name="Abahana Agent - Cloud Run" `
        --project=$PROJECT

    Write-Host "Asignando roles IAM..."
    $roles = @(
        "roles/bigquery.dataViewer",   # leer tablas silver_clean
        "roles/bigquery.jobUser",      # ejecutar queries
        "roles/aiplatform.user"        # llamar a Gemini via Vertex AI
    )
    foreach ($role in $roles) {
        gcloud projects add-iam-policy-binding $PROJECT `
            --member="serviceAccount:$SA_EMAIL" `
            --role=$role
    }
}

# ── 3. Build + deploy en Cloud Run ───────────────────────────────────────────
Write-Host "Desplegando en Cloud Run (puede tardar)..."
gcloud run deploy $SERVICE `
    --source . `
    --project=$PROJECT `
    --region=$REGION `
    --service-account=$SA_EMAIL `
    --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=true" `
    --allow-unauthenticated `
    --memory=2Gi `
    --cpu=2 `
    --concurrency=10 `
    --timeout=120 `
    --min-instances=1 `
    --max-instances=3 `
    --session-affinity

# Nota: --allow-unauthenticated en la capa Cloud Run.
# La autenticación real la gestiona Streamlit via Google OAuth (secrets.toml).
# Subir a --no-allow-unauthenticated + Cloud IAP cuando se formalice la infra.

Write-Host ""
Write-Host "Deploy completado. La URL del servicio aparece arriba."
Write-Host "Acceso controlado por Streamlit (Google OAuth). Dominios: @abahana.com + @inferia.io"
