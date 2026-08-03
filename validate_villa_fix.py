"""Validate corrected Villa ETL logic against bronze."""
from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 500 * 1024 * 1024

SQL = f"""
WITH resource_raw AS (
  SELECT ingestion_dt, run_id, payload AS json
  FROM `{PROJECT}.bronze_raw.etendo_OBRE_Resource`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
),
ficha_raw AS (
  SELECT ingestion_dt, run_id, payload AS json
  FROM `{PROJECT}.bronze_raw.etendo_opxdes_ficha_tecnica`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
),
tipovilla_raw AS (
  SELECT ingestion_dt, run_id, payload AS json
  FROM `{PROJECT}.bronze_raw.etendo_OV_Tipovilla`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
),
location_raw AS (
  SELECT ingestion_dt, run_id, payload AS json
  FROM `{PROJECT}.bronze_raw.etendo_Location`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
),
propietario_raw AS (
  SELECT ingestion_dt, run_id, payload AS json
  FROM `{PROJECT}.bronze_raw.etendo_OV_Propietario`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
),
enriched AS (
  SELECT
    LAX_STRING(r.json.id) AS villa_id,
    LAX_STRING(r.json.name) AS nombre,
    COALESCE(
      SAFE_CAST(LAX_BOOL(r.json.ovVisible) AS BOOL),
      SAFE_CAST(LAX_BOOL(f.json.ovVisible) AS BOOL),
      FALSE
    ) AS es_visible,
    LAX_STRING(f.json.pueblocercano) AS pueblo_cercano,
    r.ingestion_dt AS dataform_ingestion_dt
  FROM resource_raw r
  LEFT JOIN ficha_raw f
    ON LAX_STRING(f.json.obreResource) = LAX_STRING(r.json.id)
  LEFT JOIN tipovilla_raw tv
    ON LAX_STRING(tv.json.id) = LAX_STRING(f.json.tipovilla)
  LEFT JOIN location_raw loc
    ON LAX_STRING(loc.json.id) = LAX_STRING(f.json.location)
  LEFT JOIN propietario_raw prop
    ON LAX_STRING(prop.json.opxdesFicha) = LAX_STRING(f.json.id)
),
final AS (
  SELECT * EXCEPT(row_num)
  FROM (
    SELECT
      enriched.*,
      ROW_NUMBER() OVER (
        PARTITION BY villa_id
        ORDER BY es_visible DESC, IF(pueblo_cercano IS NULL, 1, 0), dataform_ingestion_dt DESC
      ) AS row_num
    FROM enriched
  )
  WHERE row_num = 1
)
SELECT
  (SELECT COUNT(*) FROM `{PROJECT}.silver_clean.stg_etendo_Villa`) AS silver_actual,
  (SELECT COUNT(*) FROM final) AS fixed_rows,
  (SELECT COUNT(DISTINCT villa_id) FROM `{PROJECT}.silver_clean.stg_etendo_Villa`) AS silver_unique,
  (SELECT COUNT(DISTINCT villa_id) FROM final) AS fixed_unique,
  (SELECT COUNT(*) FROM final WHERE nombre = 'ZEBRA') AS zebra_fixed
"""

c = bigquery.Client(project=PROJECT, location=LOCATION)
print(dict(list(c.query(SQL, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP)).result())[0]))
