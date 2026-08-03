from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
SELECT DISTINCT LAX_STRING(payload.ovTipoestancia) code, COUNT(*) n
FROM abahanaweb.bronze_raw.etendo_Order
GROUP BY 1
ORDER BY n DESC
"""
for row in c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result():
    print(dict(row))
