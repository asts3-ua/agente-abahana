# Análisis de tareas pendientes — Agente Abahana

---

## Tarea 1 — Agente con acceso diferenciado por rol

### Qué se pide

Que el agente muestre información distinta (o consulte tablas distintas) dependiendo de quién le esté hablando. Por ejemplo: un cliente externo solo ve el catálogo; un agente de ventas interno también ve datos del booking o del cliente.

### Estado actual

El agente no tiene ningún concepto de rol. El `runner` se inicializa siempre igual en `main.py`, la instrucción del sistema (`INSTRUCTION`) es única, y las cinco herramientas están disponibles para cualquier conversación sin restricción. No hay autenticación ni identificación del usuario más allá del `USER_ID = "cliente_01"` hardcodeado.

### Dónde vive el problema en el código

- **`main.py`**: crea la sesión con `user_id` fijo y lanza el runner siempre con el mismo agente.
- **`agent.py`**: un único `root_agent` con un único `INSTRUCTION` y las mismas herramientas para todos.
- **BigQuery**: las tablas `dim_propiedades` e `int_etendo_bookings` no tienen seguridad a nivel de fila — cualquier cliente BigQuery con permisos al dataset `silver_clean` ve todo.

### Enfoques posibles

#### Opción A — Varios agentes, un runner por rol (recomendada para empezar)

Crear un `agent_cliente`, un `agent_interno` y un `agent_admin` en `agent.py`, cada uno con su `INSTRUCTION` y su subconjunto de herramientas. En `main.py`, al arrancar la sesión se elige qué agente instanciar según el rol del usuario (leído de un parámetro, una variable de entorno, o en el futuro un token de autenticación).

```
rol → main.py elige agente → runner usa ese agente
```

Lo que cambia por rol:
| Rol | Herramientas disponibles | Qué ve |
|---|---|---|
| Cliente externo | `listar_propiedades`, `buscar_propiedades`, `buscar_por_valoracion` | Catálogo limpio, sin datos de booking |
| Agente interno | todo lo anterior + `buscar_propiedades_amenidades`, `obtener_detalle_propiedad` | Amenidades extendidas, ratings, dirección |
| Admin | todo | Todo, incluido lo que se añada en el futuro (precios, cliente) |

Ventaja: simple, sin tocar BigQuery ni ADK. Desventaja: si los roles evolucionan mucho, hay que mantener N agentes en paralelo.

#### Opción B — Un solo agente, rol en el estado de sesión

ADK permite inicializar la sesión con un `state` arbitrario (diccionario). Ese estado es accesible desde las herramientas Python a través del `tool_context`. Las herramientas leen el rol del estado y adaptan la query (p. ej. omiten columnas sensibles o añaden un `WHERE` extra).

```python
session = await session_service.create_session(
    app_name=APP_NAME,
    user_id=user_id,
    state={"rol": "cliente"},
)
```

Ventaja: un solo agente, la lógica de acceso queda centralizada en cada herramienta. Desventaja: más complejo; hay que propagar el contexto a cada función.

#### Opción C — Row-Level Security en BigQuery (largo plazo)

Crear vistas en BigQuery con los campos apropiados por rol y asignar permisos IAM a distintas service accounts. El agente usaría una service account diferente según el rol. Es la solución más robusta y segura a largo plazo, pero requiere trabajo en el lado de infraestructura GCP.

### Recomendación de secuencia

1. Corto plazo: Opción A — varios agentes, elegir en `main.py`. Sin tocar infraestructura.
2. Medio plazo: Opción B — si la lógica por rol se vuelve muy granular.
3. Largo plazo: Opción C como capa de seguridad real si el agente va a producción con usuarios reales.

---

## Tarea 2 — ¿Qué información usa el agente: descripción libre o capa silver?

### Qué se pregunta

Si el agente, cuando busca villas, se apoya en un campo de texto libre con la descripción narrativa de cada villa (como tendría una web de alquiler: *"Villa con vistas al mar, jardín con palmeras y terraza acristalada"*), o si usa exclusivamente los campos estructurados de la capa silver de BigQuery.

### Contexto: qué es la "capa silver"

El data warehouse de Abahana sigue una arquitectura medallón:

```
Etendo (ERP) → Bronze (raw) → Silver (limpio y estructurado) → Gold (agregado)
```

La capa `silver_clean` contiene datos ya procesados y normalizados desde Etendo. Los campos son estructurados: booleanos, enteros, coordenadas, categorías. **No hay campo de descripción narrativa libre.**

### Estado actual en la rama `develop`

El agente usa **exclusivamente la capa silver**. Concretamente:

**`dim_propiedades`** — campos que el agente puede consultar:
- Booleanos: `tiene_piscina_privada`, `tiene_internet`
- Numéricos: `capacidad_pax`, `total_camas`, `total_banos`, `metros_parcela`, `metros_habitables`
- Texto estructurado: `nombre`, `tipo_villa_descripcion` (categoría de tipo de villa, no una descripción narrativa), `pueblo_cercano`, `zona_nombre`, `region_nombre`

**`int_etendo_bookings`** — añade:
- Booleanos: `tiene_aire_salon`, `tiene_lavadora`, `tiene_lavavajillas`, `admite_animales`
- Numéricos: `score_rating_banos`, `score_rating_cocina`, `score_rating_interior`, `score_rating_exterior`

El único campo de búsqueda libre que hay ahora mismo es el parámetro `texto` de `buscar_propiedades`, que hace un `LIKE` sobre `nombre` y `tipo_villa_descripcion`. Este campo **no es una descripción narrativa** — es una categoría corta como *"Villa", "Apartamento", "Chalet"*.

### El gap concreto

Si un usuario pregunta:
- *"¿Hay villas con chimenea?"* → No hay campo `tiene_chimenea`. El agente no puede responder aunque Etendo lo sepa.
- *"¿Villas con vistas al mar?"* → No hay campo `vistas_mar`. El agente responderá que no tiene esa información.
- *"¿Qué villas tienen jacuzzi?"* → Ídem. No está en silver.
- *"¿Villas con barbacoa?"* → Ídem.

Estas características pueden existir en Etendo como texto en una descripción o como campos propios del ERP que simplemente no se han llevado a la capa silver todavía.

### Comparación con la rama `main` (antes del cambio del compañero)

En `main`, el agente usaba una tabla de prueba `abahana.villas` que sí tenía un campo `descripcion` TEXT con texto libre. La función `buscar_villa` hacía `LIKE` sobre ese campo. Pero eran **datos ficticios** creados manualmente — no datos reales de Etendo.

| | `main` (ficticio) | `develop` (datos reales) |
|---|---|---|
| Fuente | Tabla manual con 5 villas | `silver_clean` con 891 propiedades reales |
| Descripción libre | Sí, campo `descripcion` | No existe |
| Amenidades booleanas | Solo `piscina`, `aire_acondicionado` | 6 booleanos + ratings |
| Precios | Sí (`precio_noche`) | No disponible en silver |

### Conclusión

El agente actual **no usa descripción libre** — ni puede, porque no existe ese campo en `silver_clean`. El agente solo puede responder preguntas que mapeen a uno de los ~10 campos estructurados disponibles. Preguntas sobre características no modeladas como columnas booleanas (jacuzzi, chimenea, vistas, barbacoa, garaje, etc.) quedan fuera de su alcance.

### Posibles vías para resolverlo

#### Opción A — Añadir más campos booleanos a silver

Si Etendo ya tiene esa información estructurada (campos de check en el ERP), se añaden esas columnas al pipeline de transformación de silver. Es la solución más limpia: el agente filtra igual que ahora, sin cambios en el código del agente.

#### Opción B — Incorporar un campo de descripción narrativa a silver

Si Etendo tiene un campo de texto libre por propiedad (descripción web, notas internas), ese texto se puede añadir como columna `descripcion` a `dim_propiedades`. El agente ya tiene el parámetro `texto` con búsqueda `LIKE` — se ampliaría para buscar también en `descripcion`. Permite encontrar características no modeladas como booleanos.

Limitación: `LIKE '%jacuzzi%'` es frágil (mayúsculas, sinónimos, errores tipográficos). Para hacerlo bien se necesitaría búsqueda semántica (embeddings), lo que es un salto de complejidad importante.

#### Opción C — Búsqueda semántica con embeddings (largo plazo)

Generar embeddings de las descripciones de cada villa y almacenarlos en BigQuery o un vector store. Cuando el usuario escribe "vistas al mar con jardín", se convierte a embedding y se busca por similitud. El agente recibiría una herramienta `buscar_semantico(texto)` que internamente hace la búsqueda vectorial.

Es la solución más potente pero requiere infraestructura adicional (Vertex AI Matching Engine o AlloyDB con pgvector).

### Recomendación

Investigar qué campos existen en Etendo que todavía no están en silver. Si hay más booleanos (jacuzzi, chimenea, garaje, barbacoa…), añadirlos al pipeline — es la ganancia más rápida. Si hay descripción narrativa, añadirla como columna y ampliar el `LIKE`. Dejar embeddings para cuando haya suficiente masa crítica de preguntas sin respuesta.
