from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
WITH orphan AS (
  SELECT DISTINCT r.villa_id
  FROM abahanaweb.silver_clean.stg_etendo_Reserva r
  LEFT JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
  WHERE r.villa_id IS NOT NULL AND v.villa_id IS NULL
  LIMIT 1000
)
SELECT
  COUNT(*) AS orphan_ids,
  COUNTIF(p.payload.id IS NOT NULL) AS match_product,
  COUNTIF(res.payload.id IS NOT NULL) AS match_resource
FROM orphan o
LEFT JOIN abahanaweb.bronze_raw.etendo_Product p
  ON LAX_STRING(p.payload.id) = o.villa_id
LEFT JOIN abahanaweb.bronze_raw.etendo_OBRE_Resource res
  ON LAX_STRING(res.payload.id) = o.villa_id
"""
print(list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result()))

sql2 = """
SELECT r.villa_id, r.villa_nombre,
  MAX(IF(LAX_STRING(res.payload.id) IS NOT NULL, 'resource', NULL)) AS in_resource,
  MAX(IF(LAX_STRING(p.payload.id) IS NOT NULL, 'product', NULL)) AS in_product
FROM abahanaweb.silver_clean.stg_etendo_Reserva r
LEFT JOIN abahanaweb.silver_clean.stg_etendo_Villa v ON r.villa_id = v.villa_id
LEFT JOIN abahanaweb.bronze_raw.etendo_OBRE_Resource res
  ON LAX_STRING(res.payload.id) = r.villa_id
LEFT JOIN abahanaweb.bronze_raw.etendo_Product p
  ON LAX_STRING(p.payload.id) = r.villa_id
WHERE v.villa_id IS NULL AND r.villa_id IS NOT NULL
GROUP BY 1,2
LIMIT 10
"""
print('orphan samples:')
for row in c.query(sql2, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result():
    print(dict(row))
