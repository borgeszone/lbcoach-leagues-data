"""Tests del PDF oficial de la RFEF como fuente de equipos y calendario.

La fixture es el PDF **real** publicado por la RFEF el 29/06/2026 con el
calendario de Liga Prime Futsal 2026-27. Importa que sea el de verdad: su
formato de cabecera y su nombre de fichero cambiaron respecto a los PDFs
clásicos, y ese cambio es justo lo que estos tests protegen.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers import rfef  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIGA_PRIME = FIXTURES / "calendario_liga-prime-futsal_2026-2027.pdf"


def pdf_bytes() -> bytes:
    return LIGA_PRIME.read_bytes()


class GuardDeTemporadaEnElPdf(unittest.TestCase):
    """La URL de este PDF **no** lleva la temporada
    (`2026-06/Liga_Prime_Futsal.pdf`), así que la ruta no puede avalarlo. Lo que
    lo avala es la portada, que sí la imprime."""

    def test_reconoce_su_temporada(self):
        self.assertTrue(rfef._pdf_declares_season(pdf_bytes(), "2026-2027"))

    def test_rechaza_otra_temporada(self):
        self.assertFalse(rfef._pdf_declares_season(pdf_bytes(), "2025-2026"))
        self.assertFalse(rfef._pdf_declares_season(pdf_bytes(), "2027-2028"))

    def test_basura_no_pasa_por_buena(self):
        self.assertFalse(rfef._pdf_declares_season(b"no soy un pdf", "2026-2027"))


class CandidatosDePdf(unittest.TestCase):
    def test_el_patron_clasico_va_primero(self):
        """Lleva la temporada dentro de la URL: si existe, no hay ambigüedad."""
        urls = rfef._pdf_candidates(
            rfef.LEGACY_PDF["rfef-primera-fs-masc"], "2026-2027")
        self.assertIn("Calendario_1Div_Sala_2026-2027.pdf", urls[0])
        self.assertIn("Liga_Prime_Futsal.pdf", urls[1])

    def test_la_url_suelta_esta_atada_a_su_temporada(self):
        """Indexada por temporada a propósito: una URL sin temporada en el
        nombre no puede reutilizarse para otro año ni por accidente."""
        urls = rfef._pdf_candidates(
            rfef.LEGACY_PDF["rfef-primera-fs-masc"], "2027-2028")
        self.assertTrue(all("Liga_Prime_Futsal.pdf" not in u for u in urls))

    def test_division_sin_pdf_no_da_candidatos(self):
        self.assertEqual(rfef._pdf_candidates({}, "2026-2027"), [])


class ParseoDelCalendario(unittest.TestCase):
    def test_las_dos_fases_con_sus_quince_jornadas(self):
        cal = rfef._extract_calendar_from_pdf(pdf_bytes())
        self.assertEqual(len(cal), 30)
        ap = [j for j in cal if j.get("phase") == "Apertura"]
        cl = [j for j in cal if j.get("phase") == "Clausura"]
        self.assertEqual(len(ap), 15)
        self.assertEqual(len(cl), 15)
        self.assertEqual(sum(len(j["matches"]) for j in cal), 240)

    def test_las_dos_jornadas_1_no_se_pisan(self):
        """Clave (fase, jornada). Con una sola clave numérica, la J1 de Clausura
        sobrescribiría la de Apertura y se perderían quince jornadas sin ruido."""
        cal = rfef._extract_calendar_from_pdf(pdf_bytes())
        unos = [j for j in cal if j["jornada"] == 1]
        self.assertEqual(len(unos), 2)
        self.assertEqual({j["phase"] for j in unos}, {"Apertura", "Clausura"})
        self.assertNotEqual(unos[0]["date"], unos[1]["date"])

    def test_orden_de_competicion_no_de_numero(self):
        """Ordenar por número mezclaría las fases (A1, C1, A2, C2…)."""
        cal = rfef._extract_calendar_from_pdf(pdf_bytes())
        fases = [j["phase"] for j in cal]
        self.assertEqual(fases, ["Apertura"] * 15 + ["Clausura"] * 15)

    def test_fechas_y_enfrentamientos(self):
        cal = rfef._extract_calendar_from_pdf(pdf_bytes())
        self.assertEqual(cal[0]["date"], "2026-09-13")
        self.assertEqual(cal[-1]["date"], "2027-05-16")
        m = cal[0]["matches"][0]
        self.assertEqual(m["home"], "Jimbee Cartagena Costa Cálida")
        self.assertEqual(m["away"], "Inter JP Financial")

    def test_los_dieciseis_equipos(self):
        eq = rfef._extract_teams_from_pdf(pdf_bytes())
        self.assertEqual(len(eq), 16)
        self.assertIn("Barça", eq)
        self.assertIn("Inter JP Financial", eq)


class RellenoDeDivisionesSinPnfg(unittest.TestCase):
    """`_fill_missing_from_pdf`: el hueco que hacía que el PDF no se intentara
    nunca para una división ausente de la PNFG — que es justo el caso donde es
    la única fuente que hay."""

    def _run(self, *, declares=True, teams_only=False, divs=None):
        warnings = []
        out = divs if divs is not None else [
            {"id": "rfef-primera-fs-masc", "name": "1a", "gender": "masculino",
             "teams": []}]
        with mock.patch.object(rfef, "_download_pdf", return_value=pdf_bytes()), \
             mock.patch.object(rfef, "_pdf_declares_season", return_value=declares), \
             mock.patch.object(rfef, "resolve_logo_url", return_value=None), \
             mock.patch.object(rfef, "lookup_override", return_value=None):
            rfef._fill_missing_from_pdf(out, "2026-2027", True, warnings, teams_only)
        return out, warnings

    def test_rellena_equipos_y_calendario(self):
        out, warnings = self._run()
        self.assertEqual(len(out[0]["teams"]), 16)
        self.assertEqual(len(out[0]["calendar"]), 30)
        self.assertTrue(any("PDF oficial" in w for w in warnings))

    def test_en_modo_rapido_no_pone_calendario(self):
        """Lo hereda `scrape.py` del publicado; ponerlo aquí lo pisaría."""
        out, _ = self._run(teams_only=True)
        self.assertEqual(len(out[0]["teams"]), 16)
        self.assertNotIn("calendar", out[0])

    def test_si_el_pdf_es_de_otra_temporada_no_se_usa(self):
        out, warnings = self._run(declares=False)
        self.assertEqual(out[0]["teams"], [])
        self.assertTrue(any("no declara" in w for w in warnings))

    def test_no_pisa_una_division_que_ya_tiene_equipos(self):
        divs = [{"id": "rfef-primera-fs-masc", "name": "1a",
                 "gender": "masculino", "teams": [{"name": "De la PNFG"}]}]
        out, warnings = self._run(divs=divs)
        self.assertEqual([t["name"] for t in out[0]["teams"]], ["De la PNFG"])
        self.assertEqual(warnings, [])

    def test_division_sin_pdf_configurado_se_queda_vacia(self):
        divs = [{"id": "rfef-segunda-b-fs-masc", "name": "2aB",
                 "gender": "masculino", "teams": []}]
        out, warnings = self._run(divs=divs)
        self.assertEqual(out[0]["teams"], [])
        self.assertEqual(warnings, [])


class RegresionFormatoAntiguo(unittest.TestCase):
    """Los PDFs clásicos (`Jornada 1 (06/09/2025)`, sin fase) tienen que seguir
    parseándose igual. La regex nueva es más permisiva y podría haberlos roto."""

    def test_cabecera_clasica_sigue_casando(self):
        import re
        rx = re.compile(
            r"^(?:Torneo\s+(\w+)\s+)?J(?:ornada)?\s*(\d+)\s*"
            r"(?:\((\d{1,2}/\d{1,2}/\d{2,4})\))?", re.IGNORECASE)
        m = rx.match("Jornada 7 (06/09/2025)")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group(1))
        self.assertEqual(m.group(2), "7")
        self.assertEqual(m.group(3), "06/09/2025")

    def test_un_nombre_de_equipo_con_j_no_es_cabecera(self):
        """`J\\s*(\\d+)` sin cuidado convertiría "Jaén Paraiso" en jornada."""
        import re
        rx = re.compile(
            r"^(?:Torneo\s+(\w+)\s+)?J(?:ornada)?\s*(\d+)\s*"
            r"(?:\((\d{1,2}/\d{1,2}/\d{2,4})\))?", re.IGNORECASE)
        for nombre in ["Jaén Paraiso Interior FS.", "Jimbee Cartagena Costa Cálida"]:
            with self.subTest(nombre=nombre):
                self.assertIsNone(rx.match(nombre))


if __name__ == "__main__":
    unittest.main()
