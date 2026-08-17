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
SQLITE_PATH = Path(os.environ.get("CONVERSATIONS_SQLITE_PATH", "data/conversations.db"))
BACKEND = os.environ.get("CONVERSATIONS_BACKEND", "auto").lower()

Backend = Literal["bigquery", "sqlite"]

FEEDBACK_LABELS = {"util", "parcial", "no_resolvio"}
FEEDBACK_SCORES = {"util": 3, "parcial": 2, "no_resolvio": 1}
LEGACY_RATINGS = {"util": 1, "parcial": 0, "no_resolvio": -1}

_FEEDBACK_COLUMNS: list[tuple[str, str]] = [
    ("feedback_score", "INTEGER"),
    ("feedback_label", "TEXT"),
    ("feedback_tags", "TEXT"),
    ("feedback_comment", "TEXT"),
    ("feedback_at", "TEXT"),
]

# Fidelidad automática (juez LLM vs evidencia de tools). Se inserta en el
# mismo INSERT que el turno para no chocar con el buffer de streaming de BQ.
_GROUNDEDNESS_COLUMNS: list[tuple[str, str]] = [
    ("groundedness_verdict", "TEXT"),
    ("groundedness_score", "REAL"),
    ("groundedness_summary", "TEXT"),
    ("ungrounded_claims", "TEXT"),
    ("tools_used", "TEXT"),
    ("tool_trace", "TEXT"),
]

_EXTRA_COLUMNS = _FEEDBACK_COLUMNS + _GROUNDEDNESS_COLUMNS


class ConversationStore:
    """Persiste turnos de chat (pregunta + respuesta) de forma durable."""

    def __init__(self) -> None:
        self._backend: Backend | None = None
        self._bq_client = None
        self._bq_table_id: str | None = None

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
        groundedness: dict[str, Any] | None = None,
    ) -> str | None:
        """Guarda un turno completo. Devuelve turn_id o None si falla."""
        g = groundedness or {}
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
            "groundedness_verdict": g.get("groundedness_verdict"),
            "groundedness_score": g.get("groundedness_score"),
            "groundedness_summary": g.get("groundedness_summary"),
            "ungrounded_claims": g.get("ungrounded_claims"),
            "tools_used": g.get("tools_used"),
            "tool_trace": g.get("tool_trace"),
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
            log.warning("BigQuery no disponible (%s), usando SQLite local", exc)
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

        self._bq_client = client
        self._bq_table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

    @staticmethod
    def _sqlite_type_to_bq(sqlite_type: str) -> str:
        mapping = {"INTEGER": "INTEGER", "TEXT": "STRING", "REAL": "FLOAT"}
        return mapping.get(sqlite_type.upper(), "STRING")

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
        for name, sqlite_type in _EXTRA_COLUMNS:
            fields.append(
                bigquery.SchemaField(name, _sqlite_type_to_bq(sqlite_type))
            )
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
        for name, sqlite_type in _EXTRA_COLUMNS:
            if name not in names:
                new_fields.append(
                    bigquery.SchemaField(name, _sqlite_type_to_bq(sqlite_type))
                )
        if new_fields:
            table.schema = list(table.schema) + new_fields
            client.update_table(table, ["schema"])
            log.info("Columnas extra añadidas a %s.%s", BQ_DATASET, BQ_TABLE)

    def _save_bigquery(self, row: dict) -> None:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None

        bq_row = dict(row)
        bq_row["created_at"] = row["created_at"].replace("+00:00", "Z")

        errors = self._bq_client.insert_rows_json(self._bq_table_id, [bq_row])
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")

    def _sqlite_create_table_sql(self) -> str:
        extra_cols = ", ".join(
            f"{name} {col_type}" for name, col_type in _EXTRA_COLUMNS
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
                {extra_cols}
            )
        """

    def _ensure_sqlite_columns(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_turns)")}
        if "rating" not in cols:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN rating INTEGER")
        if "rated_at" not in cols:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN rated_at TEXT")
        for name, col_type in _EXTRA_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE chat_turns ADD COLUMN {name} {col_type}")

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
                    feedback_comment, feedback_at,
                    groundedness_verdict, groundedness_score,
                    groundedness_summary, ungrounded_claims,
                    tools_used, tool_trace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row.get("groundedness_verdict"),
                    row.get("groundedness_score"),
                    row.get("groundedness_summary"),
                    row.get("ungrounded_claims"),
                    row.get("tools_used"),
                    row.get("tool_trace"),
                ),
            )
            conn.commit()

    def _save_feedback_sqlite(self, turn_id: str, payload: dict[str, Any]) -> None:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SQLITE_PATH) as conn:
            self._ensure_sqlite_columns(conn)
            conn.execute(
                """
                UPDATE chat_turns SET
                    rating = ?, rated_at = ?,
                    feedback_score = ?, feedback_label = ?,
                    feedback_tags = ?, feedback_comment = ?, feedback_at = ?
                WHERE turn_id = ?
                """,
                (
                    payload["rating"],
                    payload["rated_at"],
                    payload["feedback_score"],
                    payload["feedback_label"],
                    payload["feedback_tags"],
                    payload["feedback_comment"],
                    payload["feedback_at"],
                    turn_id,
                ),
            )
            conn.commit()

    def _save_feedback_bigquery(self, turn_id: str, payload: dict[str, Any]) -> None:
        from google.cloud import bigquery

        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None

        feedback_at_bq = payload["feedback_at"].replace("+00:00", "Z")
        query = f"""
            UPDATE `{self._bq_table_id}`
            SET
                rating = @rating,
                rated_at = @rated_at,
                feedback_score = @feedback_score,
                feedback_label = @feedback_label,
                feedback_tags = @feedback_tags,
                feedback_comment = @feedback_comment,
                feedback_at = @feedback_at
            WHERE turn_id = @turn_id
        """
        job = self._bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("rating", "INT64", payload["rating"]),
                    bigquery.ScalarQueryParameter("rated_at", "TIMESTAMP", feedback_at_bq),
                    bigquery.ScalarQueryParameter(
                        "feedback_score", "INT64", payload["feedback_score"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "feedback_label", "STRING", payload["feedback_label"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "feedback_tags", "STRING", payload["feedback_tags"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "feedback_comment", "STRING", payload["feedback_comment"]
                    ),
                    bigquery.ScalarQueryParameter("feedback_at", "TIMESTAMP", feedback_at_bq),
                    bigquery.ScalarQueryParameter("turn_id", "STRING", turn_id),
                ]
            ),
        )
        job.result()
        if job.num_dml_affected_rows == 0:
            log.warning(
                "UPDATE de feedback no afectó filas (posible buffer de streaming): turn_id=%s",
                turn_id,
            )

    def _recent_problematic_sqlite(self, limit: int) -> list[dict]:
        if not SQLITE_PATH.exists():
            return []
        with sqlite3.connect(SQLITE_PATH) as conn:
            self._ensure_sqlite_columns(conn)
            rows = conn.execute(
                """
                SELECT user_message, assistant_message, feedback_label,
                       feedback_tags, feedback_comment, feedback_at
                FROM chat_turns
                WHERE feedback_label IN ('parcial', 'no_resolvio')
                   OR rating IN (0, -1)
                ORDER BY COALESCE(feedback_at, rated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_feedback_example(r) for r in rows]

    def _recent_problematic_bigquery(self, limit: int) -> list[dict]:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None
        query = f"""
            SELECT user_message, assistant_message, feedback_label,
                   feedback_tags, feedback_comment, feedback_at
            FROM `{self._bq_table_id}`
            WHERE feedback_label IN ('parcial', 'no_resolvio')
               OR rating IN (0, -1)
            ORDER BY COALESCE(feedback_at, rated_at) DESC
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

    @staticmethod
    def _row_to_feedback_example(row: tuple) -> dict:
        tags_raw = row[3]
        tags: list[str] = []
        if tags_raw:
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = [tags_raw]
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
                    SELECT
                      COUNTIF(feedback_label = 'util' OR rating = 1) AS util,
                      COUNTIF(feedback_label = 'parcial' OR rating = 0) AS parcial,
                      COUNTIF(feedback_label = 'no_resolvio' OR rating = -1) AS no_resolvio
                    FROM `{self._bq_table_id}`
                    WHERE rating IS NOT NULL OR feedback_label IS NOT NULL
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
                util = conn.execute(
                    """
                    SELECT COUNT(*) FROM chat_turns
                    WHERE feedback_label = 'util' OR rating = 1
                    """
                ).fetchone()[0]
                parcial = conn.execute(
                    """
                    SELECT COUNT(*) FROM chat_turns
                    WHERE feedback_label = 'parcial' OR rating = 0
                    """
                ).fetchone()[0]
                no_resolvio = conn.execute(
                    """
                    SELECT COUNT(*) FROM chat_turns
                    WHERE feedback_label = 'no_resolvio' OR rating = -1
                    """
                ).fetchone()[0]
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

    def count_groundedness(self) -> dict[str, int] | None:
        """Cuenta veredictos de fidelidad automática."""
        keys = (
            "soportada",
            "parcial",
            "no_soportada",
            "sin_evidencia",
            "conversacional",
            "error",
        )
        empty = {k: 0 for k in keys}
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._init_bigquery()
                assert self._bq_client is not None and self._bq_table_id is not None
                result = self._bq_client.query(
                    f"""
                    SELECT groundedness_verdict AS v, COUNT(*) AS n
                    FROM `{self._bq_table_id}`
                    WHERE groundedness_verdict IS NOT NULL
                    GROUP BY groundedness_verdict
                    """
                ).result()
                counts = dict(empty)
                for row in result:
                    if row.v in counts:
                        counts[row.v] = row.n
                return counts
            if not SQLITE_PATH.exists():
                return empty
            with sqlite3.connect(SQLITE_PATH) as conn:
                self._ensure_sqlite_columns(conn)
                rows = conn.execute(
                    """
                    SELECT groundedness_verdict, COUNT(*)
                    FROM chat_turns
                    WHERE groundedness_verdict IS NOT NULL
                    GROUP BY groundedness_verdict
                    """
                ).fetchall()
            counts = dict(empty)
            for verdict, n in rows:
                if verdict in counts:
                    counts[verdict] = n
            return counts
        except Exception:
            log.exception("No se pudo contar veredictos de fidelidad")
            return None


_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
