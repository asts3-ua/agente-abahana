"""Los datos de acceso (alarma, caja fuerte, wifi) son para el equipo interno.

En producción el agente se negaba a dar el código de alarma a un manager y
llegaba a afirmar que no tenía acceso: las instrucciones decían cuándo pedir la
sección acceso_seguridad, pero nunca que el usuario estaba autorizado a verla.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class InstruccionesDeAccesoTest(unittest.TestCase):

    def test_interno_y_admin_saben_que_estan_autorizados(self):
        for rol in ("interno", "admin"):
            instruccion = agent.AGENTS[rol].instruction
            self.assertIn("acceso_seguridad", instruccion, rol)
            self.assertIn("autorizado", instruccion, rol)

    def test_no_pueden_decir_que_no_tienen_acceso(self):
        for rol in ("interno", "admin"):
            self.assertIn("No digas que no tienes acceso", agent.AGENTS[rol].instruction, rol)

    def test_cubre_las_preguntas_de_seguimiento(self):
        # "¿y cuál es su contraseña?" tras preguntar si tienen alarma.
        for rol in ("interno", "admin"):
            instruccion = agent.AGENTS[rol].instruction
            self.assertIn("contraseña", instruccion, rol)
            self.assertIn("seguimiento", instruccion, rol)

    def test_el_cliente_ni_la_conoce(self):
        self.assertNotIn("acceso_seguridad", agent.AGENTS["cliente"].instruction)


if __name__ == "__main__":
    unittest.main()
