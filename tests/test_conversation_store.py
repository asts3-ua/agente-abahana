import datetime
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import conversation_store

UTC = datetime.timezone.utc
ANA = "ana@abahanavillas.com"
LUIS = "luis@abahanavillas.com"


class ConversationHistorySqliteTest(unittest.TestCase):
    """Histórico de conversaciones sobre el backend SQLite (sin GCP)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._patch(conversation_store, "SQLITE_PATH", Path(tmp.name) / "conv.db")
        self._patch(conversation_store, "BACKEND", "sqlite")
        self.store = conversation_store.ConversationStore()

    def _patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _guardar(
        self,
        *,
        session_id,
        user_message,
        cuando,
        user_id=ANA,
        assistant_message="Respuesta",
    ):
        momento = datetime.datetime(2026, 9, 8, tzinfo=UTC) + datetime.timedelta(
            minutes=cuando
        )
        with patch.object(conversation_store, "datetime") as dt:
            dt.now.return_value = momento
            return self.store.save_turn(
                session_id=session_id,
                user_id=user_id,
                user_role="interno",
                user_message=user_message,
                assistant_message=assistant_message,
            )

    def test_lista_conversaciones_de_la_mas_activa_a_la_mas_antigua(self):
        self._guardar(session_id="s1", user_message="Villas en Calpe", cuando=0)
        self._guardar(session_id="s2", user_message="Fiestas de Altea", cuando=5)
        # s1 vuelve a tener actividad: debe adelantar a s2
        self._guardar(session_id="s1", user_message="¿Y con piscina?", cuando=10)

        sesiones = self.store.list_sessions(ANA)

        self.assertEqual(["s1", "s2"], [s["session_id"] for s in sesiones])

    def test_el_titulo_es_la_primera_pregunta_de_la_conversacion(self):
        self._guardar(session_id="s1", user_message="Villas en Calpe", cuando=0)
        self._guardar(session_id="s1", user_message="¿Y con piscina?", cuando=5)

        sesion = self.store.list_sessions(ANA)[0]

        self.assertEqual("Villas en Calpe", sesion["title"])
        self.assertEqual(2, sesion["turns"])

    def test_no_lista_conversaciones_de_otro_usuario(self):
        self._guardar(session_id="s1", user_message="Mía", cuando=0)
        self._guardar(session_id="s2", user_message="De Luis", cuando=5, user_id=LUIS)

        sesiones = self.store.list_sessions(ANA)

        self.assertEqual(["s1"], [s["session_id"] for s in sesiones])

    def test_respeta_el_limite_de_conversaciones(self):
        for i in range(5):
            self._guardar(session_id=f"s{i}", user_message=f"Pregunta {i}", cuando=i)

        self.assertEqual(2, len(self.store.list_sessions(ANA, limit=2)))

    def test_sin_conversaciones_devuelve_lista_vacia(self):
        self.assertEqual([], self.store.list_sessions(ANA))

    def test_devuelve_los_turnos_en_orden(self):
        self._guardar(session_id="s1", user_message="Primera", cuando=0)
        self._guardar(session_id="s1", user_message="Segunda", cuando=5)
        self._guardar(session_id="s2", user_message="Otra conversación", cuando=7)

        turnos = self.store.get_session_turns("s1", ANA)

        self.assertEqual(
            ["Primera", "Segunda"], [t["user_message"] for t in turnos]
        )
        self.assertEqual("Respuesta", turnos[0]["assistant_message"])

    def test_los_turnos_traen_el_feedback_ya_dado(self):
        turn_id = self._guardar(session_id="s1", user_message="Primera", cuando=0)
        self.store.save_feedback(
            turn_id, label="parcial", tags=["faltan_datos"], comment="Faltaba el precio"
        )

        turno = self.store.get_session_turns("s1", ANA)[0]

        self.assertEqual(turn_id, turno["turn_id"])
        self.assertEqual("parcial", turno["feedback_label"])
        self.assertEqual(["faltan_datos"], turno["feedback_tags"])
        self.assertEqual("Faltaba el precio", turno["feedback_comment"])

    def test_la_ultima_valoracion_es_la_que_cuenta(self):
        turn_id = self._guardar(session_id="s1", user_message="Primera", cuando=0)
        self.store.save_feedback(turn_id, label="no_resolvio")
        self.store.save_feedback(turn_id, label="util", comment="me rectifico")

        turno = self.store.get_session_turns("s1", ANA)[0]

        self.assertEqual("util", turno["feedback_label"])
        self.assertEqual("me rectifico", turno["feedback_comment"])

    def test_revalorar_no_cuenta_el_turno_dos_veces(self):
        turn_id = self._guardar(session_id="s1", user_message="Primera", cuando=0)
        self.store.save_feedback(turn_id, label="no_resolvio")
        self.store.save_feedback(turn_id, label="util")

        recuento = self.store.count_ratings()

        self.assertEqual(1, recuento["util"])
        self.assertEqual(0, recuento["no_resolvio"])

    def test_el_feedback_aparece_en_los_ejemplos_problematicos(self):
        turn_id = self._guardar(session_id="s1", user_message="Primera", cuando=0)
        self.store.save_feedback(turn_id, label="no_resolvio", comment="faltó el precio")

        ejemplos = self.store.get_recent_problematic_examples()

        self.assertEqual(1, len(ejemplos))
        self.assertEqual("no_resolvio", ejemplos[0]["feedback_label"])
        self.assertEqual("faltó el precio", ejemplos[0]["feedback_comment"])

    def test_no_devuelve_los_turnos_de_la_conversacion_de_otro(self):
        self._guardar(session_id="s1", user_message="De Luis", cuando=0, user_id=LUIS)

        self.assertEqual([], self.store.get_session_turns("s1", ANA))


class _FakeScalarQueryParameter:
    def __init__(self, name, tipo, value):
        self.name = name
        self.type_ = tipo
        self.value = value


class _FakeQueryJobConfig:
    def __init__(self, query_parameters=None):
        self.query_parameters = query_parameters or []


def _fake_bigquery_modules():
    """google.cloud.bigquery no está instalado en local; basta con un doble."""
    bigquery = types.ModuleType("google.cloud.bigquery")
    bigquery.ScalarQueryParameter = _FakeScalarQueryParameter
    bigquery.QueryJobConfig = _FakeQueryJobConfig
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = bigquery
    google = types.ModuleType("google")
    google.cloud = cloud
    return {
        "google": google,
        "google.cloud": cloud,
        "google.cloud.bigquery": bigquery,
    }


class FeedbackSinUpdateTest(unittest.TestCase):
    """BigQuery no deja actualizar filas recién insertadas por streaming:
    'UPDATE ... would affect rows in the streaming buffer'. Por eso el feedback
    se añade en lugar de actualizarse."""

    def setUp(self):
        modulos = patch.dict(sys.modules, _fake_bigquery_modules())
        modulos.start()
        self.addCleanup(modulos.stop)
        self.store = conversation_store.ConversationStore()
        self.store._backend = "bigquery"
        self.store._bq_table_id = "proj.dataset.chat_turns"
        self.store._bq_feedback_table_id = "proj.dataset.chat_feedback"
        self.store._bq_client = Mock()
        self.store._bq_client.insert_rows_json.return_value = []
        self._init = patch.object(self.store, "_init_bigquery")
        self._init.start()
        self.addCleanup(self._init.stop)

    def test_guardar_feedback_inserta_y_no_lanza_ningun_update(self):
        self.store.save_feedback("t1", label="util")

        self.store._bq_client.insert_rows_json.assert_called_once()
        for llamada in self.store._bq_client.query.call_args_list:
            self.assertNotIn("UPDATE", llamada[0][0].upper())

    def test_la_fila_insertada_lleva_el_turno_y_la_valoracion(self):
        self.store.save_feedback("t1", label="parcial", tags=["faltan_datos"])

        tabla, filas = self.store._bq_client.insert_rows_json.call_args[0][:2]
        self.assertIn("chat_feedback", tabla)
        self.assertEqual("t1", filas[0]["turn_id"])
        self.assertEqual("parcial", filas[0]["feedback_label"])


class EsquemaBigQueryTest(unittest.TestCase):
    """Los tipos de SQLite no valen en BigQuery: TEXT lo rechaza."""

    class _Campo:
        def __init__(self, name, field_type, mode="NULLABLE"):
            self.name = name
            self.field_type = field_type
            self.mode = mode

    def _campos(self):
        bigquery = types.SimpleNamespace(SchemaField=self._Campo)
        return {
            c.name: c.field_type
            for c in conversation_store.ConversationStore._bq_schema_fields(bigquery)
        }

    def test_ningun_campo_usa_tipos_de_sqlite(self):
        invalidos = {n: t for n, t in self._campos().items() if t == "TEXT"}
        self.assertEqual({}, invalidos, "BigQuery rechaza TEXT al crear la tabla")

    def test_las_columnas_de_feedback_tienen_el_tipo_que_se_escribe(self):
        campos = self._campos()
        self.assertEqual("STRING", campos["feedback_label"])
        self.assertEqual("STRING", campos["feedback_tags"])
        self.assertEqual("STRING", campos["feedback_comment"])
        self.assertEqual("INTEGER", campos["feedback_score"])
        # save_feedback lo envía como parámetro TIMESTAMP.
        self.assertEqual("TIMESTAMP", campos["feedback_at"])

    def test_la_ddl_de_sqlite_sigue_usando_tipos_de_sqlite(self):
        ddl = conversation_store.ConversationStore()._sqlite_create_table_sql()
        self.assertIn("feedback_label TEXT", ddl)
        self.assertIn("feedback_score INTEGER", ddl)
        self.assertNotIn("STRING", ddl)


class ConversationHistoryBigQueryTest(unittest.TestCase):
    """El filtro por usuario debe viajar como parámetro, no interpolado."""

    def setUp(self):
        modulos = patch.dict(sys.modules, _fake_bigquery_modules())
        modulos.start()
        self.addCleanup(modulos.stop)
        self.store = conversation_store.ConversationStore()
        self.store._backend = "bigquery"
        self.store._bq_table_id = "proj.dataset.chat_turns"
        self.store._bq_client = Mock()
        self.store._bq_client.query.return_value.result.return_value = []
        self._init = patch.object(self.store, "_init_bigquery")
        self._init.start()
        self.addCleanup(self._init.stop)

    def _parametros(self):
        _, kwargs = self.store._bq_client.query.call_args
        return {p.name: p.value for p in kwargs["job_config"].query_parameters}

    def test_list_sessions_filtra_por_usuario_con_parametro(self):
        self.store.list_sessions(ANA, limit=7)

        consulta = self.store._bq_client.query.call_args[0][0]
        self.assertNotIn(ANA, consulta)
        self.assertEqual(ANA, self._parametros()["user_id"])
        self.assertEqual(7, self._parametros()["limit"])

    def test_get_session_turns_filtra_por_usuario_y_sesion(self):
        self.store.get_session_turns("s1", ANA)

        consulta = self.store._bq_client.query.call_args[0][0]
        self.assertNotIn(ANA, consulta)
        self.assertEqual(ANA, self._parametros()["user_id"])
        self.assertEqual("s1", self._parametros()["session_id"])


if __name__ == "__main__":
    unittest.main()
