"""Roundtrip SQLite del turno con campos de fidelidad."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import conversation_store
from groundedness import GroundednessResult


def test_save_turn_persists_groundedness(tmp_path, monkeypatch):
    db = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "BACKEND", "sqlite")
    monkeypatch.setattr(conversation_store, "SQLITE_PATH", db)
    store = conversation_store.ConversationStore()

    result = GroundednessResult(
        verdict="parcial",
        score=0.6,
        summary="Una cifra no aparece en las tools",
        ungrounded_claims=["capacidad 20"],
        tools_used=["buscar_propiedades"],
        tool_trace=[{"kind": "result", "name": "buscar_propiedades", "payload": {}}],
    )
    turn_id = store.save_turn(
        session_id="s1",
        user_id="user@abahana.com",
        user_role="interno",
        user_message="¿Villas en Calpe?",
        assistant_message="Hay una villa para 20.",
        groundedness=result.to_store_fields(),
    )
    assert turn_id

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """
            SELECT groundedness_verdict, groundedness_score,
                   groundedness_summary, ungrounded_claims, tools_used
            FROM chat_turns WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()

    assert row[0] == "parcial"
    assert row[1] == 0.6
    assert "cifra" in row[2]
    assert json.loads(row[3]) == ["capacidad 20"]
    assert row[4] == "buscar_propiedades"

    counts = store.count_groundedness()
    assert counts is not None
    assert counts["parcial"] == 1
    assert counts["soportada"] == 0
