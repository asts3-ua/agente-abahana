"""Deep-dive follow-up for silver_clean duplicates."""
from google.cloud import bigquery

client = bigquery.Client(project="abahanaweb", location="EU")
cap = 100 * 1024 * 1024
V = "`abahanaweb.silver_clean.stg_etendo_Villa`"
R = "`abahanaweb.silver_clean.stg_etendo_Reserva`"

queries = [
    (
        "Columnas que varían en duplicados villa_id",
        f"""
        WITH dup AS (
          SELECT villa_id FROM {V} GROUP BY villa_id HAVING COUNT(*) > 1
        )
        SELECT villa_id, COUNT(*) AS n,
          COUNT(DISTINCT tipovilla_id) AS tipos,
          COUNT(DISTINCT propietario_id) AS props,
          COUNT(DISTINCT pueblo_cercano) AS pueblos,
          COUNT(DISTINCT CAST(es_visible AS STRING)) AS visibles,
          COUNT(DISTINCT CAST(capacidad_pax AS STRING)) AS caps
        FROM {V}
        WHERE villa_id IN (SELECT villa_id FROM dup)
        GROUP BY villa_id
        ORDER BY n DESC
        LIMIT 10
        """,
    ),
    (
        "Villas únicas visibles (filtro del agente)",
        f"""
        SELECT COUNT(*) AS filas, COUNT(DISTINCT villa_id) AS villas_unicas
        FROM {V}
        WHERE es_activo = TRUE AND es_visible = TRUE
        """,
    ),
    (
        "Filas exactamente idénticas en Villa",
        f"""
        SELECT
          COUNT(*) AS total,
          COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(t)) AS duplicados_exactos
        FROM {V} t
        """,
    ),
    (
        "Muestra ZEBRA — qué difiere entre filas",
        f"""
        SELECT villa_id, nombre, tipovilla_id, propietario_id, es_visible,
               capacidad_pax, pueblo_cercano, rating_exterior
        FROM {V}
        WHERE nombre = 'ZEBRA'
        ORDER BY tipovilla_id
        LIMIT 20
        """,
    ),
    (
        "Reservas dup localizador — reserva_id distintos?",
        f"""
        SELECT localizador, COUNT(*) AS n,
          COUNT(DISTINCT reserva_id) AS reservas,
          COUNT(DISTINCT villa_id) AS villas,
          COUNT(DISTINCT CAST(fecha_entrada AS STRING)) AS fechas
        FROM {R}
        WHERE localizador IN ('00000201', '00000202', 'plantillaVencimientos')
        GROUP BY localizador
        """,
    ),
    (
        "Reservas huérfanas — desglose",
        f"""
        SELECT
          COUNTIF(v.villa_id IS NULL) AS sin_match,
          COUNTIF(v.villa_id IS NULL AND r.villa_nombre IS NOT NULL) AS con_nombre,
          COUNTIF(v.villa_id IS NULL AND r.villa_id IS NULL) AS sin_villa_id
        FROM {R} r
        LEFT JOIN {V} v ON r.villa_id = v.villa_id
        """,
    ),
    (
        "Huérfanas: join por nombre en lugar de villa_id",
        f"""
        SELECT COUNT(*) AS match_por_nombre
        FROM {R} r
        LEFT JOIN {V} v ON r.villa_id = v.villa_id
        INNER JOIN {V} v2 ON LOWER(r.villa_nombre) = LOWER(v2.nombre)
        WHERE v.villa_id IS NULL AND r.villa_nombre IS NOT NULL
        """,
    ),
    (
        "Estado_reserva NULL con Completada — sin fechas?",
        f"""
        SELECT
          COUNTIF(fecha_entrada IS NULL) AS sin_entrada,
          COUNTIF(fecha_salida IS NULL) AS sin_salida
        FROM {R}
        WHERE estado_reserva IS NULL AND estado_documento = 'Completada'
        """,
    ),
]

for label, sql in queries:
    print("=" * 60)
    print(label)
    print("=" * 60)
    try:
        rows = list(
            client.query(
                sql,
                job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap),
            ).result()
        )
        for row in rows:
            print(dict(row))
        if not rows:
            print("(vacío)")
    except Exception as exc:
        print(f"ERROR: {exc}")
    print()
