"""El "Cómo lo he calculado" sobrevive a cerrar la conversación.

Sin esto, auditar una respuesta de hace semanas es imposible: el número sigue
ahí pero la cuenta que lo produjo se perdió al recargar. Se guarda como JSON
en el turno, junto a la pregunta y la respuesta.
"""

import datetime
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import conversation_store

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app

UTC = datetime.timezone.utc
ANA = "ana@abahanavillas.com"

METODO = [{
    "titulo": "Ocupación · junio 2026",
    "fuentes": ["stg_etendo_Ocupacion"],
    "criterios": ["Quedan fuera del cálculo las noches del propietario."],
    "formula": "3.305 ÷ 5.020 = 65,84 %",
}]


class GuardarYRecuperarTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("SQLITE_PATH", Path(tmp.name) / "conv.db"),
                            ("BACKEND", "sqlite")):
            p = patch.object(conversation_store, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.store = conversation_store.ConversationStore()

    def _guardar(self, metodo=METODO, **kw):
        return self.store.save_turn(
            session_id="s1", user_id=ANA, user_role="admin",
            user_message="¿Ocupación de junio?", assistant_message="65,84 %",
            metodo=metodo, **kw)

    def test_vuelve_tal_cual_lo_guardamos(self):
        self._guardar()
        turno = self.store.get_session_turns("s1", ANA)[0]
        self.assertEqual(METODO, turno["metodo"])

    def test_un_turno_sin_calculos_no_guarda_nada(self):
        self._guardar(metodo=[])
        self.assertEqual([], self.store.get_session_turns("s1", ANA)[0]["metodo"])

    def test_los_acentos_y_los_simbolos_sobreviven(self):
        self._guardar()
        metodo = self.store.get_session_turns("s1", ANA)[0]["metodo"][0]
        self.assertIn("÷", metodo["formula"])
        self.assertIn("cálculo", metodo["criterios"][0])

    def test_un_turno_viejo_sin_la_columna_no_rompe(self):
        """Las conversaciones de antes de este cambio siguen abriéndose."""
        self._guardar(metodo=None)
        self.assertEqual([], self.store.get_session_turns("s1", ANA)[0]["metodo"])

    def test_un_json_corrupto_no_tumba_la_conversacion(self):
        self._guardar()
        import sqlite3
        with sqlite3.connect(conversation_store.SQLITE_PATH) as conn:
            conn.execute("UPDATE chat_turns SET metodo = '{no es json'")
            conn.commit()
        self.assertEqual([], self.store.get_session_turns("s1", ANA)[0]["metodo"])


class EsquemaTest(unittest.TestCase):

    def test_la_columna_existe_en_los_dos_motores(self):
        self.assertIn(("metodo", "TEXT", "STRING"),
                      conversation_store._COLUMNAS_EXTRA)

    def test_se_añade_a_una_tabla_que_ya_existia(self):
        """La tabla de producción ya está creada: hay que migrarla."""
        codigo = inspect.getsource(conversation_store.ConversationStore._ensure_sqlite_columns)
        self.assertIn("_COLUMNAS_EXTRA", codigo)
        codigo_bq = inspect.getsource(conversation_store.ConversationStore._ensure_bq_columns)
        self.assertIn("_COLUMNAS_EXTRA", codigo_bq)

    def test_la_consulta_de_turnos_lo_trae_en_los_dos_motores(self):
        self.assertIn("t.metodo", conversation_store.ConversationStore._TURNS_SQLITE_SQL)
        self.assertIn("t.metodo", inspect.getsource(
            conversation_store.ConversationStore._session_turns_bigquery))


class EnLaAppTest(unittest.TestCase):

    def test_el_turno_se_guarda_con_su_metodo(self):
        self.assertIn("metodo=", inspect.getsource(chat_app._process_user_prompt))

    def test_al_abrir_una_conversacion_se_recupera(self):
        self.assertIn('"metodo": turno.get("metodo")',
                      inspect.getsource(chat_app._load_conversation))


if __name__ == "__main__":
    unittest.main()
