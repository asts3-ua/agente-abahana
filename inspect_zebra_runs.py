from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT dataform_run_id, dataform_ingestion_dt, es_visible, pueblo_cercano, COUNT(*) n
FROM abahanaweb.silver_clean.stg_etendo_Villa
WHERE villa_id = '5BD6BBB7CF0B415C89A3F23B76392F2B'
GROUP BY 1,2,3,4
ORDER BY dataform_ingestion_dt DESC, n DESC
"""
for row in c.query(sql).result():
    print(dict(row))

sql2 = """
SELECT dataform_run_id, COUNT(*) n,
  COUNT(DISTINCT CONCAT(CAST(es_visible AS STRING), '|', IFNULL(pueblo_cercano,''))) combos
FROM abahanaweb.silver_clean.stg_etendo_Villa
WHERE villa_id = '5BD6BBB7CF0B415C89A3F23B76392F2B'
GROUP BY 1
ORDER BY dataform_run_id DESC
"""
print('per run_id:')
for row in c.query(sql2).result():
    print(dict(row))
