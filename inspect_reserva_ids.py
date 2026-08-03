from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
WITH sample AS (
  SELECT r.reserva_id, r.villa_id, r.villa_nombre
  FROM abahanaweb.silver_clean.stg_etendo_Reserva r
  JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
  WHERE r.estado_reserva = 'Reserva' AND r.fecha_entrada IS NOT NULL
  LIMIT 5
), order_raw AS (
  SELECT
    LAX_STRING(payload.id) AS order_id,
    LAX_STRING(payload.opxdesResource) AS resource_id,
    LAX_STRING(payload.opxdesMProduct) AS product_id
  FROM abahanaweb.bronze_raw.etendo_Order
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
), resource AS (
  SELECT
    LAX_STRING(payload.id) AS resource_id,
    LAX_STRING(payload.product) AS product_id
  FROM abahanaweb.bronze_raw.etendo_OBRE_Resource
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
)
SELECT
  s.reserva_id,
  s.villa_nombre,
  s.villa_id,
  o.resource_id,
  o.product_id,
  res.resource_id AS res_from_product
FROM sample s
JOIN order_raw o ON o.order_id = s.reserva_id
LEFT JOIN resource res ON res.product_id = o.product_id
"""
for row in c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result():
    print(dict(row))
