"""
Almacenamiento persistente de conversaciones del asistente.

Guarda cada turno pregunta/respuesta para análisis posterior y mejora continua.
- Producción (GCP): BigQuery (dataset configurable, por defecto agent_analytics.chat_turns)
- Desarrollo local: SQLite en data/conversations.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger("conversation-store")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
BQ_DATASET = os.environ.get("CONVERSATIONS_DATASET", "agent_analytics")
BQ_TABLE = os.environ.get("CONVERSATIONS_TABLE", "chat_turns")
SQLITE_PATH = Path(os.environ.get("CONVERSATIONS_SQLITE_PATH", "data/conversations.db"))
BACKEND = os.environ.get("CONVERSATIONS_BACKEND", "auto").lower()

Backend = Literal["bigquery", "sqlite"]


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
    ) -> None:
        """Guarda un turno completo. No lanza excepción si el almacenamiento falla."""
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
        }
        try:
            backend = self._resolve_backend()
            if backend == "bigquery":
                self._save_bigquery(row)
            else:
                self._save_sqlite(row)
        except Exception:
            log.exception("No se pudo guardar el turno de conversación")

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

        # auto: intentar BigQuery, caer a SQLite
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
            schema = [
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
            ]
            table = bigquery.Table(table_ref, schema=schema)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="created_at",
            )
            client.create_table(table)
            log.info("Tabla BigQuery creada: %s.%s.%s", PROJECT_ID, BQ_DATASET, BQ_TABLE)

        self._bq_client = client
        self._bq_table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

    def _save_bigquery(self, row: dict) -> None:
        self._init_bigquery()
        assert self._bq_client is not None and self._bq_table_id is not None

        bq_row = dict(row)
        bq_row["created_at"] = row["created_at"].replace("+00:00", "Z")

        errors = self._bq_client.insert_rows_json(self._bq_table_id, [bq_row])
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")

    def _save_sqlite(self, row: dict) -> None:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(
                """
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
                    app_name TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chat_turns (
                    turn_id, session_id, user_id, user_role,
                    user_message, assistant_message, created_at,
                    response_ms, error, app_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

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


_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
