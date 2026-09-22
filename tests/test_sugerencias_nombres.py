"""Con un nombre de villa o de titular mal escrito, sugerir los parecidos en vez
de responder solo que no hay resultados."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent

VILLAS = ["ADORA", "ATALAYA", "TALAYA", "NAUTILUS", "EL RINCON DEL OLIVO", "ALHAMBRA DEL PORTET", "DIVA"]


class _ConVillas(unittest.TestCase):

    def setUp(self):
        p = patch.object(agent, "_nombres_villas", return_value=VILLAS)
        p.start()
        self.addCleanup(p.stop)
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        b = patch.object(agent, "_bq", self.bq)
        b.start()
        self.addCleanup(b.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)


class SugerirVillasTest(_ConVillas):

    def test_una_letra_de_mas_o_de_menos(self):
        self.assertEqual("ADORA", agent._sugerir_villas("Adorra")[0])
        self.assertEqual("NAUTILUS", agent._sugerir_villas("nautilos")[0])

    def test_solo_las_cercanas_a_la_mejor(self):
        self.assertEqual(["ADORA"], agent._sugerir_villas("Adorra"))

    def test_con_la_palabra_villa_delante(self):
        self.assertIn("ATALAYA", agent._sugerir_villas("Villa Atalya"))

    def test_nombres_de_varias_palabras(self):
        self.assertEqual("EL RINCON DEL OLIVO", agent._sugerir_villas("rincon del olibo")[0])

    def test_si_el_nombre_existe_no_sugiere(self):
        # "ador" encaja en ADORA: la búsqueda la encuentra, no hay que sugerir.
        self.assertEqual([], agent._sugerir_villas("ador"))
        self.assertEqual([], agent._sugerir_villas("Diva"))

    def test_nada_parecido_nada_que_sugerir(self):
        self.assertEqual([], agent._sugerir_villas("zzzzqqq"))


class VillaNoEncontradaEnCadaHerramientaTest(_ConVillas):

    def test_ficha(self):
        r = agent.obtener_detalle_propiedad("Adorra")
        self.assertEqual(["ADORA"], r["sugerencias"][:1])

    def test_precios_y_calendario(self):
        self.assertIn("ADORA", agent.consultar_precios("Adorra", "2099-10-01", "2099-10-07")["sugerencias"])
        self.assertIn("ADORA", agent.calendario_villa("Adorra", "2099-10-01", "2099-10-07")["sugerencias"])

    def test_alternativas(self):
        r = agent.alternativas_villa("Adorra", "2099-10-01", "2099-10-08")
        self.assertIn("ADORA", r["sugerencias"])

    def test_disponibilidad_de_una_villa_que_no_existe(self):
        r = agent.consultar_disponibilidad("2099-10-01", "2099-10-08", villa_nombre="Adorra")
        self.assertIn("ADORA", r["sugerencias"])

    def test_disponibilidad_de_una_villa_que_existe_pero_esta_ocupada(self):
        # 0 resultados aquí significa "no está libre", no "no existe".
        r = agent.consultar_disponibilidad("2099-10-01", "2099-10-08", villa_nombre="ADORA")
        self.assertNotIn("sugerencias", r)

    def test_reservas_de_una_villa_mal_escrita(self):
        r = agent.consultar_reservas(villa_nombre="Nautilos")
        self.assertIn("NAUTILUS", r["sugerencias"])


class SugerirTitularesTest(unittest.TestCase):

    def test_sin_reservas_para_ese_titular_busca_nombres_parecidos(self):
        bq = Mock()
        vacio = Mock(**{"result.return_value": []})
        parecidos = Mock(**{"result.return_value": [
            Mock(cliente_nombre="Farley, Olivia", puntos=1),
            Mock(cliente_nombre="Olivier, Maroy", puntos=4),
        ]})
        bq.query.side_effect = [vacio, parecidos]
        with patch.object(agent, "_bq", bq):
            r = agent.consultar_reservas(titular="Olivia Farly")
        # Solo los que están a una letra como mucho del mejor.
        self.assertEqual(["Farley, Olivia"], r["sugerencias_titular"])
        sql = bq.query.call_args_list[1][0][0]
        self.assertIn("EDIT_DISTANCE", sql)

    def test_con_reservas_no_busca_parecidos(self):
        bq = Mock()
        bq.query.return_value.result.return_value = [
            Mock(**{"items.return_value": [("localizador", "2026_1"), ("total_resultados", 1)]})]
        with patch.object(agent, "_bq", bq), patch.object(agent, "_separar_total",
                                                         return_value=([{"localizador": "2026_1"}], 1)):
            r = agent.consultar_reservas(titular="Farley")
        self.assertNotIn("sugerencias_titular", r)
        self.assertEqual(1, bq.query.call_count)


class InstruccionesTest(unittest.TestCase):

    def test_ante_sugerencias_pregunta_en_vez_de_decir_que_no_hay(self):
        for rol, ag in agent.AGENTS.items():
            texto = " ".join(ag.instruction.split())
            self.assertIn("¿te refieres a", texto, rol)
            self.assertIn("`sugerencias`", texto, rol)


if __name__ == "__main__":
    unittest.main()
