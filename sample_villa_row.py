from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT *
FROM abahanaweb.silver_clean.stg_etendo_Villa
WHERE nombre = 'PARADISE'
LIMIT 1
"""
row = dict(list(c.query(sql).result())[0])
for k, v in row.items():
    print(f"{k}: {v}")
