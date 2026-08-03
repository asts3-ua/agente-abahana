from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = "SELECT * FROM abahanaweb.silver_clean.stg_etendo_Tercero WHERE es_propietario = TRUE LIMIT 2"
for row in c.query(sql).result():
    print(dict(row))
