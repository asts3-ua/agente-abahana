"""Tests puros del verificador de fidelidad (sin credenciales GCP)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import groundedness
from groundedness import (
    ToolEvent,
    extract_tool_events,
    format_evidence,
    parse_judge_payload,
    result_from_judge_payload,
    tool_events_to_trace,
    tools_used_from_trace,
)


class _FakeCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeResp:
    def __init__(self, name, response):
        self.name = name
        self.response = response


class _FakeEvent:
    def __init__(self, calls=None, responses=None):
        self._calls = calls or []
        self._responses = responses or []

    def get_function_calls(self):
        return self._calls

    def get_function_responses(self):
        return self._responses


def test_extract_tool_events_calls_and_results():
    event = _FakeEvent(
        calls=[_FakeCall("buscar_propiedades", {"ubicacion": "Calpe"})],
        responses=[
            _FakeResp(
                "buscar_propiedades",
                {"matches": [{"nombre": "ADORA"}], "count": 1},
            )
        ],
    )
    events = extract_tool_events(event)
    assert [e.kind for e in events] == ["call", "result"]
    assert events[0].name == "buscar_propiedades"
    assert events[1].payload["count"] == 1


def test_compact_payload_trunca_listas_largas():
    event = _FakeEvent(
        responses=[
            _FakeResp(
                "listar_propiedades",
                {"matches": [{"n": i} for i in range(20)], "count": 20},
            )
        ]
    )
    payload = extract_tool_events(event)[0].payload
    assert len(payload["matches"]) == 8
    assert payload["matches_omitidos"] == 12
    assert payload["count"] == 20


def test_tools_used_preserva_orden_sin_duplicados():
    trace = tool_events_to_trace([
        ToolEvent("call", "buscar_propiedades", {}),
        ToolEvent("result", "buscar_propiedades", {}),
        ToolEvent("call", "buscar_internet", {}),
    ])
    assert tools_used_from_trace(trace) == ["buscar_propiedades", "buscar_internet"]


def test_format_evidence_sin_tools():
    assert "sin herramientas" in format_evidence([])


def test_parse_judge_payload_con_fence_markdown():
    raw = """```json
    {"veredicto": "soportada", "confianza": 0.9,
     "afirmaciones_no_soportadas": [], "resumen": "ok"}
    ```"""
    data = parse_judge_payload(raw)
    assert data["veredicto"] == "soportada"


def test_parse_judge_payload_con_texto_alrededor():
    raw = 'El JSON:\n{"veredicto": "parcial", "confianza": 0.5, "afirmaciones_no_soportadas": ["x"], "resumen": "mixto"}\nfin'
    data = parse_judge_payload(raw)
    assert data["veredicto"] == "parcial"


def test_parse_judge_payload_vacio():
    with pytest.raises(ValueError):
        parse_judge_payload("   ")


def test_result_from_judge_payload_normaliza():
    result = result_from_judge_payload(
        {
            "veredicto": "NO_SOPORTADA",
            "confianza": 1.7,
            "afirmaciones_no_soportadas": ["Villa inventada", "  ", 3],
            "resumen": "Inventa datos",
        },
        tools_used=["buscar_propiedades"],
        tool_trace=[],
    )
    assert result.verdict == "no_soportada"
    assert result.score == 1.0
    assert result.ungrounded_claims == ["Villa inventada", "3"]
    store = result.to_store_fields()
    assert store["groundedness_verdict"] == "no_soportada"
    assert store["tools_used"] == "buscar_propiedades"


def test_result_from_judge_payload_veredicto_invalido():
    with pytest.raises(ValueError):
        result_from_judge_payload(
            {"veredicto": "regular"},
            tools_used=[],
            tool_trace=[],
        )


def test_judge_fail_open_si_el_modelo_falla():
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("cuota")
            )
        )
    )
    result = groundedness.judge_groundedness(
        question="¿Villas en Calpe?",
        answer="Hay 3 villas.",
        tool_trace=[],
        genai_client=client,
    )
    assert result.verdict == "error"
    assert result.error
    assert "No se pudo verificar" in result.summary


def test_to_ui_dict_no_incluye_la_traza():
    result = result_from_judge_payload(
        {
            "veredicto": "soportada",
            "confianza": 0.8,
            "afirmaciones_no_soportadas": [],
            "resumen": "Fiel",
        },
        tools_used=["buscar_propiedades"],
        tool_trace=[{"kind": "result", "name": "buscar_propiedades", "payload": {}}],
    )
    ui = result.to_ui_dict()
    assert "tool_trace" not in ui
    assert ui["verdict"] == "soportada"
