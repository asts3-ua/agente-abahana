import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
sql = """
SELECT
  LAX_STRING(payload.documentStatus) AS doc_status,
  LAX_STRING(payload.ovjchEstadoreserva) AS est_reserva,
  COUNT(*) n
FROM abahanaweb.bronze_raw.etendo_Order
GROUP BY 1,2
ORDER BY n DESC
LIMIT 15
"""
for row in c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result():
    print(dict(row))
