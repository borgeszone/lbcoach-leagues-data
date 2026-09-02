# -*- coding: utf-8 -*-
"""Los escudos que trae el calendario, que era la fuente que faltaba.

La cobertura estaba en el 33 % (65 de 192) y el techo parecía temporal: la única
fuente que da el escudo **en la misma fila que el nombre** era la clasificación,
y la clasificación no existe hasta que se juega la J1. Medido el 2026-09-02, con
la temporada arrancando dos días después, su página respondía 60 KB sin tabla.

El calendario sí los trae, y el scraper ya lo descarga entero. Sobre el fixture
—captura real de la PNFG, Segunda Femenina Grupo 1— la cadena completa lleva ese
grupo de 9 escudos a 14 de 16.

Los dos que faltan son el caso que este fichero existe para no volver a
estropear: la PNFG sirve un **placeholder genérico**, la misma URL para todos,
y publicarlo como escudo sería peor que el hueco.

Ninguno toca la red.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers import logo_resolver  # noqa: E402
from scrapers.rfef_calendario import (  # noqa: E402
    _is_placeholder,
    _parse_matches,
    parse_jornada_badges,
)

FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
           / "jornada1_segunda-fem-g1_2026-2027.html")
HTML = FIXTURE.read_text(encoding="iso-8859-15", errors="replace")

PLACEHOLDER = ("https://filesrfef.novanet.es/pnfg/img/web_responsive_2/ESP/"
               "escudo_sm_resultados_.jpg")


class LaCosechaDelCalendario(unittest.TestCase):
    """Sobre HTML real de la PNFG, no sobre uno inventado."""

    def test_saca_un_escudo_por_equipo_que_juega(self):
        # 8 partidos = 16 equipos, menos los 2 que sólo tienen placeholder.
        self.assertEqual(len(parse_jornada_badges(HTML)), 14)

    def test_no_cambia_lo_que_ya_hacia_el_parser_de_partidos(self):
        """El iterador de filas es compartido, así que el riesgo de este cambio
        es romper el parseo del calendario, que es lo que de verdad importa.

        Se comprueban los emparejamientos exactos y no "que haya fechas": este
        fixture no trae ninguna —sus filas no llevan el sello `DD-MM-YYYY
        HH:MM`— y la primera versión de este test lo daba por hecho y salió en
        rojo. Los pares son además un guardarraíl más fuerte: si el iterador
        emparejara mal, se vería aquí.
        """
        ms = _parse_matches(HTML)
        self.assertEqual(len(ms), 8)
        self.assertEqual(ms[0]["home"], "U.D.C. Txantrea K.K.E.")
        self.assertEqual(ms[0]["away"], "El Gaitero Rodiles FSF")
        self.assertEqual(ms[1]["home"], "CD Lacturale Orvina")
        self.assertEqual(ms[1]["away"], "FC Meigas")
        self.assertTrue(all(m["home"] and m["away"] for m in ms))

    def test_el_escudo_es_el_de_su_equipo_y_no_el_del_vecino(self):
        """Lo que hace fiable esta fuente: la asociación la confirma la fila, no
        un parecido de nombres. En cada `<tr>` van dos imágenes, local y
        visitante, en ese orden.

        Si el orden se invirtiera, cada equipo saldría con el escudo de su rival
        de la J1 — y un escudo equivocado se parece muchísimo a un escudo, así
        que no se vería en ningún recuento de cobertura.
        """
        b = parse_jornada_badges(HTML)
        # Fila real del fixture: 'CD Lacturale Orvina' vs 'FC Meigas'.
        self.assertIn("orvina", b["CD Lacturale Orvina"].lower())
        self.assertIn("meigas", b["FC Meigas"].lower())

    def test_una_fila_sin_las_dos_imagenes_no_afirma_nada(self):
        html = """<table><tr>
            <td><div class="font_widgetL">Alfa</div></td>
            <td><img src="https://rfef.filesnovanet.es/pnfg/pimg/Clubes/a.png"></td>
            <td><div class="font_widgetV">Beta</div></td>
        </tr></table>"""
        self.assertEqual(parse_jornada_badges(html), {})


class ElPlaceholderNoEsUnEscudo(unittest.TestCase):
    """La misma URL para todos, sin ningún id dentro. Publicarlo daría el mismo
    dibujo gris a media liga, con el agravante de que contaría como cobertura."""

    def test_se_descarta(self):
        self.assertNotIn(PLACEHOLDER, parse_jornada_badges(HTML).values())

    def test_los_dos_equipos_sin_escudo_se_quedan_sin_escudo(self):
        b = parse_jornada_badges(HTML)
        self.assertNotIn("At. Arnoya", b)
        self.assertNotIn('CLUB DEPORTIVO TEIDAYA "A"', b)

    def test_se_reconoce_por_lo_que_es_y_no_por_el_host(self):
        """Hoy viene de un dominio que la allowlist no admite, así que el filtro
        de `logo_resolver` lo pararía de rebote. Eso es una casualidad: el día
        que lo sirvan desde el host bueno tiene que seguir cayéndose."""
        self.assertTrue(_is_placeholder(
            "https://rfef.filesnovanet.es/pnfg/img/web_responsive_2/ESP/"
            "escudo_sm_resultados_.jpg"))

    def test_y_un_escudo_de_verdad_no_se_descarta(self):
        self.assertFalse(_is_placeholder(
            "https://rfef.filesnovanet.es/pnfg/pimg/Clubes/00100_007_logo.png"))


class LaInyeccionEnElResolver(unittest.TestCase):
    """`add_rfef_shields` es la frontera: aquí se aplica el filtro de dominio y
    aquí se normalizan las claves."""

    def setUp(self):
        self._orig = dict(logo_resolver._rfef_shields)

    def tearDown(self):
        logo_resolver._rfef_shields = self._orig

    def test_sube_la_cobertura_del_grupo(self):
        cosecha = parse_jornada_badges(HTML)
        antes = sum(1 for n in cosecha
                    if logo_resolver.resolve_logo_url(n, trusted_only=True))
        logo_resolver.add_rfef_shields(cosecha)
        despues = sum(1 for n in cosecha
                      if logo_resolver.resolve_logo_url(n, trusted_only=True))
        self.assertGreater(despues, antes, f'{antes} -> {despues}')
        self.assertEqual(despues, len(cosecha))

    def test_normaliza_las_claves_ella_misma(self):
        """Su llamador tiene los nombres tal y como los escribió la federación.
        Si no normalizara aquí, el mapa se indexaría con una clave y se
        consultaría con otra — y eso no falla en voz alta, simplemente no
        encuentra nada."""
        logo_resolver.add_rfef_shields({
            'C.D. Un Nombre Con Puntos':
                'https://rfef.filesnovanet.es/pnfg/pimg/Clubes/x.png'})
        self.assertIsNotNone(
            logo_resolver.resolve_logo_url('C.D. Un Nombre Con Puntos'))

    def test_no_pisa_lo_que_ya_habia(self):
        """El mapa del portal se inyecta antes, al empezar el run. Si esto
        reemplazara —que es lo que hace `inject_rfef_shields`— lo borraría."""
        logo_resolver.inject_rfef_shields({
            'alfa': 'https://futsal.rfef.es/primero.png'})
        logo_resolver.add_rfef_shields({
            'Alfa': 'https://rfef.filesnovanet.es/segundo.png'})
        self.assertEqual(logo_resolver.resolve_logo_url('Alfa'),
                         'https://futsal.rfef.es/primero.png')

    def test_un_dominio_de_fuera_no_entra(self):
        logo_resolver.add_rfef_shields({
            'Beta': 'https://t.resfu.com/img_data/equipos/1041.png'})
        self.assertIsNone(logo_resolver.resolve_logo_url('Beta'))


if __name__ == "__main__":
    unittest.main()
