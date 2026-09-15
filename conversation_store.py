"""
Almacenamiento persistente de conversaciones del asistente.

Guarda cada turno pregunta/respuesta para análisis posterior y mejora continua.
- Producción (GCP): BigQuery (dataset configurable, por defecto agent_analytics.chat_turns)
- Desarrollo local: SQLite en data/conversations.db
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("conversation-store")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
BQ_DATASET = os.environ.get("CONVERSATIONS_DATASET", "agent_analytics")
BQ_TABLE = os.environ.get("CONVERSATIONS_TABLE", "chat_turns")
# El feedback va en su propia tabla y se AÑADE, nunca se actualiza: BigQuery
# rechaza UPDATE sobre filas recién insertadas por streaming ("would affect
# rows in the streaming buffer"), que es justo cuando el usuario valora.
BQ_FEEDBACK_TABLE = os.environ.get("CONVERSATIONS_FEEDBACK_TABLE", "chat_feedback")
SQLITE_PATH = Path(os.environ.get("CONVERSATIONS_SQLITE_PATH", "data/conversations.db"))
BACKEND = os.environ.get("CONVERSATIONS_BACKEND", "auto").lower()

Backend = Literal["bigquery", "sqlite"]

FEEDBACK_LABELS = {"util", "parcial", "no_resolvio"}
FEEDBACK_SCORES = {"util": 3, "parcial": 2, "no_resolvio": 1}
LEGACY_RATINGS = {"util": 1, "parcial": 0, "no_resolvio": -1}

# (nombre, tipo SQLite, tipo BigQuery). Los dos motores NO comparten
# vocabulario: BigQuery rechaza TEXT con "Invalid value for type", así que
# reutilizar los tipos de SQLite impedía crear la tabla de conversaciones.
# feedback_at va como TIMESTAMP porque save_feedback lo escribe así.
_FEEDBACK_COLUMNS: list[tuple[str, str, str]] = [
    ("feedback_score", "INTEGER", "INTEGER"),
    ("feedback_label", "TEXT", "STRING"),
    ("feedback_tags", "TEXT", "STRING"),
    ("feedback_comment", "TEXT", "STRING"),
    ("feedback_at", "TEXT", "TIMESTAMP"),
]


# Última valoración de cada turno. Se resuelve al leer porque el feedback es
# un registro de altas: revalorar añade una fila, no reescribe la anterior.
_ULTIMO_FEEDBACK_SQLITE = """
    ultimo_feedback AS (
        SELECT turn_id, rating, feedback_score, feedback_label,
               feedback_tags, feedback_comment, feedback_at
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY turn_id ORDER BY feedback_at DESC) AS rn
            FROM chat_feedback
        )
        WHERE rn = 1
    )"""

# Ojo: _row_to_feedback_example mapea por posición, así que este bloque debe
# devolver exactamente label, tags, comment y fecha, en ese orden.
_COLUMNAS_FEEDBACK_RESUELTAS = """
    COALESCE(f.feedback_label, t.feedback_label) AS feedback_label,
    COALESCE(f.feedback_tags, t.feedback_tags) AS feedback_tags,
    COALESCE(f.feedback_comment, t.feedback_comment) AS feedback_comment,
    COALESCE(f.feedback_at, t.feedback_at, t.rated_at) AS feedback_at"""


class ConversationStore:
    """Persiste turnos de chat (pregunta + respuesta) de forma durable."""

    def __init__(self) -> None:
        self._backend: Backend | None = None
        self._bq_client = None
        self._bq_table_id: str | None = None
        self._bq_feedback_table_id: str | None = None

    def save_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        user_role: str,
        user_message: str,
        assistant_message: str,
        app_name: str = "abahana_chat",
        response_ms: int | None = None,
        error: str | None = None,
    ) -> str | None:
        """Guarda un turno completo. Devuelve turn_id o None si falla."""
        row = {
            "turn_id": str(uuid.uuid4()),
            "session_id": session_id,
            "user_id": user_id,
            "user_role": user_role,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response_ms": response_ms,
            "error": error,
            "app_name": app_name,
            "rating": None,
            "rated_at": None,
            "feedback_score": None,
            "feedback_label": None,
            "feedback_tags": None,
            "feedback_comment": None,
            "feedback_at": None,
        }
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._save_bigquery(row)
            else:
                self._save_sqlite(row)
            return row["turn_id"]
        except Exception:
            log.exception("No se pudo guardar el turno de conversación")
            return None

    def save_rating(self, turn_id: str, rating: int) -> None:
        """Compatibilidad legacy: 1 = útil, -1 = no útil."""
        if rating not in (1, -1):
            raise ValueError("rating debe ser 1 o -1")
        label = "util" if rating == 1 else "no_resolvio"
        self.save_feedback(turn_id, label=label)

    def save_feedback(
        self,
        turn_id: str,
        *,
        label: str,
        tags: list[str] | None = None,
        comment: str | None = None,
    ) -> None:
        """Guarda feedback estructurado: util | parcial | no_resolvio."""
        if label not in FEEDBACK_LABELS:
            raise ValueError(f"label debe ser uno de {FEEDBACK_LABELS}")

        feedback_at = datetime.now(timezone.utc).isoformat()
        score = FEEDBACK_SCORES[label]
        rating = LEGACY_RATINGS[label]
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        comment_clean = (comment or "").strip() or None

        payload = {
            "rating": rating,
            "rated_at": feedback_at,
            "feedback_score": score,
            "feedback_label": label,
            "feedback_tags": tags_json,
            "feedback_comment": comment_clean,
            "feedback_at": feedback_at,
        }
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._save_feedback_bigquery(turn_id, payload)
            else:
                self._save_feedback_sqlite(turn_id, payload)
        except Exception:
            log.exception("No se pudo guardar el feedback turn_id=%s", turn_id)

    def get_recent_negative_examples(self, limit: int = 8) -> list[dict]:
        """Ejemplos recientes mal valorados (compatibilidad con agente)."""
        return self.get_recent_problematic_examples(limit=limit)

    def get_recent_problematic_examples(self, limit: int = 8) -> list[dict]:
        """Ejemplos recientes con feedback parcial o negativo."""
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                return self._recent_problematic_bigquery(limit)
            return self._recent_problematic_sqlite(limit)
        except Exception:
            log.exception("No se pudo obtener feedback problemático reciente")
            return []

    def list_sessions(self, user_id: str, limit: int = 25) -> list[dict]:
        """Conversaciones del usuario, de la más activa a la más antigua."""
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                return self._list_sessions_bigquery(user_id, limit)
            return self._list_sessions_sqlite(user_id, limit)
        except Exception:
            log.exception("No se pudieron listar las conversaciones")
            return []

    def get_session_turns(self, session_id: str, user_id: str) -> list[dict]:
        """Turnos de una conversación, en orden.

        El user_id va en el WHERE a propósito: sin él bastaría con conocer un
        session_id para leer la conversación de otra persona.
        """
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                return self._session_turns_bigquery(session_id, user_id)
            return self._session_turns_sqlite(session_id, user_id)
        except Exception:
            log.exception("No se pudieron leer los turnos de la conversación")
            return []

    def _resolve_backend(self) -> Backend:
        if self._backend:
            return self._backend

        if BACKEND == "sqlite":
            self._backend = "sqlite"
            return self._backend

        if BACKEND == "bigquery":
            self._init_bigquery()
            self._backend = "bigquery"
            return self._backend

        try:
            self._init_bigquery()
            self._backend = "bigquery"
        except Exception as exc:
            # A nivel error, no warning: en Cloud Run este fallback escribe en
            # el disco del contenedor y el historial se pierde en cada
            # despliegue. En producción se fija CONVERSATIONS_BACKEND=bigquery
            # para que ni siquiera se llegue aquí.
            log.error(
                "BigQuery no disponible (%s). Se usa SQLite local: el historial "
                "NO persistirá si esto corre en un contenedor efímero.", exc
            )
            self._backend = "sqlite"
        return self._backend

    def _init_bigquery(self) -> None:
        if self._bq_client is not None:
            return

        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound

        client = bigquery.Client(project=PROJECT_ID)
        dataset_ref = bigquery.DatasetReference(PROJECT_ID, BQ_DATASET)
        table_ref = dataset_ref.table(BQ_TABLE)

        try:
            client.get_dataset(dataset_ref)
        except NotFound:
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west1")
            client.create_dataset(dataset, exists_ok=True)
            log.info("Dataset BigQuery creado: %s.%s", PROJECT_ID, BQ_DATASET)

        try:
            client.get_table(table_ref)
        except NotFound:
            schema = self._bq_schema_fields(bigquery)
            table = bigquery.Table(table_ref, schema=schema)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="created_at",
            )
            client.create_table(table)
            log.info("Tabla BigQuery creada: %s.%s.%s", PROJECT_ID, BQ_DATASET, BQ_TABLE)
        else:
            self._ensure_bq_columns(client, table_ref)

        feedback_ref = dataset_ref.table(BQ_FEEDBACK_TABLE)
        try:
            client.get_table(feedback_ref)
        except NotFound:
            tabla_fb = bigquery.Table(
                feedback_ref, schema=self._bq_feedback_schema_fields(bigquery)
            )
            tabla_fb.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="feedback_at",
            )
            client.create_table(tabla_fb)
            log.info("Tabla BigQuery creada: %s.%s", BQ_DATASET, BQ_FEEDBACK_TABLE)

        self._bq_client = client
        self._bq_table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
        self._bq_feedback_table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_FEEDBACK_TABLE}"

    @staticmethod
    def _bq_feedback_schema_fields(bigquery) -> list:
        campos = [
            bigquery.SchemaField("feedback_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("rating", "INTEGER"),
        ]
        for name, _, tipo_bq in _FEEDBACK_COLUMNS:
            campos.append(bigquery.SchemaField(name, tipo_bq))
        return campos

    @staticmethod
    def _bq_schema_fields(bigquery) -> list:
        fields = [
            bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("user_role", "STRING"),
            bigquery.SchemaField("user_message", "STRING"),
            bigquery.SchemaField("assistant_message", "STRING"),
            bigquery.SchemaField("created_at", "TIMESTAMP"),
            bigquery.SchemaField("response_ms", "INTEGER"),
            bigquery.SchemaField("error", "STRING"),
            bigquery.SchemaField("app_name", "STRING"),
            bigquery.SchemaField("rating", "INTEGER"),
            bigquery.SchemaField("rated_at", "TIMESTAMP"),
        ]
        for name, _, tipo_bq in _FEEDBACK_COLUMNS:
            fields.append(bigquery.SchemaField(name, tipo_bq))
        return fields

    def _ensure_bq_columns(self, client, table_ref) -> None:
        from google.cloud import bigquery

        table = client.get_table(table_ref)
        names = {f.name for f in table.schema}
        new_fields = []
        if "rating" not in names:
            new_fields.append(bigquery.SchemaField("rating", "INTEGER"))
        if "rated_at" not in names:
            new_fields.append(bigquery.SchemaField("rated_at", "TIMESTAMP"))
        for name, _, tipo_bq in _FEEDBACK_COLUMNS:
            if name not in names:
                new_fields.append(bigquery.SchemaField(name, tipo_bq))
        if new_fields:
            table.schema = list(table.schema) + new_fields
            client.update_table(table, ["schema"])
            log.info("Columnas de feedback añadidas a %s.%s", BQ_DATASET, BQ_TABLE)

    def _save_bigquery(self, row: dict) -> None:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None

        bq_row = dict(row)
        bq_row["created_at"] = row["created_at"].replace("+00:00", "Z")

        errors = self._bq_client.insert_rows_json(self._bq_table_id, [bq_row])
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")

    def _sqlite_create_table_sql(self) -> str:
        feedback_cols = ", ".join(
            f"{name} {tipo_sqlite}" for name, tipo_sqlite, _ in _FEEDBACK_COLUMNS
        )
        return f"""
            CREATE TABLE IF NOT EXISTS chat_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_role TEXT,
                user_message TEXT,
                assistant_message TEXT,
                created_at TEXT NOT NULL,
                response_ms INTEGER,
                error TEXT,
                app_name TEXT,
                rating INTEGER,
                rated_at TEXT,
                {feedback_cols}
            )
        """

    @staticmethod
    def _sqlite_feedback_table_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS chat_feedback (
                feedback_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                rating INTEGER,
                feedback_score INTEGER,
                feedback_label TEXT,
                feedback_tags TEXT,
                feedback_comment TEXT,
                feedback_at TEXT NOT NULL
            )
        """

    def _ensure_sqlite_columns(self, conn: sqlite3.Connection) -> None:
        conn.execute(self._sqlite_feedback_table_sql())
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_turn "
            "ON chat_feedback (turn_id, feedback_at DESC)"
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_turns)")}
        if "rating" not in cols:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN rating INTEGER")
        if "rated_at" not in cols:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN rated_at TEXT")
        for name, tipo_sqlite, _ in _FEEDBACK_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE chat_turns ADD COLUMN {name} {tipo_sqlite}")

    def _save_sqlite(self, row: dict) -> None:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(self._sqlite_create_table_sql())
            self._ensure_sqlite_columns(conn)
            conn.execute(
                """
                INSERT INTO chat_turns (
                    turn_id, session_id, user_id, user_role,
                    user_message, assistant_message, created_at,
                    response_ms, error, app_name, rating, rated_at,
                    feedback_score, feedback_label, feedback_tags,
                    feedback_comment, feedback_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["turn_id"],
                    row["session_id"],
                    row["user_id"],
                    row["user_role"],
                    row["user_message"],
                    row["assistant_message"],
                    row["created_at"],
                    row["response_ms"],
                    row["error"],
                    row["app_name"],
                    row.get("rating"),
                    row.get("rated_at"),
                    row.get("feedback_score"),
                    row.get("feedback_label"),
                    row.get("feedback_tags"),
                    row.get("feedback_comment"),
                    row.get("feedback_at"),
                ),
            )
            conn.commit()

    def _save_feedback_sqlite(self, turn_id: str, payload: dict[str, Any]) -> None:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(self._sqlite_create_table_sql())
            self._ensure_sqlite_columns(conn)
            conn.execute(
                """
                INSERT INTO chat_feedback (
                    feedback_id, turn_id, rating, feedback_score,
                    feedback_label, feedback_tags, feedback_comment, feedback_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    turn_id,
                    payload["rating"],
                    payload["feedback_score"],
                    payload["feedback_label"],
                    payload["feedback_tags"],
                    payload["feedback_comment"],
                    payload["feedback_at"],
                ),
            )
            conn.commit()

    def _save_feedback_bigquery(self, turn_id: str, payload: dict[str, Any]) -> None:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_feedback_table_id is not None

        fila = {
            "feedback_id": str(uuid.uuid4()),
            "turn_id": turn_id,
            "rating": payload["rating"],
            "feedback_score": payload["feedback_score"],
            "feedback_label": payload["feedback_label"],
            "feedback_tags": payload["feedback_tags"],
            "feedback_comment": payload["feedback_comment"],
            "feedback_at": payload["feedback_at"].replace("+00:00", "Z"),
        }
        errors = self._bq_client.insert_rows_json(self._bq_feedback_table_id, [fila])
        if errors:
            raise RuntimeError(f"BigQuery insert errors (feedback): {errors}")

    def _recent_problematic_sqlite(self, limit: int) -> list[dict]:
        if not SQLITE_PATH.exists():
            return []
        with sqlite3.connect(SQLITE_PATH) as conn:
            self._ensure_sqlite_columns(conn)
            rows = conn.execute(
                f"""
                WITH{_ULTIMO_FEEDBACK_SQLITE}
                SELECT t.user_message, t.assistant_message,
                       {_COLUMNAS_FEEDBACK_RESUELTAS}
                FROM chat_turns t
                LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
                WHERE COALESCE(f.feedback_label, t.feedback_label)
                          IN ('parcial', 'no_resolvio')
                   OR COALESCE(f.rating, t.rating) IN (0, -1)
                ORDER BY COALESCE(f.feedback_at, t.feedback_at, t.rated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_feedback_example(r) for r in rows]

    def _recent_problematic_bigquery(self, limit: int) -> list[dict]:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None
        query = f"""
            WITH ultimo_feedback AS (
                SELECT turn_id, rating, feedback_label, feedback_tags,
                       feedback_comment, feedback_at
                FROM `{self._bq_feedback_table_id}`
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY turn_id ORDER BY feedback_at DESC) = 1
            )
            SELECT t.user_message, t.assistant_message,
                   {_COLUMNAS_FEEDBACK_RESUELTAS}
            FROM `{self._bq_table_id}` t
            LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
            WHERE COALESCE(f.feedback_label, t.feedback_label)
                      IN ('parcial', 'no_resolvio')
               OR COALESCE(f.rating, t.rating) IN (0, -1)
            ORDER BY COALESCE(f.feedback_at, t.feedback_at, t.rated_at) DESC
            LIMIT @limit
        """
        from google.cloud import bigquery

        rows = self._bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ]
            ),
        ).result()
        return [
            self._row_to_feedback_example(
                (
                    row.user_message,
                    row.assistant_message,
                    row.feedback_label,
                    row.feedback_tags,
                    row.feedback_comment,
                    str(row.feedback_at) if row.feedback_at else None,
                )
            )
            for row in rows
        ]

    # Una sola consulta con funciones de ventana en los dos backends: el
    # título es la primera pregunta de cada conversación, y el orden lo marca
    # la última actividad, no la fecha de inicio.
    _SESSIONS_SQLITE_SQL = """
        SELECT session_id, started_at, last_at, turns, title FROM (
            SELECT
                session_id,
                MIN(created_at) OVER (PARTITION BY session_id) AS started_at,
                MAX(created_at) OVER (PARTITION BY session_id) AS last_at,
                COUNT(*) OVER (PARTITION BY session_id) AS turns,
                FIRST_VALUE(user_message) OVER (
                    PARTITION BY session_id ORDER BY created_at
                ) AS title,
                ROW_NUMBER() OVER (
                    PARTITION BY session_id ORDER BY created_at
                ) AS rn
            FROM chat_turns
            WHERE user_id = ?
        )
        WHERE rn = 1
        ORDER BY last_at DESC
        LIMIT ?
    """

    _TURNS_SQLITE_SQL = f"""
        WITH{_ULTIMO_FEEDBACK_SQLITE}
        SELECT t.turn_id, t.user_message, t.assistant_message, t.created_at,
               COALESCE(f.feedback_label, t.feedback_label) AS feedback_label,
               COALESCE(f.feedback_tags, t.feedback_tags) AS feedback_tags,
               COALESCE(f.feedback_comment, t.feedback_comment) AS feedback_comment
        FROM chat_turns t
        LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
        WHERE t.session_id = ? AND t.user_id = ?
        ORDER BY t.created_at
    """

    def _list_sessions_sqlite(self, user_id: str, limit: int) -> list[dict]:
        if not SQLITE_PATH.exists():
            return []
        with sqlite3.connect(SQLITE_PATH) as conn:
            self._ensure_sqlite_columns(conn)
            rows = conn.execute(
                self._SESSIONS_SQLITE_SQL, (user_id, limit)
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def _list_sessions_bigquery(self, user_id: str, limit: int) -> list[dict]:
        from google.cloud import bigquery

        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
                bigquery.ScalarQueryParameter("limit", "INT64", limit),
            ]
        )
        rows = self._bq_client.query(
            f"""
            SELECT
                session_id,
                MIN(created_at) AS started_at,
                MAX(created_at) AS last_at,
                COUNT(*) AS turns,
                ARRAY_AGG(user_message ORDER BY created_at LIMIT 1)[OFFSET(0)]
                    AS title
            FROM `{self._bq_table_id}`
            WHERE user_id = @user_id
            GROUP BY session_id
            ORDER BY last_at DESC
            LIMIT @limit
            """,
            job_config=job_config,
        ).result()
        return [
            self._row_to_session(
                (
                    row.session_id,
                    str(row.started_at) if row.started_at else None,
                    str(row.last_at) if row.last_at else None,
                    row.turns,
                    row.title,
                )
            )
            for row in rows
        ]

    def _session_turns_sqlite(self, session_id: str, user_id: str) -> list[dict]:
        if not SQLITE_PATH.exists():
            return []
        with sqlite3.connect(SQLITE_PATH) as conn:
            self._ensure_sqlite_columns(conn)
            rows = conn.execute(
                self._TURNS_SQLITE_SQL, (session_id, user_id)
            ).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def _session_turns_bigquery(self, session_id: str, user_id: str) -> list[dict]:
        from google.cloud import bigquery

        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
                bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
            ]
        )
        rows = self._bq_client.query(
            f"""
            WITH ultimo_feedback AS (
                SELECT turn_id, feedback_label, feedback_tags, feedback_comment
                FROM `{self._bq_feedback_table_id}`
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY turn_id ORDER BY feedback_at DESC) = 1
            )
            SELECT t.turn_id, t.user_message, t.assistant_message, t.created_at,
                   COALESCE(f.feedback_label, t.feedback_label) AS feedback_label,
                   COALESCE(f.feedback_tags, t.feedback_tags) AS feedback_tags,
                   COALESCE(f.feedback_comment, t.feedback_comment)
                       AS feedback_comment
            FROM `{self._bq_table_id}` t
            LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
            WHERE t.session_id = @session_id AND t.user_id = @user_id
            ORDER BY t.created_at
            """,
            job_config=job_config,
        ).result()
        return [
            self._row_to_turn(
                (
                    row.turn_id,
                    row.user_message,
                    row.assistant_message,
                    str(row.created_at) if row.created_at else None,
                    row.feedback_label,
                    row.feedback_tags,
                    row.feedback_comment,
                )
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_session(row: tuple) -> dict:
        return {
            "session_id": row[0],
            "started_at": row[1],
            "last_at": row[2],
            "turns": row[3],
            "title": (row[4] or "").strip() or "Sin título",
        }

    @classmethod
    def _row_to_turn(cls, row: tuple) -> dict:
        return {
            "turn_id": row[0],
            "user_message": row[1],
            "assistant_message": row[2],
            "created_at": row[3],
            "feedback_label": row[4],
            "feedback_tags": cls._parse_tags(row[5]),
            "feedback_comment": row[6],
        }

    @staticmethod
    def _parse_tags(tags_raw: Any) -> list[str]:
        if not tags_raw:
            return []
        try:
            return json.loads(tags_raw)
        except json.JSONDecodeError:
            return [tags_raw]

    @classmethod
    def _row_to_feedback_example(cls, row: tuple) -> dict:
        tags = cls._parse_tags(row[3])
        return {
            "user_message": row[0],
            "assistant_message": row[1],
            "feedback_label": row[2],
            "feedback_tags": tags,
            "feedback_comment": row[4],
            "feedback_at": row[5],
        }

    def count_turns(self) -> int | None:
        """Devuelve el número de turnos almacenados (útil para admins)."""
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._init_bigquery()
                assert self._bq_client is not None and self._bq_table_id is not None
                result = self._bq_client.query(
                    f"SELECT COUNT(*) AS n FROM `{self._bq_table_id}`"
                ).result()
                return next(result).n
            with sqlite3.connect(SQLITE_PATH) as conn:
                cur = conn.execute("SELECT COUNT(*) FROM chat_turns")
                return cur.fetchone()[0]
        except Exception:
            log.exception("No se pudo contar turnos almacenados")
            return None

    def count_ratings(self) -> dict[str, int] | None:
        """Cuenta feedback por categoría (útil, parcial, no resuelto)."""
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._init_bigquery()
                assert self._bq_client is not None and self._bq_table_id is not None
                result = self._bq_client.query(
                    f"""
                    WITH ultimo_feedback AS (
                        SELECT turn_id, rating, feedback_label
                        FROM `{self._bq_feedback_table_id}`
                        QUALIFY ROW_NUMBER() OVER (
                            PARTITION BY turn_id ORDER BY feedback_at DESC) = 1
                    ),
                    resuelto AS (
                        SELECT COALESCE(f.feedback_label, t.feedback_label) AS etiqueta,
                               COALESCE(f.rating, t.rating) AS valoracion
                        FROM `{self._bq_table_id}` t
                        LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
                    )
                    SELECT
                      COUNTIF(etiqueta = 'util' OR valoracion = 1) AS util,
                      COUNTIF(etiqueta = 'parcial' OR valoracion = 0) AS parcial,
                      COUNTIF(etiqueta = 'no_resolvio' OR valoracion = -1)
                          AS no_resolvio
                    FROM resuelto
                    WHERE valoracion IS NOT NULL OR etiqueta IS NOT NULL
                    """
                ).result()
                row = next(result)
                return {
                    "util": row.util,
                    "parcial": row.parcial,
                    "no_resolvio": row.no_resolvio,
                    "thumbs_up": row.util,
                    "thumbs_down": row.no_resolvio,
                }
            if not SQLITE_PATH.exists():
                return {"util": 0, "parcial": 0, "no_resolvio": 0, "thumbs_up": 0, "thumbs_down": 0}
            with sqlite3.connect(SQLITE_PATH) as conn:
                self._ensure_sqlite_columns(conn)
                # Un turno revalorado no puede contar dos veces: se resuelve
                # primero su última valoración y luego se agrupa.
                util, parcial, no_resolvio = conn.execute(
                    f"""
                    WITH{_ULTIMO_FEEDBACK_SQLITE},
                    resuelto AS (
                        SELECT COALESCE(f.feedback_label, t.feedback_label) AS etiqueta,
                               COALESCE(f.rating, t.rating) AS valoracion
                        FROM chat_turns t
                        LEFT JOIN ultimo_feedback f ON f.turn_id = t.turn_id
                    )
                    SELECT
                        SUM(CASE WHEN etiqueta = 'util' OR valoracion = 1
                                 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN etiqueta = 'parcial' OR valoracion = 0
                                 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN etiqueta = 'no_resolvio' OR valoracion = -1
                                 THEN 1 ELSE 0 END)
                    FROM resuelto
                    """
                ).fetchone()
                util, parcial, no_resolvio = (
                    util or 0, parcial or 0, no_resolvio or 0
                )
                return {
                    "util": util,
                    "parcial": parcial,
                    "no_resolvio": no_resolvio,
                    "thumbs_up": util,
                    "thumbs_down": no_resolvio,
                }
        except Exception:
            log.exception("No se pudo contar valoraciones")
            return None


_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
