"""La allowlist curada de escudos (`data/badges-overrides.json`).

Lo que se protege aquí son dos cosas que se rompen en silencio.

**Que las entradas se sigan leyendo.** El fichero pasó de `{clave: "url"}` a
`{clave: {url, fuente, titular, licencia, verificado}}` para poder registrar la
procedencia que pide `IP-004`. Si el loader deja de entender una de las dos
formas, `resolve_logo_url` devuelve None, el JSON se publica sin escudos y
**nada falla**: la app enseña su placeholder genérico, que es exactamente lo que
enseñaría si el club no tuviera escudo.

**Que los 47 escudos migrados sigan aquí.** Vivían dentro de
`data/rfef-fallback.json`, que desde agosto de 2026 se ignora entero si su
`season` no coincide con la que se scrapea (§6.32). En el camino del calendario
—el que se usa antes de que se juegue la J1, que es cuando el scraper hace
falta— la resolución solo lee este fichero, el mapa del portal y la caché de
runs anteriores, así que devolverlas al fallback las vuelve inalcanzables sin
que ningún test ni ningún warning lo diga.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers import logo_resolver  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def _reset_resolver() -> None:
    """El resolver cachea overrides y caché en globals del módulo."""
    logo_resolver._overrides = None
    logo_resolver._cache = None
    logo_resolver._rfef_shields = {}


class OverrideShapeTest(unittest.TestCase):
    """Las dos formas de una entrada, y las que hay que tolerar sin reventar."""

    def test_objeto_con_procedencia(self):
        self.assertEqual(
            logo_resolver._override_url(
                {"url": "https://x/1.png", "fuente": "lnfs"}
            ),
            "https://x/1.png",
        )

    def test_cadena_suelta_legacy(self):
        self.assertEqual(
            logo_resolver._override_url("https://x/2.png"), "https://x/2.png"
        )

    def test_entrada_a_medias_no_revienta(self):
        # Este fichero se edita a mano: una entrada sin `url` tiene que costar
        # un escudo, no el run entero.
        for valor in ({}, {"fuente": "lnfs"}, {"url": ""}, {"url": None}, "", None, 42):
            self.assertIsNone(logo_resolver._override_url(valor), repr(valor))

    def test_espacios_alrededor_de_la_url(self):
        self.assertEqual(
            logo_resolver._override_url({"url": "  https://x/3.png\n"}),
            "https://x/3.png",
        )


class AllowlistFileTest(unittest.TestCase):
    """El fichero real, tal como está en el repo."""

    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(
            (DATA / "badges-overrides.json").read_text(encoding="utf-8")
        )
        cls.entries = {k: v for k, v in cls.raw.items() if not k.startswith("_")}

    def test_todas_las_entradas_resuelven_a_una_url(self):
        for key, value in self.entries.items():
            with self.subTest(key=key):
                url = logo_resolver._override_url(value)
                self.assertIsNotNone(url)
                self.assertTrue(url.startswith("https://"), url)

    def test_todas_declaran_procedencia(self):
        # Es el punto 4 del plan de IP-004: origen, titular y licencia por
        # entrada. Sin esto el fichero vuelve a ser una lista de URLs sin saber
        # de dónde salió ninguna.
        for key, value in self.entries.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, dict)
                for campo in ("fuente", "titular", "licencia", "verificado"):
                    self.assertIn(campo, value)
                self.assertIn(value["fuente"], ("lnfs", "pnfg"))

    def test_ninguna_entrada_viene_de_wikimedia_ni_de_buscadores(self):
        # IP-004 manda quitar Wikipedia y DuckDuckGo por procedencia
        # desconocida. Una entrada curada que venga de ahí reintroduce por la
        # puerta de atrás justo lo que se está retirando.
        prohibidos = ("wikimedia", "wikipedia", "duckduckgo", "bing", "google")
        for key, value in self.entries.items():
            url = logo_resolver._override_url(value).lower()
            with self.subTest(key=key):
                for dominio in prohibidos:
                    self.assertNotIn(dominio, url)

    def test_las_claves_estan_normalizadas(self):
        # Una clave con mayúsculas o acentos no casa nunca: la búsqueda
        # normaliza el nombre del equipo, no la clave.
        for key in self.entries:
            self.assertEqual(key, logo_resolver._norm(key), key)


class MigracionDelFallbackTest(unittest.TestCase):
    """Los 47 escudos que estaban atrapados en el fallback por temporada."""

    @classmethod
    def setUpClass(cls):
        cls.fallback = json.loads(
            (DATA / "rfef-fallback.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _fallback_teams_con_escudo(fallback):
        out = []
        for div in fallback.get("divisions", {}).values():
            equipos = list(div.get("teams") or [])
            for grupo in div.get("groups") or []:
                equipos.extend(grupo.get("teams") or [])
            out.extend(t for t in equipos if t.get("logoUrl"))
        return out

    def test_cada_escudo_del_fallback_esta_en_la_allowlist(self):
        _reset_resolver()
        for team in self._fallback_teams_con_escudo(self.fallback):
            with self.subTest(team=team["name"]):
                # `lookup_override` es lo que consulta el camino de la
                # clasificación; `resolve_logo_url(trusted_only=True)` el del
                # calendario. Los dos tienen que encontrarlo.
                #
                # Se comprueba que sea una **cadena**, no solo que no sea None:
                # si el loader dejara pasar la entrada en crudo, esto devolvería
                # el dict entero —que es igual de "no None"— y acabaría dentro
                # del JSON como `logoUrl`, con la app pidiendo una URL que es un
                # objeto.
                for got in (
                    logo_resolver.lookup_override(team["name"]),
                    logo_resolver.resolve_logo_url(team["name"], trusted_only=True),
                ):
                    self.assertIsInstance(got, str)
                    self.assertTrue(got.startswith("https://"), got)

    def test_la_allowlist_no_depende_de_la_temporada(self):
        # El fallback declara `season` y se ignora entero si no coincide. Este
        # fichero no puede tener ese campo, o los escudos volverían a caducar
        # cada julio.
        self.assertIn("season", self.fallback)
        raw = json.loads((DATA / "badges-overrides.json").read_text(encoding="utf-8"))
        self.assertNotIn("season", raw)


class PrimeraMasculinaTest(unittest.TestCase):
    """Los 16 clubes de Primera masculina, que es la division que se publicaba
    con 6 escudos de 16.

    No salen de la clasificacion —no existe hasta que se juega la J1— sino del
    PDF oficial, que no trae imagenes. Asi que cada escudo sale o del mapa del
    portal (seis, los que casan por nombre desde la home) o de la allowlist.

    Los nombres son los del PDF de 2026-27 y **no** son los del fallback
    curado: ahi seguian "Movistar Inter FS", "Industrias Santa Coloma", "Real
    Betis Futsal" y "ATP Iluminacion Tudelano", que ni juegan esta temporada ni
    se llaman ya asi. Es el recordatorio de que la clave es el nombre publicado,
    no el club.
    """

    DESDE_EL_PORTAL = {
        "Barça": "18",
        "ElPozo Murcia Costa Cálida": "22",
        "Jimbee Cartagena Costa Cálida": "21",
        "Illes Balears Palma Futsal": "30",
        "Viña Albali Valdepeñas": "32",
        "Noia Portus Apostoli": "10",
    }

    # Los diez restantes del PDF de 2026-27, que dependen solo de la allowlist.
    DESDE_LA_ALLOWLIST = [
        "C.A. Osasuna Magna",
        "Córdoba Patrimonio De La Humanidad",
        "FS García",
        "Inter JP Financial",
        "Jaén Paraiso Interior FS.",
        "O Parrulo Ferrol FS.",
        "Quesos El Hidalgo Manzanares FS",
        "Servigroup Peñiscola FS",
        "Wanapix AD Sala 10",
    ]

    # Nombres con los que estos mismos clubes aparecieron en temporadas
    # anteriores. Se conservan a proposito: el PDF de una division distinta o de
    # otro año puede seguir usandolos, y borrarlos no ahorra nada.
    ALIAS_DE_TEMPORADAS_ANTERIORES = [
        "Movistar Inter FS",
        "Industrias Santa Coloma",
        "Catgas Energia Santa Coloma",
        "CD Xota",
        "Jaén Paraíso Interior FS",
        "Real Betis Futsal",
        "Servigroup Peñíscola FS",
        "ATP Iluminación Tudelano Ribera de Navarra",
    ]

    SHIELD = "https://futsal.rfef.es/media/lnfs/shields_futsal/png/{id}.png"

    def test_los_que_no_casan_en_el_portal_salen_de_la_allowlist(self):
        # Sin inyectar el mapa: es el estado de un run en el que
        # futsal.rfef.es no responda, y estos diez tienen que aguantar solos.
        _reset_resolver()
        faltan = [
            n
            for n in self.DESDE_LA_ALLOWLIST
            if not isinstance(logo_resolver.lookup_override(n), str)
        ]
        self.assertEqual(faltan, [])

    def test_con_el_portal_delante_siguen_resolviendo_todos(self):
        _reset_resolver()
        logo_resolver.inject_rfef_shields(
            {
                logo_resolver._norm(name): self.SHIELD.format(id=shield_id)
                for name, shield_id in self.DESDE_EL_PORTAL.items()
            }
        )
        todos = list(self.DESDE_EL_PORTAL) + self.DESDE_LA_ALLOWLIST
        faltan = [
            n
            for n in todos
            if not isinstance(
                logo_resolver.resolve_logo_url(n, trusted_only=True), str
            )
        ]
        self.assertEqual(faltan, [])

    def test_los_nombres_viejos_siguen_resolviendo(self):
        _reset_resolver()
        faltan = [
            n
            for n in self.ALIAS_DE_TEMPORADAS_ANTERIORES
            if not isinstance(logo_resolver.lookup_override(n), str)
        ]
        self.assertEqual(faltan, [])

    def test_el_hueco_conocido_esta_documentado(self):
        # "Sur Seed CD El Ejido Futsal" no tiene ficha en el portal, asi que se
        # queda con el placeholder. Lo que se protege aqui no es el escudo sino
        # que el hueco siga anotado: sin la nota, dentro de un año nadie sabra
        # si falta porque no existe o porque se olvido.
        _reset_resolver()
        self.assertIsNone(logo_resolver.lookup_override("Sur Seed CD El Ejido Futsal"))
        raw = json.loads((DATA / "badges-overrides.json").read_text(encoding="utf-8"))
        self.assertIn("El Ejido", raw.get("_pendientes", ""))


class FuentesDeConfianzaTest(unittest.TestCase):
    """La garantía que sustituye a la cascada retirada.

    Wikipedia y DuckDuckGo salieron por `IP-004` (procedencia desconocida). Lo
    que impide que vuelvan a entrar no es que ya no se llamen, sino que **nada
    que no venga de un dominio de la federación puede quedarse en la caché**, ni
    al leerla ni al escribirla. Sin esa validación, la purga de agosto de 2026
    sería un apaño de una vez: bastaría con recuperar un fichero de caché viejo
    del historial de git para reintroducir las URLs.
    """

    def test_acepta_los_dos_dominios_de_la_federacion(self):
        for url in (
            "https://futsal.rfef.es/media/lnfs/shields_futsal/png/18.png",
            "https://rfef.filesnovanet.es/pnfg/pimg/Equipos/x.jpg",
        ):
            self.assertTrue(logo_resolver._is_trusted_url(url), url)

    def test_rechaza_lo_que_IP_004_manda_retirar(self):
        for url in (
            "https://commons.wikimedia.org/wiki/Special:FilePath/Escudo.svg",
            "https://upload.wikimedia.org/wikipedia/commons/a/b/Escudo.png",
            "https://external-content.duckduckgo.com/iu/?u=x",
            "https://i.imgur.com/x.png",
            "",
            None,
            42,
        ):
            self.assertFalse(logo_resolver._is_trusted_url(url), repr(url))

    def test_no_basta_con_que_el_dominio_aparezca_en_la_url(self):
        # El agujero clásico de esta comprobación: un `in` sobre la cadena deja
        # pasar cualquier host que lleve el dominio bueno en la ruta o pegado
        # al nombre.
        for url in (
            "https://malo.example.com/futsal.rfef.es/escudo.png",
            "https://futsal.rfef.es.malo.example.com/escudo.png",
            "https://notfutsal.rfef.es.evil/escudo.png",
        ):
            self.assertFalse(logo_resolver._is_trusted_url(url), url)

    def test_un_subdominio_de_la_federacion_si_vale(self):
        self.assertTrue(
            logo_resolver._is_trusted_url("https://cdn.futsal.rfef.es/x.png")
        )

    def test_la_cache_en_disco_solo_tiene_urls_oficiales(self):
        raw = json.loads((DATA / "badges-cache.json").read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            with self.subTest(key=key):
                self.assertTrue(logo_resolver._is_trusted_url(value), value)

    def test_el_mapa_del_portal_se_filtra_al_inyectarlo(self):
        # El mapa lo construye un parser sobre HTML de terceros, y de él salen
        # las escrituras que se persisten en la caché.
        _reset_resolver()
        logo_resolver.inject_rfef_shields({
            "bueno": "https://futsal.rfef.es/media/lnfs/shields_futsal/png/9.png",
            "malo": "https://commons.wikimedia.org/wiki/Special:FilePath/x.svg",
        })
        self.assertIn("bueno", logo_resolver._rfef_shields)
        self.assertNotIn("malo", logo_resolver._rfef_shields)

    def test_ya_no_hay_ninguna_fuente_de_red_en_el_resolver(self):
        # Si alguien vuelve a meter una búsqueda automática, el import reaparece
        # y este caso lo caza antes de que llegue a publicarse nada.
        fuente = (
            pathlib.Path(logo_resolver.__file__)
            .read_text(encoding="utf-8")
            .lower()
        )
        codigo = "\n".join(
            l for l in fuente.splitlines()
            if not l.lstrip().startswith("#")
        )
        for prohibido in ("import requests", "requests.get", "duckduckgo.com",
                          "es.wikipedia.org", "api.php"):
            self.assertNotIn(prohibido, codigo, prohibido)


if __name__ == "__main__":
    unittest.main()
