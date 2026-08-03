from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
WITH orphan AS (
  SELECT r.reserva_id, r.villa_id, r.villa_nombre, r.localizador
  FROM abahanaweb.silver_clean.stg_etendo_Reserva r
  LEFT JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
  WHERE r.villa_id IS NOT NULL AND v.villa_id IS NULL
  LIMIT 10
), order_raw AS (
  SELECT
    LAX_STRING(payload.id) AS order_id,
    LAX_STRING(payload.opxdesResource) AS resource_id,
    LAX_STRING(payload.opxdesMProduct) AS product_id,
    LAX_STRING(payload.documentNo) AS localizador
  FROM abahanaweb.bronze_raw.etendo_Order
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
)
SELECT o.*
FROM orphan x
JOIN order_raw o ON o.order_id = x.reserva_id
"""
for row in c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result():
    print(dict(row))
