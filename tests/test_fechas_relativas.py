"""Con "mañana", "este sábado" o "este fin de semana" la respuesta dice la
fecha exacta que se ha usado, con su día de la semana."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class FechaExactaEnLaRespuestaTest(unittest.TestCase):

    def test_los_tres_roles_tienen_la_regla(self):
        for rol, ag in agent.AGENTS.items():
            texto = " ".join(ag.instruction.split())
            self.assertIn("di la fecha exacta que has usado", texto, rol)
            self.assertIn("este sábado, <día> de <mes> de <año>", texto, rol)

    def test_el_ejemplo_no_lleva_fechas_fijas(self):
        # Un ejemplo con fechas concretas quedaría mal en unos días y el
        # modelo podría copiarlo.
        texto = " ".join(agent.AGENTS["interno"].instruction.split())
        inicio = texto.index("di la fecha exacta que has usado")
        self.assertNotRegex(texto[inicio:inicio + 400], r"\b\d{1,2} de [a-z]+ de 20\d\d")


if __name__ == "__main__":
    unittest.main()
