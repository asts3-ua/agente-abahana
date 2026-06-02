"""
Crea dataset 'abahana' y tabla 'villas' en BigQuery con 5 villas de prueba
de la Costa Blanca. Ejecutar una sola vez antes de usar el agente.

Requisitos: gcloud auth application-default login
"""

from google.cloud import bigquery

PROJECT_ID = "abahana-agent"
DATASET_ID = "abahana"
TABLE_ID = "villas"

client = bigquery.Client(project=PROJECT_ID)

# Dataset
dataset_ref = f"{PROJECT_ID}.{DATASET_ID}"
dataset = bigquery.Dataset(dataset_ref)
dataset.location = "EU"
dataset = client.create_dataset(dataset, exists_ok=True)
print(f"Dataset '{DATASET_ID}' listo.")

# Schema
schema = [
    bigquery.SchemaField("id", "STRING", description="Identificador único de la villa"),
    bigquery.SchemaField("nombre", "STRING", description="Nombre de la villa"),
    bigquery.SchemaField("habitaciones", "INTEGER", description="Número de habitaciones"),
    bigquery.SchemaField("banos", "INTEGER", description="Número de baños"),
    bigquery.SchemaField("capacidad", "INTEGER", description="Capacidad máxima de personas"),
    bigquery.SchemaField("piscina", "BOOL", description="Tiene piscina privada"),
    bigquery.SchemaField("aire_acondicionado", "BOOL", description="Tiene aire acondicionado"),
    bigquery.SchemaField("ubicacion", "STRING", description="Localidad en la Costa Blanca"),
    bigquery.SchemaField("precio_noche", "FLOAT64", description="Precio por noche en euros"),
    bigquery.SchemaField("descripcion", "STRING", description="Descripción de la villa"),
]

table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
table = bigquery.Table(table_ref, schema=schema)
table = client.create_table(table, exists_ok=True)
print(f"Tabla '{TABLE_ID}' lista.")

# 5 villas ficticias de la Costa Blanca
villas = [
    {
        "id": "villa_001",
        "nombre": "Villa Mediterránea Altea",
        "habitaciones": 4,
        "banos": 3,
        "capacidad": 8,
        "piscina": True,
        "aire_acondicionado": True,
        "ubicacion": "Altea",
        "precio_noche": 350.0,
        "descripcion": (
            "Espectacular villa con vistas al mar y al casco histórico de Altea. "
            "Piscina infinity, jardín privado con barbacoa y terraza panorámica."
        ),
    },
    {
        "id": "villa_002",
        "nombre": "Casa Ifach Calpe",
        "habitaciones": 3,
        "banos": 2,
        "capacidad": 6,
        "piscina": True,
        "aire_acondicionado": True,
        "ubicacion": "Calpe",
        "precio_noche": 220.0,
        "descripcion": (
            "Villa moderna a 500m de la playa de la Fossa. Piscina privada con "
            "vistas al Peñón de Ifach, terraza cubierta y parking."
        ),
    },
    {
        "id": "villa_003",
        "nombre": "Villa Moraira Luxury",
        "habitaciones": 5,
        "banos": 4,
        "capacidad": 10,
        "piscina": True,
        "aire_acondicionado": True,
        "ubicacion": "Moraira",
        "precio_noche": 580.0,
        "descripcion": (
            "Villa de lujo en Moraira con piscina climatizada, sala de cine, "
            "jacuzzi exterior y vistas al Mediterráneo. Cocina de chef equipada."
        ),
    },
    {
        "id": "villa_004",
        "nombre": "Apartamento Benidorm Centro",
        "habitaciones": 2,
        "banos": 1,
        "capacidad": 4,
        "piscina": False,
        "aire_acondicionado": True,
        "ubicacion": "Benidorm",
        "precio_noche": 95.0,
        "descripcion": (
            "Apartamento en el corazón de Benidorm, a 100m de la playa de Levante. "
            "Terraza con vistas al mar, piscina comunitaria y acceso a spa del edificio."
        ),
    },
    {
        "id": "villa_005",
        "nombre": "Villa Dénia Montgó",
        "habitaciones": 3,
        "banos": 2,
        "capacidad": 6,
        "piscina": True,
        "aire_acondicionado": True,
        "ubicacion": "Dénia",
        "precio_noche": 265.0,
        "descripcion": (
            "Villa rodeada de naturaleza al pie del macizo del Montgó. Piscina privada, "
            "huerto ecológico, a 10 minutos del puerto y las playas de arena blanca."
        ),
    },
]

job = client.load_table_from_json(villas, table_ref)
job.result()
errors = []
if True:
    print(f"5 villas insertadas correctamente en '{DATASET_ID}.{TABLE_ID}'.")
    print("\nVillas creadas:")
    for v in villas:
        print(
            f"  - {v['nombre']} ({v['ubicacion']}) | "
            f"{v['habitaciones']} hab | {v['capacidad']} pers | "
            f"Piscina: {'Sí' if v['piscina'] else 'No'} | "
            f"{v['precio_noche']}€/noche"
        )
