# Agente IA — Abahana Villas
## Asistente virtual para consultas de villas vacacionales

---

## 1. ¿Qué es este proyecto?

Un **asistente virtual inteligente** que responde preguntas en lenguaje natural sobre las villas de alquiler vacacional de Abahana Villas. El usuario escribe preguntas como si hablara con una persona, y el agente consulta la base de datos y responde con información real.

### Ejemplos de uso

```
Usuario: "¿Qué villas tienen piscina en Calpe?"
Agente:  "He encontrado 1 villa en Calpe con piscina:
          - Casa Ifach Calpe | 3 habitaciones | 6 personas | 220€/noche"

Usuario: "¿Cuál es la villa más barata?"
Agente:  "La villa más económica es el Apartamento Benidorm Centro,
          con un precio de 95€/noche."

Usuario: "Necesito algo para 10 personas con piscina"
Agente:  "La Villa Moraira Luxury es perfecta: 5 habitaciones,
          capacidad para 10 personas, piscina climatizada. 580€/noche."
```

**Estado actual: funcional.** El bot está operativo con el proyecto `abahanaweb` en Google Cloud.

---

## 2. Tecnologías utilizadas

### Google BigQuery — La base de datos
- **¿Qué es?** Almacén de datos en la nube de Google. Guarda los datos de las villas (nombre, precio, ubicación, etc.).
- **¿Por qué BigQuery?** Es el servicio que ya usa el proyecto de datos de Abahana en Google Cloud.
- **¿Qué guarda?** Tabla `villas` dentro del dataset `abahana`, proyecto `abahanaweb`.

### Google ADK (Agent Development Kit) — El framework
- **¿Qué es?** Kit de desarrollo de Google para crear agentes de IA. Conecta el modelo Gemini con herramientas externas.
- **¿Qué hace?** Gestiona la conversación, envía las preguntas a Gemini, ejecuta las herramientas Python y devuelve la respuesta al usuario.
- **Herramientas:** Dos funciones Python propias (ver sección 4) que hacen queries a BigQuery con `google-cloud-bigquery`. No se usa `BigQueryToolset`.

### Google Cloud Platform (GCP) — La infraestructura
- **Proyecto:** `abahanaweb`
- **Servicios usados:**
  - BigQuery (almacén de datos, dataset en región `EU`)
  - Vertex AI (API de Gemini, región `us-central1`)
  - IAM (permisos y autenticación)

---

## 3. Arquitectura del sistema

```
┌─────────────────────────────────────────────────────┐
│                    USUARIO                          │
│         "¿Qué villas tienen piscina?"               │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              GOOGLE ADK (Framework)                 │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │           AGENTE (agent.py)                   │  │
│  │                                               │  │
│  │  - Recibe pregunta del usuario                │  │
│  │  - Gemini decide qué herramienta llamar       │  │
│  │  - Ejecuta la herramienta Python              │  │
│  │  - Gemini formatea y devuelve respuesta       │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌──────────────────┐  ┌──────────────────────────┐ │
│  │ Gemini 2.5 Flash │  │   Herramientas Python    │ │
│  │  (Modelo IA)     │  │   buscar_villa()         │ │
│  │                  │  │   buscar_villas_por_     │ │
│  │                  │  │   caracteristicas()      │ │
│  └──────────────────┘  └──────────────────────────┘ │
└──────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│              GOOGLE BIGQUERY                         │
│                                                      │
│  Proyecto: abahanaweb                                │
│  Dataset:  abahana  (región EU)                      │
│  Tabla:    villas                                    │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ id | nombre | habitaciones | banos | capacidad │  │
│  │ piscina | aire_acondicionado | ubicacion       │  │
│  │ precio_noche | descripcion                     │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Flujo de una consulta

1. **Usuario** escribe: "Busco villa con piscina para 8 personas"
2. **ADK** envía la pregunta a **Gemini 2.5 Flash**
3. **Gemini** decide llamar a `buscar_villas_por_caracteristicas(["piscina"])`
4. La **función Python** ejecuta el SQL parametrizado contra BigQuery
5. **Gemini** recibe los resultados y filtra/formatea la respuesta en lenguaje natural
6. **Usuario** recibe: "He encontrado la Villa Mediterránea Altea..."

---

## 4. Estructura del código

```
agente_abahana/
├── .env                 ← Configuración (proyecto GCloud, credenciales)
├── requirements.txt     ← Dependencias Python
├── create_table.py      ← Script para crear tabla en BigQuery con datos de prueba
├── agent.py             ← Definición del agente, herramientas y conexión a BigQuery
├── main.py              ← Interfaz de línea de comandos (CLI interactivo)
└── agente_villas/
    └── __init__.py      ← Re-exporta root_agent para compatibilidad con ADK
```

### agent.py — El corazón del sistema

Define el agente y las dos herramientas que puede llamar:

#### `buscar_villa(nombre: str)`
Busca villas por nombre usando búsqueda parcial, sin distinguir mayúsculas/minúsculas.

```python
# Ejemplo: buscar_villa("ifach") → encuentra "Casa Ifach Calpe"
# Usa query parametrizada: WHERE LOWER(nombre) LIKE LOWER(@patron)
# Límite: 5 resultados, 10 MB facturados por consulta
```

#### `buscar_villas_por_caracteristicas(caracteristicas: list[str])`
Filtra villas por columnas booleanas de la tabla. Combina múltiples filtros con AND.

```python
# Ejemplo: buscar_villas_por_caracteristicas(["piscina", "aire_acondicionado"])
# Genera: WHERE `piscina` = TRUE AND `aire_acondicionado` = TRUE
# Lee el schema de BigQuery dinámicamente para validar columnas
# Columnas booleanas actuales: piscina, aire_acondicionado
# Límite: 10 resultados, 10 MB facturados por consulta
```

**Seguridad en consultas:**
- Queries parametrizadas (sin interpolación de strings del usuario en SQL)
- `maximum_bytes_billed=10 MB` en cada consulta — evita costes inesperados
- Validación de columnas contra el schema real de BigQuery antes de ejecutar

#### Configuración del agente

```python
root_agent = Agent(
    name="abahana_villas_agent",
    model="gemini-2.5-flash",
    instruction=INSTRUCTION,       # instrucciones en español
    tools=[buscar_villa, buscar_villas_por_caracteristicas],
)
```

### create_table.py — Datos de prueba

Crea el dataset `abahana` y la tabla `villas` en BigQuery con 5 villas ficticias de la Costa Blanca:

| Villa | Ubicación | Hab. | Baños | Cap. | Piscina | A/C | €/noche |
|-------|-----------|------|-------|------|---------|-----|---------|
| Villa Mediterránea Altea | Altea | 4 | 3 | 8 | Sí | Sí | 350€ |
| Casa Ifach Calpe | Calpe | 3 | 2 | 6 | Sí | Sí | 220€ |
| Villa Moraira Luxury | Moraira | 5 | 4 | 10 | Sí | Sí | 580€ |
| Apartamento Benidorm Centro | Benidorm | 2 | 1 | 4 | No | Sí | 95€ |
| Villa Dénia Montgó | Dénia | 3 | 2 | 6 | Sí | Sí | 265€ |

### main.py — La interfaz CLI

CLI interactivo asíncrono. Crea una sesión ADK en memoria, lanza el runner y entra en bucle de input/output hasta que el usuario escribe "salir".

---

## 5. Requisitos para funcionar

### Configurado y operativo
- [x] Proyecto de Google Cloud `abahanaweb` con facturación activa
- [x] BigQuery habilitado — dataset `abahana` creado en región `EU`
- [x] Vertex AI API habilitada (`aiplatform.googleapis.com`)
- [x] Modelo `gemini-2.5-flash` accesible desde el proyecto
- [x] Código del agente completo y funcional
- [x] Datos de prueba insertados (5 villas)

### Coste estimado en producción
| Concepto | Precio |
|----------|--------|
| Gemini 2.5 Flash — Input | ~$0.15 / millón de tokens |
| Gemini 2.5 Flash — Output | ~$0.60 / millón de tokens |
| BigQuery — Consultas | 5€ por TB procesado (primeros 10 TB/mes gratis) |

Estimación realista para testing: menos de 1€/mes. Las consultas del agente son cortas.

---

## 6. Cómo ejecutar

```powershell
# 1. Activar entorno virtual
.venv\Scripts\Activate.ps1

# 2. Autenticarse con Google Cloud (si no está activo)
gcloud auth application-default login

# 3. Crear tabla en BigQuery (solo la primera vez)
python create_table.py

# 4. Lanzar el agente
python main.py
```

También puede lanzarse con el servidor web de ADK:

```powershell
adk web
```

---

## 7. Próximos pasos / Mejoras posibles

- **Conectar datos reales:** Sustituir las 5 villas de prueba por el catálogo real de Abahana desde el data warehouse de Etendo.
- **Más herramientas:** Añadir `buscar_por_capacidad`, `buscar_por_ubicacion`, `buscar_por_rango_precio`.
- **Más campos:** Añadir disponibilidad por fechas, fotos, valoraciones, distancia a la playa.
- **Interfaz web:** Integrar el agente en una web o app con `adk web` o un frontend custom.
- **Multi-idioma:** El agente puede responder en inglés, francés, alemán, etc. (Gemini es multilingüe).
- **Integración con reservas:** Conectar con el sistema de reservas de Etendo para que el agente pueda consultar disponibilidad o iniciar reservas.

---

## 8. Notas técnicas

- **Autenticación:** Application Default Credentials (ADC) de Google Cloud. Configurar con `gcloud auth application-default login`.
- **Región Vertex AI:** `us-central1` (mayor disponibilidad de modelos Gemini).
- **BigQuery location:** Dataset en `EU` (datos en Europa).
- **Python:** 3.13 con entorno virtual `.venv`.
- **Dependencias principales:** `google-adk[bigquery]`, `google-cloud-bigquery`, `google-cloud-dataplex`, `google-genai`, `python-dotenv`.
- **Schema dinámico:** `_columnas_booleanas()` usa `@lru_cache` — lee el schema de BigQuery una sola vez por sesión y lo cachea.
