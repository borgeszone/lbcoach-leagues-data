"""Tests de la web pública de la RFEF como fuente de equipos (`rfef_web`).

Las fixtures son **reales**, capturadas el 2026-08-29, y una de ellas está ahí
precisamente por ser vieja: `calendario_grupo_2_segunda_femenina_2025-2026.pdf`
es el fichero que sigue vivo en la URL que el scraper llevaba escrita, y es
literalmente el que metió los 48 equipos de 2025-26 en el JSON de 2026-27. Si
alguna vez vuelve a colarse, el test que lo rechaza es el que se pone rojo.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers import rfef_web  # noqa: E402
from scrapers.rfef_discovery import is_phase  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PAGINA_SEGUNDA_FEM = FIXTURES / "competicion_segunda-fem_2026-2027.html"
PDF_G2_NUEVO = FIXTURES / "calendario_2af_g2_2026-2027.pdf"
PDF_G2_VIEJO = FIXTURES / "calendario_grupo_2_segunda_femenina_2025-2026.pdf"
PDF_SEGUNDA_B_G4 = FIXTURES / "calendario_segunda-b_g4_2026-2027.pdf"
PDF_LIGA_PRIME = FIXTURES / "calendario_liga-prime-futsal_2026-2027.pdf"


class LaPaginaDeCompeticion(unittest.TestCase):
    """`ul.lista-escudos` es la única fuente que existe en pretemporada: la
    clasificación de la PNFG no se puebla hasta la J1."""

    @classmethod
    def setUpClass(cls):
        cls.html = PAGINA_SEGUNDA_FEM.read_text(encoding="utf-8")
        cls.teams = rfef_web.parse_teams(cls.html)

    def test_saca_los_48_de_la_division(self):
        self.assertEqual(len(self.teams), 48)

    def test_trae_los_equipos_nuevos_de_2026_27(self):
        """Los que distinguen esta temporada de la anterior. Si el parser
        devolviera la lista del año pasado, estos tres no estarían."""
        nombres = {t.name for t in self.teams}
        for nuevo in ("La Villa Móstoles", "Parque Col. Santa Ana",
                      "AE Penya Esplugues"):
            self.assertIn(nuevo, nombres)

    def test_encuentra_las_noticias_de_calendario(self):
        """Los PDF no cuelgan de la página de competición: viven en la noticia
        que los anuncia."""
        links = rfef_web.parse_news_links(self.html)
        self.assertTrue(links)
        self.assertTrue(all("calendario" in u.lower() for u in links))


class ElGuardDeTemporadaDelPdf(unittest.TestCase):
    """El bug de agosto de 2026, en dos tests.

    `calendario_grupo_2_segunda_femenina_futbol_sala.pdf` es una URL **sin
    temporada dentro** que la federación no ha vuelto a tocar desde el
    25/08/2025. Sigue respondiendo 200 y sigue sirviendo la liga del año
    pasado."""

    def test_el_pdf_viejo_se_declara_de_2025_26(self):
        r = rfef_web.read_pdf_roster(PDF_G2_VIEJO.read_bytes())
        self.assertEqual(r.season, "2025-2026")

    def test_el_pdf_nuevo_se_declara_de_2026_27(self):
        r = rfef_web.read_pdf_roster(PDF_G2_NUEVO.read_bytes())
        self.assertEqual(r.season, "2026-2027")

    def test_los_dos_pdf_no_traen_los_mismos_equipos(self):
        """Que sirva de recordatorio de por qué importa: no son el mismo
        calendario con otra portada, son dos ligas distintas."""
        viejo = set(rfef_web.read_pdf_roster(PDF_G2_VIEJO.read_bytes()).teams)
        nuevo = set(rfef_web.read_pdf_roster(PDF_G2_NUEVO.read_bytes()).teams)
        self.assertNotEqual(viejo, nuevo)


class ElPlantelDelPdf(unittest.TestCase):
    def test_formato_numerado(self):
        """Generador clásico: `1.- AECS L Hospitalet (115378)`."""
        r = rfef_web.read_pdf_roster(PDF_G2_NUEVO.read_bytes())
        self.assertEqual(len(r.teams), 16)
        self.assertIn("AECS L Hospitalet", r.teams)
        self.assertIn("Feme Castellón C.F.S.", r.teams)

    def test_formato_a_dos_columnas(self):
        """Generador nuevo ("Creación de Calendario"): el plantel va a dos
        columnas y sin numerar. En texto plano las dos columnas quedan pegadas,
        y ahí es donde el parser viejo se inventaba equipos."""
        r = rfef_web.read_pdf_roster(PDF_SEGUNDA_B_G4.read_bytes())
        self.assertEqual(len(r.teams), 16)
        self.assertIn("CD Albacete FS", r.teams)
        self.assertIn("Club Unión Deportiva Loeches", r.teams)
        pegados = [t for t in r.teams if "Albacete" in t and "Loeches" in t]
        self.assertEqual(pegados, [], "dos columnas leídas como un solo equipo")

    def test_el_grupo_sale_del_numero_impreso(self):
        r = rfef_web.read_pdf_roster(PDF_SEGUNDA_B_G4.read_bytes())
        self.assertEqual(r.group_label, "Grupo 4")
        self.assertEqual(r.group_id, "g4")

    def test_grupo_unico_no_inventa_id(self):
        """`único` es una división plana: darle un `g1` haría que la app pidiera
        elegir un grupo que no existe."""
        self.assertIsNone(rfef_web.group_id_from_label("único"))
        self.assertIsNone(rfef_web.group_id_from_label(None))


class LaCabeceraDeLigaPrime(unittest.TestCase):
    """El PDF de la máxima categoría masculina va en otro formato: no dice
    "Temporada" y no lleva grupo. Aun así hay que sacarle las dos cosas que
    importan — de qué competición es y de qué año."""

    def test_lee_competicion_y_temporada(self):
        r = rfef_web.read_pdf_roster(PDF_LIGA_PRIME.read_bytes())
        self.assertEqual(r.season, "2026-2027")
        self.assertEqual(r.competition, "Liga Prime Futsal")


class DeQueDivisionEsEstePdf(unittest.TestCase):
    """Una página de competición enlaza las noticias de calendario de media
    federación: desde Segunda B se llega a los ocho PDF de División de Honor
    Juvenil. Filtrar por quién enlazó el fichero sería fiarse otra vez de la
    ruta; se filtra por lo que el documento dice ser."""

    def _roster(self, competition):
        return rfef_web.PdfRoster(
            season="2026-2027", competition=competition,
            group_label=None, group_id=None, teams=["X"])

    def test_reconoce_las_nuestras(self):
        casos = {
            "Liga Prime Futsal": "rfef-primera-fs-masc",
            "Segunda División Fútbol Sala Masculino (OPCIÓN 1)":
                "rfef-segunda-fs-masc",
            "Segunda División B Fútbol Sala": "rfef-segunda-b-fs-masc",
            "Segunda División Fútbol Sala Femenino": "rfef-segunda-fs-fem",
        }
        for nombre, div_id in casos.items():
            with self.subTest(nombre=nombre):
                self.assertEqual(
                    rfef_web.roster_division(self._roster(nombre)), div_id)

    def test_descarta_lo_que_no_es_una_de_nuestras_ligas(self):
        for nombre in ("División de Honor Juvenil Fútbol Sala",
                       "Copa de España Fútbol Sala",
                       "Campeonato de Selecciones Autonómicas Sub 16"):
            with self.subTest(nombre=nombre):
                self.assertIsNone(
                    rfef_web.roster_division(self._roster(nombre)))


class EmparejarNombreCortoYOficial(unittest.TestCase):
    """La entrenadora ve el corto; el oficial viaja para casar con el calendario
    y con los partidos ya guardados."""

    def test_casa_pese_al_patrocinador(self):
        pares = rfef_web.pair_names(
            ["Burela FS", "Alzira FS"],
            ["REYCO Burela FS", "Family Cash Alzira F.S."])
        self.assertEqual(pares["REYCO Burela FS"], "Burela FS")
        self.assertEqual(pares["Family Cash Alzira F.S."], "Alzira FS")

    def test_el_desempate_no_le_roba_el_nombre_al_vecino(self):
        """Los dos oficiales contienen "Granada FS" entero, así que la
        cobertura satura en 1,0 para ambos. Quien decide es el ajuste."""
        pares = rfef_web.pair_names(
            ["Granada FS", "Almagro FSF"],
            ["Granada FS Femenino", "Fundación UAPO Granada FS Femenino",
             "Almagro F.S.F."])
        self.assertEqual(pares["Granada FS Femenino"], "Granada FS")
        self.assertNotIn("Fundación UAPO Granada FS Femenino", pares)

    def test_no_inventa_pareja(self):
        """Sin candidato razonable se queda sin par, y el caller publica el
        nombre oficial. Renombrar al rival por un parecido le rompe el
        histórico a quien ya jugó contra él."""
        pares = rfef_web.pair_names(
            ["Futsi Atlético B"], ["Atletico Navalcarnero"])
        self.assertEqual(pares, {})

    def test_las_palabras_de_todos_no_emparejan_a_nadie(self):
        """"CD" y "FS" las lleva medio fútbol sala español: si contaran, dos
        clubes cualesquiera casarían."""
        pares = rfef_web.pair_names(["CD Leganés FS"], ["C.D. Melistar"])
        self.assertEqual(pares, {})


def _word(text, x0, width=20.0, top=100.0):
    return {"text": text, "x0": x0, "width": width, "top": top}


class ElCalendarioADosColumnas(unittest.TestCase):
    """Los calendarios de la RFEF ponen la ida a la izquierda y la vuelta a la
    derecha, con la J1 y la J16 empezando en la misma línea. Leídos como texto
    plano, cada línea era un partido inventado entre el visitante de la ida y el
    local de la vuelta: 240 nombres distintos para un grupo de 16 equipos."""

    def _cal(self, path):
        return rfef_web.parse_calendar_multicolumn(path.read_bytes())

    def test_generador_clasico_completo(self):
        cal = self._cal(PDF_G2_NUEVO)
        self.assertEqual(len(cal), 30)
        self.assertTrue(all(len(j["matches"]) == 8 for j in cal))

    def test_generador_nuevo_completo(self):
        cal = self._cal(PDF_SEGUNDA_B_G4)
        self.assertEqual(len(cal), 30)
        self.assertTrue(all(len(j["matches"]) == 8 for j in cal))

    def test_los_equipos_del_calendario_son_los_del_plantel(self):
        """La prueba que de verdad detecta el pegado de columnas: si vuelve a
        ocurrir, aquí salen 240 nombres en vez de 16."""
        for pdf in (PDF_G2_NUEVO, PDF_SEGUNDA_B_G4):
            with self.subTest(pdf=pdf.name):
                roster = set(rfef_web.read_pdf_roster(pdf.read_bytes()).teams)
                cal = self._cal(pdf)
                nombres = {m[k] for j in cal for m in j["matches"]
                           for k in ("home", "away")}
                self.assertEqual(nombres, roster)

    def test_la_ida_y_la_vuelta_no_se_mezclan(self):
        """La J1 y la J16 comparten línea. Si la columna se asignara mal, los
        partidos de la vuelta caerían en la jornada de la ida."""
        cal = {j["jornada"]: j for j in self._cal(PDF_G2_NUEVO)}
        j1 = cal[1]["matches"][0]
        self.assertEqual(j1["home"], "AECS L Hospitalet")
        self.assertEqual(j1["away"], 'Club Deportivo Nou Turia FSF "A"')
        # La vuelta es el mismo partido del revés.
        j16 = cal[16]["matches"][0]
        self.assertEqual(j16["home"], 'Club Deportivo Nou Turia FSF "A"')
        self.assertEqual(j16["away"], "AECS L Hospitalet")

    def test_la_fecha_va_tambien_en_cada_partido(self):
        """El cliente lee la fecha de `CalendarMatch`, no de la jornada. Un
        calendario que solo la llevara arriba llegaba a la app sin fecha."""
        cal = self._cal(PDF_G2_NUEVO)
        self.assertEqual(cal[0]["date"], "2026-09-19")
        self.assertTrue(all(m["date"] == "2026-09-19" for m in cal[0]["matches"]))
        self.assertEqual(cal[-1]["date"], "2027-05-22")

    def test_un_pdf_de_una_columna_no_lo_toca(self):
        """Liga Prime separa local y visitante solo por el hueco, sin guion.
        Devolver `[]` es lo que deja que siga el parser de siempre."""
        self.assertEqual(self._cal(PDF_LIGA_PRIME), [])


class DetectarLasColumnasPorElGuion(unittest.TestCase):
    def _lines(self, pdf):
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf.read_bytes())) as doc:
            return [rfef_web._lines_of(p) for p in doc.pages]

    def test_dos_columnas_en_los_calendarios_de_grupo(self):
        for pdf in (PDF_G2_NUEVO, PDF_SEGUNDA_B_G4):
            with self.subTest(pdf=pdf.name):
                self.assertEqual(len(rfef_web.separator_columns(self._lines(pdf))), 2)

    def test_el_guion_de_un_nombre_no_es_una_columna(self):
        """"Entreparéntesis - Enertel FS Talavera" lleva su guion siempre a la
        misma x, porque su línea empieza donde empieza la columna. Con un umbral
        absoluto se colaba como tercera columna y la mitad de las jornadas se
        quedaban sin un solo partido."""
        seps = rfef_web.separator_columns(self._lines(PDF_SEGUNDA_B_G4))
        self.assertEqual(len(seps), 2)
        self.assertTrue(all(abs(s - 373) > 20 for s in seps))

    def test_sin_guiones_no_hay_columnas(self):
        self.assertEqual(rfef_web.separator_columns([[[_word("Barça", 10)]]]), [])


class LaCabeceraDeJornadaVaPorSuX(unittest.TestCase):
    """En el calendario de Segunda las dos cabeceras no están alineadas, así
    que "Jornada 16" cae en su propia línea. Por orden de aparición se le
    asignaba la columna izquierda, y quince jornadas se quedaban vacías."""

    SEPS = [159.0, 439.0]

    def test_cabecera_suelta_de_la_columna_derecha(self):
        words = [_word("Jornada", 402), _word("16", 428),
                 _word("(16-01-2027)", 441)]
        self.assertEqual(rfef_web.headers_in_line(words, self.SEPS),
                         [(1, 16, "2027-01-16")])

    def test_las_dos_cabeceras_en_la_misma_linea(self):
        words = [_word("Jornada", 124), _word("1", 150), _word("-", 155),
                 _word("(19-09-2026)", 159),
                 _word("Jornada", 402), _word("16", 428), _word("-", 437),
                 _word("(16-01-2027)", 441)]
        self.assertEqual(rfef_web.headers_in_line(words, self.SEPS),
                         [(0, 1, "2026-09-19"), (1, 16, "2027-01-16")])

    def test_una_linea_de_partido_no_es_cabecera(self):
        words = [_word("CD", 171), _word("Albacete", 182), _word("FS", 208)]
        self.assertEqual(rfef_web.headers_in_line(words, self.SEPS), [])


class LosCodigosDeLaPnfgQueTraeLaPagina(unittest.TestCase):
    """La página de competición enlaza su calendario en la PNFG con los tres
    códigos dentro. Rescata el código de grupo cuando el catálogo de la PNFG
    —su llamada más frágil— se come el rate-limit.

    Los enlaces son los reales del 2026-08-29, y su desigualdad es el motivo del
    guard: Segunda y Primera Femenina apuntaban a `CodTemporada=22` (2026-27) y
    Segunda B a `CodTemporada=21`, y encima al playoff de ascenso del año pasado.
    """

    SEGUNDA = ('href="https://resultados.rfef.es/pnfg/NPcd/NFG_CmpJornada?'
               'cod_primaria=1000120&amp;CodCompeticion=33918407&amp;'
               'CodGrupo=33918408&amp;CodTemporada=22&amp;CodJornada=1"')
    SEGUNDA_B = ('href="https://resultados.rfef.es/pnfg/NPcd/NFG_CmpJornada?'
                 'cod_primaria=1000120&amp;CodCompeticion=33575532&amp;'
                 'CodGrupo=33718393&amp;CodTemporada=21&amp;CodJornada=2"')

    def test_lee_los_tres_codigos(self):
        self.assertEqual(rfef_web.parse_pnfg_links(self.SEGUNDA),
                         [("33918407", "33918408", "22")])

    def test_devuelve_el_grupo_de_la_temporada_pedida(self):
        self.assertEqual(
            rfef_web.pnfg_group_for(self.SEGUNDA, "33918407", "22"), "33918408")

    def test_ignora_el_enlace_de_la_temporada_pasada(self):
        self.assertIsNone(
            rfef_web.pnfg_group_for(self.SEGUNDA_B, "33575532", "22"))

    def test_ignora_el_enlace_de_otra_competicion(self):
        """El de Segunda B apuntaba al playoff de ascenso. Exigir que la
        competición sea la ya descubierta impide que se cuele como si fuera
        la liga."""
        self.assertIsNone(
            rfef_web.pnfg_group_for(self.SEGUNDA_B, "33836163", "21"))

    def test_la_pagina_real_de_segunda_femenina_no_sirve(self):
        html = PAGINA_SEGUNDA_FEM.read_text(encoding="utf-8")
        # El recorte de la fixture no lleva el bloque de resultados, así que
        # aquí lo que se comprueba es que la ausencia no revienta nada.
        self.assertIsNone(rfef_web.pnfg_group_for(html, "33836181", "22"))


class FasesQueNoSonGrupos(unittest.TestCase):
    """Desde 2026-27 la PNFG expone "Torneo Apertura" y "Torneo Clausura" por el
    mismo `<select>` que los grupos territoriales. La diferencia es que las
    fases tienen los **mismos** 16 equipos: pedirle a la entrenadora que elija
    una es pedirle una decisión que no existe."""

    def test_reconoce_una_fase(self):
        self.assertTrue(is_phase("Torneo Apertura"))
        self.assertTrue(is_phase("Torneo Clausura"))

    def test_un_grupo_territorial_no_es_una_fase(self):
        self.assertFalse(is_phase("Grupo 1"))
        self.assertFalse(is_phase("Grupo 4"))

    def test_el_grupo_unico_con_nombre_largo_tampoco(self):
        self.assertFalse(
            is_phase("Primera División Fútbol Sala Femenino 2026-27"))


if __name__ == "__main__":
    unittest.main()
