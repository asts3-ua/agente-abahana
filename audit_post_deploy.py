"""Post-deploy checks for all changed staging tables."""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 200 * 1024 * 1024
S = f"{PROJECT}.silver_clean"


def q(client, sql):
    return [dict(r) for r in client.query(
        sql,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP),
        location=LOCATION,
    ).result()]


def main():
    c = bigquery.Client(project=PROJECT, location=LOCATION)
    checks = [
        ("Villa totals", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT villa_id) unicos,
              COUNTIF(es_activo AND es_visible) visibles,
              COUNT(DISTINCT IF(es_activo AND es_visible, villa_id, NULL)) visibles_unicas
            FROM `{S}.stg_etendo_Villa`
        """),
        ("Villa dup villa_id", f"""
            SELECT COUNT(*) grupos_dup FROM (
              SELECT villa_id FROM `{S}.stg_etendo_Villa`
              GROUP BY villa_id HAVING COUNT(*) > 1
            )
        """),
        ("ZEBRA count", f"""
            SELECT COUNT(*) n FROM `{S}.stg_etendo_Villa`
            WHERE villa_id = '5BD6BBB7CF0B415C89A3F23B76392F2B'
        """),
        ("Proveedor dup id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT id) unicos FROM `{S}.stg_etendo_Proveedor`
        """),
        ("Tercero dup id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT id) unicos FROM `{S}.stg_etendo_Tercero`
        """),
        ("Tarifa dup tarifa_id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT tarifa_id) unicos FROM `{S}.stg_etendo_Tarifa`
        """),
        ("TipoTarifa dup id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT id) unicos FROM `{S}.stg_etendo_TipoTarifa`
        """),
        ("TipoProducto dup id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT id) unicos FROM `{S}.stg_etendo_TipoProducto`
        """),
        ("TipoUnidad dup id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT id) unicos FROM `{S}.stg_etendo_TipoUnidad`
        """),
        ("TipoEstancia rows", f"SELECT COUNT(*) n FROM `{S}.stg_etendo_TipoEstancia`"),
        ("Reserva dup reserva_id", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT reserva_id) unicos,
              COUNT(DISTINCT localizador) localizadores
            FROM `{S}.stg_etendo_Reserva`
        """),
        ("Reserva dup localizador top", f"""
            SELECT localizador, COUNT(*) n FROM `{S}.stg_etendo_Reserva`
            GROUP BY localizador HAVING COUNT(*) > 1
            ORDER BY n DESC LIMIT 5
        """),
        ("OBRE_Resource dup", f"""
            SELECT COUNT(*) total, COUNT(DISTINCT etendo_raw_id) unicos
            FROM `{S}.stg_etendo_OBRE_Resource`
        """),
        ("Reservas huérfanas villa", f"""
            SELECT COUNT(*) sin_match
            FROM `{S}.stg_etendo_Reserva` r
            LEFT JOIN `{S}.stg_etendo_Villa` v ON r.villa_id = v.villa_id
            WHERE r.villa_id IS NOT NULL AND v.villa_id IS NULL
        """),
        ("Reservas reales con fechas", f"""
            SELECT COUNT(*) n FROM `{S}.stg_etendo_Reserva`
            WHERE estado_reserva = 'Reserva' AND fecha_entrada IS NOT NULL
        """),
    ]
    for label, sql in checks:
        print(f"== {label}")
        try:
            rows = q(c, sql)
            for row in rows:
                print(row)
            if not rows:
                print("(vacío)")
        except Exception as exc:
            print(f"ERROR: {exc}")
        print()


if __name__ == "__main__":
    main()
