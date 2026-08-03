from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
WITH matched AS (
  SELECT r.villa_id, r.reserva_id
  FROM abahanaweb.silver_clean.stg_etendo_Reserva r
  JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
  LIMIT 50
), order_raw AS (
  SELECT
    LAX_STRING(payload.id) AS order_id,
    LAX_STRING(payload.opxdesResource) AS opxdes_resource,
    LAX_STRING(payload.opxdesMProduct) AS opxdes_mproduct
  FROM abahanaweb.bronze_raw.etendo_Order
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
)
SELECT
  COUNTIF(o.opxdes_resource = m.villa_id) AS villa_eq_resource,
  COUNTIF(o.opxdes_mproduct = m.villa_id) AS villa_eq_product,
  COUNT(*) AS n
FROM matched m
JOIN order_raw o ON o.order_id = m.reserva_id
"""
print("matched mapping", list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result()))

sql2 = """
WITH orphan AS (
  SELECT r.reserva_id, r.villa_id
  FROM abahanaweb.silver_clean.stg_etendo_Reserva r
  LEFT JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
  WHERE r.villa_id IS NOT NULL AND v.villa_id IS NULL
  LIMIT 50
), order_raw AS (
  SELECT
    LAX_STRING(payload.id) AS order_id,
    LAX_STRING(payload.opxdesResource) AS opxdes_resource,
    LAX_STRING(payload.opxdesMProduct) AS opxdes_mproduct
  FROM abahanaweb.bronze_raw.etendo_Order
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LAX_STRING(payload.id)
    ORDER BY ingestion_dt DESC, run_id DESC
  ) = 1
)
SELECT
  COUNTIF(o.opxdes_resource IS NOT NULL) AS has_resource,
  COUNTIF(o.opxdes_mproduct IS NOT NULL) AS has_product,
  COUNTIF(EXISTS (
    SELECT 1 FROM abahanaweb.silver_clean.stg_etendo_Villa v
    WHERE v.villa_id = o.opxdes_resource
  )) AS resource_in_villa,
  COUNTIF(EXISTS (
    SELECT 1 FROM abahanaweb.silver_clean.stg_etendo_Villa v
    WHERE v.villa_id = o.opxdes_mproduct
  )) AS product_in_villa,
  COUNT(*) AS n
FROM orphan x
JOIN order_raw o ON o.order_id = x.reserva_id
"""
print("orphan mapping", list(c.query(sql2, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result()))
