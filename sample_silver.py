from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
queries = {
    "TipoTarifa": "SELECT id, nombre, tipo, descuento FROM abahanaweb.silver_clean.stg_etendo_TipoTarifa LIMIT 3",
    "Tarifa": "SELECT tarifa_id, nombre, villa_id, es_tarifa_venta FROM abahanaweb.silver_clean.stg_etendo_Tarifa LIMIT 3",
    "TipoEstancia": "SELECT * FROM abahanaweb.silver_clean.stg_etendo_TipoEstancia",
    "TipoProducto": "SELECT * FROM abahanaweb.silver_clean.stg_etendo_TipoProducto LIMIT 3",
    "TipoUnidad": "SELECT * FROM abahanaweb.silver_clean.stg_etendo_TipoUnidad LIMIT 3",
    "Reserva": "SELECT reserva_id, localizador, villa_id, villa_nombre, estado_reserva, fecha_entrada FROM abahanaweb.silver_clean.stg_etendo_Reserva WHERE villa_id IS NOT NULL LIMIT 3",
    "OBRE_Resource": "SELECT etendo_raw_id, recurso_nombre, opx_poblacion, canal_ov_visible FROM abahanaweb.silver_clean.stg_etendo_OBRE_Resource WHERE etendo_raw_id='5BD6BBB7CF0B415C89A3F23B76392F2B'",
}
for name, sql in queries.items():
    print("==", name)
    for row in c.query(sql).result():
        print(dict(row))
