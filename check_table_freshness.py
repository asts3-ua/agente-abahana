"""Check if silver tables were recently rebuilt by Dataform."""
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
tables = [
    "stg_etendo_Villa",
    "stg_etendo_Proveedor",
    "stg_etendo_Tercero",
    "stg_etendo_Reserva",
    "stg_etendo_Tarifa",
    "stg_etendo_TipoTarifa",
    "stg_etendo_TipoProducto",
    "stg_etendo_TipoUnidad",
    "stg_etendo_TipoEstancia",
    "stg_etendo_OBRE_Resource",
]
for t in tables:
    tbl = c.get_table(f"abahanaweb.silver_clean.{t}")
    sql = f"""
    SELECT
      MAX(dataform_ingestion_dt) AS max_ingestion,
      MAX(dataform_run_id) AS sample_run
    FROM `abahanaweb.silver_clean.{t}`
    """
    try:
        row = list(c.query(sql).result())[0]
        print(
            t,
            f"rows={tbl.num_rows}",
            f"modified={tbl.modified}",
            f"max_ingestion={row.max_ingestion}",
        )
    except Exception as e:
        print(t, "no ingestion cols", tbl.num_rows, tbl.modified, e)
