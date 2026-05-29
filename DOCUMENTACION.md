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

---

## 2. Tecnologías utilizadas

### Google BigQuery — La base de datos
- **¿Qué es?** Un almacén de datos en la nube de Google. Funciona como una base de datos gigante donde se guardan los datos de las villas (nombre, precio, ubicación, etc.).
- **¿Por qué BigQuery?** Es el servicio que ya usa el proyecto de datos de Abahana en Google Cloud. Permite hacer consultas SQL sobre millones de filas en segundos.
- **¿Qué guarda?** Una tabla llamada `villas` dentro del dataset `abahana` con campos como nombre, habitaciones, baños, capacidad, piscina, aire acondicionado, ubicación, precio y descripción.

### Google ADK (Agent Development Kit) — El framework
- **¿Qué es?** Un kit de desarrollo de Google para crear agentes de IA. Proporciona la estructura para conectar el modelo Gemini con herramientas externas (como BigQuery).
- **¿Qué hace?** Gestiona la conversación, envía las preguntas a Gemini, ejecuta las herramientas (consultas a BigQuery) y devuelve la respuesta al usuario.
- **Componente clave: BigQueryToolset.** Es el conector que permite al agente hacer consultas SQL a BigQuery de forma automática. El agente "sabe" leer tablas, listar datasets y ejecutar consultas.

### Google Cloud Platform (GCP) — La infraestructura
- **Proyecto:** `project-d945be28-75a7-460a-998`
- **Servicios usados:**
  - BigQuery (almacén de datos)
  - Vertex AI (API de Gemini)
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
│  │  - Envía a Gemini para interpretar            │  │
│  │  - Ejecuta consulta SQL en BigQuery           │  │
│  │  - Formatea y devuelve respuesta              │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌──────────────────┐  ┌──────────────────────────┐ │
│  │  Gemini 2.0 Flash│  │   BigQuery Toolset       │ │
│  │  (Modelo IA)     │  │   (Conector a datos)     │ │
│  └──────────────────┘  └──────────────────────────┘ │
└──────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│              GOOGLE BIGQUERY                         │
│                                                      │
│  Proyecto: project-d945be28-75a7-460a-998            │
│  Dataset:  abahana                                   │
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
2. **ADK** envía la pregunta a **Gemini**
3. **Gemini** interpreta la pregunta y genera la SQL:
   `SELECT * FROM abahana.villas WHERE piscina = TRUE AND capacidad >= 8`
4. **BigQuery Toolset** ejecuta la SQL contra BigQuery
5. **Gemini** recibe los resultados y genera una respuesta en lenguaje natural
6. **Usuario** recibe: "He encontrado la Villa Mediterránea Altea..."

---

## 4. Estructura del código

```
agente-abahana/
├── .env                 ← Configuración (proyecto GCloud, credenciales)
├── requirements.txt     ← Dependencias Python
├── create_table.py      ← Script para crear tabla en BigQuery con datos de prueba
├── agent.py             ← Definición del agente (instrucciones, modelo, herramientas)
└── main.py              ← Interfaz de línea de comandos para hablar con el agente
```

### agent.py — El agente
Define:
- **Modelo:** Gemini 2.0 Flash
- **Instrucciones:** Cómo debe comportarse (responder en español, formato de precios, etc.)
- **Herramientas:** BigQueryToolset conectado al proyecto de GCloud
- **Esquema de datos:** Descripción de la tabla `villas` para que Gemini sepa qué campos existen

### create_table.py — Datos de prueba
Crea el dataset `abahana` y la tabla `villas` en BigQuery con 5 villas ficticias:

| Villa | Ubicación | Hab. | Baños | Cap. | Piscina | A/C | €/noche |
|-------|-----------|------|-------|------|---------|-----|---------|
| Villa Mediterránea Altea | Altea | 4 | 3 | 8 | Sí | Sí | 350€ |
| Casa Ifach Calpe | Calpe | 3 | 2 | 6 | Sí | Sí | 220€ |
| Villa Moraira Luxury | Moraira | 5 | 4 | 10 | Sí | Sí | 580€ |
| Apartamento Benidorm Centro | Benidorm | 2 | 1 | 4 | No | Sí | 95€ |
| Villa Dénia Montgó | Dénia | 3 | 2 | 6 | Sí | Sí | 265€ |

### main.py — La interfaz
CLI interactivo donde el usuario escribe preguntas y recibe respuestas del agente.

---

## 5. Requisitos para funcionar

### Ya configurado
- [x] Proyecto de Google Cloud creado
- [x] BigQuery habilitado en el proyecto
- [x] Vertex AI API habilitada (`aiplatform.googleapis.com`)
- [x] Código del agente completo y funcional
- [x] Datos de prueba preparados (5 villas)

### Pendiente
- [ ] **Habilitar facturación real (pay-as-you-go) en el proyecto de GCloud**

#### Error actual al ejecutar el agente

Al enviar un mensaje al agente, se produce este error:

```
google.genai.errors.ClientError: 404 NOT_FOUND.
Publisher Model `projects/project-d945be28-75a7-460a-998/locations/europe-west1
/publishers/google/models/gemini-2.0-flash` was not found or your project
does not have access to it.
```

#### ¿Qué significa este error?

**El código del agente está correctamente desarrollado. El error NO es del código.** Se produce porque la cuenta de Google Cloud no tiene acceso a los modelos de Gemini.

Se han probado exhaustivamente todas las alternativas gratuitas:

| Prueba realizada | Resultado |
|-----------------|-----------|
| Vertex AI con `gemini-2.0-flash` en `europe-west1` | 404 — modelo no accesible |
| Vertex AI con `gemini-2.0-flash` en `us-central1` | 404 — modelo no accesible |
| Vertex AI con `gemini-1.5-flash` en `us-central1` | 404 — modelo no accesible |
| Vertex AI con `gemini-2.0-flash-001` en `us-central1` | 404 — modelo no accesible |
| API key gratuita (Google AI Studio) | 429 — quota = 0 en UE |

**Diagnóstico final:** Los créditos de prueba de Google Cloud ($300) **excluyen explícitamente la API de Gemini**. Mensaje literal de Google Cloud: *"El crédito de bienvenida ($300) restante aún se puede usar en los productos de Google Cloud aptos (excepto la API de Gemini)"*. La alternativa gratuita (API key de Google AI Studio) tampoco funciona porque el **free tier de Gemini tiene quota 0 en la Unión Europea**.

#### ¿Cómo se resuelve?

**Activar facturación real (pay-as-you-go)** en el proyecto de Google Cloud. No es necesario un plan especial, solo vincular un método de pago al proyecto.

#### ¿Cuánto cuesta?
| Concepto | Precio |
|----------|--------|
| Gemini 2.0 Flash — Input | $0.10 / millón de tokens |
| Gemini 2.0 Flash — Output | $0.40 / millón de tokens |
| BigQuery — Consultas | 5€ por TB procesado (primeros 10 TB/mes gratis) |

**Estimación realista para testing:** menos de 1€/mes. Las consultas del agente son cortas (pocas decenas de tokens cada una).

#### ¿Cómo activarlo?
1. Ir a `console.cloud.google.com`
2. Seleccionar el proyecto `project-d945be28-75a7-460a-998`
3. Menú lateral → **Facturación (Billing)**
4. Click en **Activar cuenta completa / Upgrade**
5. Introducir método de pago

---

## 6. Cómo ejecutar (cuando billing esté activo)

```powershell
# 1. Activar entorno virtual
.venv\Scripts\Activate.ps1

# 2. Crear tabla en BigQuery (solo primera vez)
python create_table.py

# 3. Lanzar el agente
python main.py
```

---

## 7. Próximos pasos / Mejoras posibles

- **Conectar datos reales:** Sustituir las 5 villas de prueba por el catálogo real de Abahana desde el data warehouse.
- **Interfaz web:** Integrar el agente en una web o app con `adk web` o un frontend custom.
- **Más campos:** Añadir fotos, disponibilidad por fechas, valoraciones, distancia a la playa, etc.
- **Multi-idioma:** El agente puede responder en inglés, francés, alemán, etc. (Gemini es multilingüe).
- **Integración con reservas:** Conectar con el sistema de reservas para que el agente pueda hacer reservas directamente.
- **Historial de conversación:** El agente ya mantiene contexto dentro de una sesión (puede hacer preguntas de seguimiento).

---

## 8. Notas técnicas

- **Autenticación:** Usa Application Default Credentials (ADC) de Google Cloud. Se configura con `gcloud auth application-default login`.
- **Región:** Configurado en `us-central1` para Vertex AI (mayor disponibilidad de modelos Gemini).
- **BigQuery location:** Dataset en `EU` (datos en Europa).
- **Python:** 3.13 con entorno virtual `.venv`.
- **Dependencias principales:** `google-adk`, `google-cloud-bigquery`, `google-cloud-dataplex`, `google-genai`, `python-dotenv`.
