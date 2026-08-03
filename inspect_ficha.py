from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024

sql = """
WITH ficha AS (
  SELECT
    LAX_STRING(payload.product) AS product_id,
    COUNT(*) AS n
  FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
  GROUP BY 1
  HAVING COUNT(*) > 1
  ORDER BY n DESC
  LIMIT 10
)
SELECT * FROM ficha
"""
print("products with multiple ficha rows", list(c.query(sql).result()))

sql2 = """
SELECT
  LAX_STRING(payload.id) AS villa_id,
  COUNT(*) AS bronze_rows
FROM abahanaweb.bronze_raw.etendo_OBRE_Resource
GROUP BY 1
HAVING COUNT(*) > 1
ORDER BY bronze_rows DESC
LIMIT 10
"""
print("villas with multiple bronze rows", list(c.query(sql2).result()))

sql3 = """
SELECT COUNT(*) AS n
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
JOIN abahanaweb.bronze_raw.etendo_OBRE_Resource r
  ON LAX_STRING(f.payload.product) = LAX_STRING(r.payload.product)
WHERE LAX_STRING(r.payload.id) = '5BD6BBB7CF0B415C89A3F23B76392F2B'
"""
print("ficha join zebra all bronze rows", list(c.query(sql3).result()))

sql4 = """
SELECT COUNT(*) AS n
FROM abahanaweb.bronze_raw.etendo_OV_Propietario p
JOIN abahanaweb.bronze_raw.etendo_Product pr
  ON LAX_STRING(p.payload.opxdesFicha) = LAX_STRING(pr.payload.id)
WHERE LAX_STRING(pr.payload.id) = 'E8FD54F33BB44CA2AE0E58C6DACFAC27'
"""
print("propietario join via ficha", list(c.query(sql4, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result()))
