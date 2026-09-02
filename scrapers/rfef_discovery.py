"""Descubrimiento de competiciones RFEF en la plataforma PNFG.

Antes, los códigos `comp`/`grupo` de cada división vivían escritos a mano en
`rfef.py`. Eso escondía un fallo que solo aparece una vez al año, y en
silencio: en la PNFG **el código de competición es por temporada**. Los de
2025-26 siguen respondiendo en agosto de 2026 —con los equipos de 2025-26—, así
que el scraper se traía la liga del año pasado y `scrape.py` la publicaba con
el sello de la nueva. Ni un error, ni un aviso.

Aquí se le pregunta al servidor, en cada run, qué tiene la temporada pedida:

    1. `NFG_CmpJornada`        <select name=temporada>   "2026-2027" → 22
    2. `NFG_CmpJornada_Exec`   competiciones de esa temporada
       ?codtemporada=22&Sch_Cod_Agrupacion=900160072&Sch_Tipo_Juego=3
    3. `NFG_CmpJornada_Exec`   grupos de una competición
       ?codcompeticion=33836181 → 33836182 Grupo 1, 33836183 Grupo 2, ...

## Lo único que sigue escrito a mano, y por qué

El **nombre** de cada competición se casa con un id de división estable
(`rfef-segunda-fs-fem`) mediante `DIVISION_RULES`. Esos ids no se pueden
generar: la app los persiste dentro de cada equipo (`Team.divisionId`,
`Team.groupId`) y de ellos cuelgan el calendario, la importación de rivales y
las novedades de federación. Un id nuevo cada temporada dejaría a **todos** los
equipos existentes sin liga.

Se casa por nombre y no por código porque los nombres aguantan de una temporada
a otra; los códigos son justo lo que no.

## Dos trampas del HTML que devuelve la PNFG

- **Las subcompeticiones vienen anidadas dentro de la principal**, con `<option>`
  sin cerrar:

      <option value='23289365'>Segunda División B Fútbol Sala
        <option value='33575532'> - PlayOff de Ascenso a Segunda División</option>
      </option>

  Un parser de HTML "arregla" ese anidamiento de formas distintas según la
  librería, así que aquí se leen las opciones con una expresión regular sobre el
  texto crudo. Las subcompeticiones se reconocen porque su etiqueta empieza por
  `-`, y además caen por la lista de exclusión.

  Esto no es teórico: la config vieja tenía Segunda B apuntando a `33575532`,
  que es **el playoff de ascenso**, no la liga. Por eso esa división salía con
  cero equipos y un calendario de una sola jornada en mayo.

- **La respuesta es un `<script>` que asigna `innerHTML`**, no HTML suelto, y va
  en ISO-8859-15. El `<select>` interior usa comillas simples, así que no hay
  que desescapar nada del literal de JavaScript.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from scrapers.rfef_clasificacion import BASE_URL, COD_PRIMARIA, make_session

CAL_PATH = "/pnfg/NPcd/NFG_CmpJornada"
EXEC_PATH = "/pnfg/NPcd/NFG_CmpJornada_Exec"

# Filtros del buscador de competiciones de la PNFG. `Sch_Cod_Agrupacion` es la
# agrupación "Fútbol Sala" y `Sch_Tipo_Juego` el deporte homónimo. Son estables
# entre temporadas (a diferencia de los códigos de competición) porque
# describen el catálogo, no una edición concreta.
AGRUPACION_FUTSAL = "900160072"
TIPO_JUEGO_FUTSAL = "3"

_TIMEOUT = 20


@dataclass(frozen=True)
class Competition:
    """Una competición tal y como la anuncia el `<select>` de la PNFG."""
    code: str
    name: str
    group_label: str | None = None


@dataclass(frozen=True)
class Group:
    """Un grupo dentro de una competición. `id` es el estable de la app."""
    id: str
    code: str
    name: str


# ── Casado nombre → id estable ──────────────────────────────────────────────
#
# Orden significativo: gana la primera regla que encaje. Segunda B va antes que
# Segunda porque su nombre ("Segunda División B Fútbol Sala") no lleva género y
# comparte todas las demás palabras con la masculina.
#
# `require` son palabras que tienen que estar; `forbid`, palabras que no pueden.
DIVISION_RULES: tuple[tuple[str, str, frozenset[str], frozenset[str]], ...] = (
    # Marca nueva de la máxima categoría masculina desde 2026-27: la RFEF
    # rebautizó "Primera División Fútbol Sala Masculino" como **Liga Prime
    # Futsal**, con torneos de Apertura y Clausura. No lleva ni "primera" ni
    # "sala" ni "masculino", así que ninguna de las reglas clásicas la
    # reconocía — y se descartaba en silencio. Ver la nota sobre nombres al
    # final de este bloque.
    ("rfef-primera-fs-masc", "masculino",
     frozenset({"liga", "prime"}), frozenset({"femenino", "femenina"})),
    ("rfef-segunda-b-fs-masc", "masculino",
     frozenset({"segunda", "b", "sala"}), frozenset({"femenino"})),
    ("rfef-primera-fs-fem", "femenino",
     frozenset({"primera", "sala", "femenino"}), frozenset()),
    ("rfef-segunda-fs-fem", "femenino",
     frozenset({"segunda", "sala", "femenino"}), frozenset({"b"})),
    ("rfef-primera-fs-masc", "masculino",
     frozenset({"primera", "sala", "masculino"}), frozenset({"femenino"})),
    ("rfef-segunda-fs-masc", "masculino",
     frozenset({"segunda", "sala", "masculino"}), frozenset({"b", "femenino"})),
)

# NOTA SOBRE NOMBRES, escrita después de equivocarme.
#
# La decisión de casar por nombre en vez de por código sigue siendo la correcta
# —los códigos cambian **cada** temporada, sin excepción— pero el argumento con
# el que se justificó ("los nombres aguantan de un año a otro") resultó falso a
# la primera: en 2026-27 la máxima categoría masculina pasó a llamarse "Liga
# Prime Futsal", sin una sola palabra en común con la anterior.
#
# Por eso lo que de verdad protege no es la lista de reglas, que siempre irá por
# detrás de la próxima campaña de marketing, sino que **lo que no casa se
# denuncia** (`classify_competition` → "desconocida" → `warnings` del JSON). Una
# competición de sala sin reconocer es una señal, no un caso normal.

# Nombre visible de cada división en el JSON publicado. No se toma el de la
# PNFG porque cambia de puntuación y de mayúsculas entre temporadas ("Primera
# División Futbol Sala Masculino" un año, "Fútbol" con tilde al siguiente) y la
# app lo enseña tal cual en un desplegable.
#
# **El nombre se puede corregir; el id de arriba no.** La app guarda
# `Team.divisionId` dentro de cada equipo, y de él cuelgan el calendario, la
# importación de rivales y las novedades. Por eso `rfef-segunda-fs-masc` sigue
# llamándose así aunque su nombre visible ya no lleve la "A".
DIVISION_NAMES = {
    "rfef-primera-fs-masc": "Primera División FS",
    # No es "Segunda A": la categoría se llama Segunda División, y la de abajo
    # Segunda División B. La "A" era invención nuestra para distinguirlas, y en
    # el desplegable le hacía dudar a quien sí sabe cómo se llama su liga.
    "rfef-segunda-fs-masc": "Segunda División FS",
    "rfef-segunda-b-fs-masc": "Segunda División FS B",
    "rfef-primera-fs-fem": "Primera División FS Femenina",
    "rfef-segunda-fs-fem": "Segunda División FS Femenina",
}

# Género por división, derivado de las reglas para que no haya dos listas que
# un día discrepen.
DIVISION_GENDER = {div_id: gender for div_id, gender, _, _ in DIVISION_RULES}

# Cualquiera de estas palabras descarta la competición. Cubre playoffs, copas,
# supercopas, categorías de base y campeonatos de selecciones — todo lo que
# comparte plataforma con las ligas pero no es una liga.
_EXCLUDE = frozenset({
    "playoff", "playoffs", "copa", "supercopa", "juvenil", "cadete",
    "infantil", "alevin", "benjamin", "prebenjamin", "seleccion",
    "selecciones", "autonomicas", "campeonato", "campeonatos", "base",
    "sub", "reina", "rey", "playa", "fase", "ascenso", "titulo",
    "diversidad", "amistoso", "honor",
})
# "torneo" estuvo aquí y hubo que sacarlo: desde 2026-27 la máxima categoría
# masculina se juega como "Torneo Apertura" y "Torneo Clausura", así que
# excluirlo tiraba justo la competición que buscamos. Lo que de verdad descarta
# selecciones y campeonatos de base son "selecciones", "campeonato" y "sub".


def _words(name: str) -> list[str]:
    """Palabras en minúscula y sin acentos. `_norm` de los otros módulos pega
    todo junto, y aquí hacen falta separadas: la "B" de Segunda B es una palabra
    entera y dentro de una cadena pegada no se puede distinguir."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


# "Sala" y "futsal" son la misma cosa. Hizo falta el segundo cuando la marca
# nueva dejó de decir "sala".
_SALA_WORDS = frozenset({"sala", "futsal"})

# Clasificación de una competición del catálogo.
COMP_LEAGUE = "liga"          # es una de nuestras divisiones
COMP_IGNORED = "descartada"   # copa, juvenil, selecciones, playa… correcto ignorarla
COMP_UNKNOWN = "desconocida"  # parece liga sénior de sala y no la reconocemos


def classify_competition(name: str) -> tuple[str, str | None, str | None]:
    """`(clase, division_id, gender)`.

    La clase `COMP_UNKNOWN` es la que importa: una competición que **parece** una
    liga de fútbol sala y que ninguna regla reconoce. Antes eso era
    indistinguible de una copa y se tiraba sin decir nada — que es como una
    división rebautizada desaparecería del JSON durante una temporada entera sin
    que nadie se enterase.
    """
    words = set(_words(name))
    is_futsal = bool(words & _SALA_WORDS)
    # "Futsal" y "sala" son sinónimos, así que las reglas escritas con "sala"
    # tienen que casar igual con "Segunda División Futsal Masculino".
    if "futsal" in words:
        words.add("sala")
    # La marca nueva no dice ni "sala" ni "futsal" en algunos rótulos cortos.
    if not is_futsal and {"liga", "prime"} <= words:
        is_futsal = True
    if not is_futsal:
        return COMP_IGNORED, None, None
    if words & _EXCLUDE:
        return COMP_IGNORED, None, None
    for div_id, gender, require, forbid in DIVISION_RULES:
        if require <= words and not (forbid & words):
            return COMP_LEAGUE, div_id, gender
    return COMP_UNKNOWN, None, None


def match_division(competition_name: str) -> tuple[str, str] | None:
    """`(division_id, gender)` de una competición, o None si no nos interesa.

    Devolver None es lo normal y no es un error: la agrupación de fútbol sala
    trae también juveniles, copas y selecciones. Para distinguir "no nos
    interesa" de "no la reconozco", usa `classify_competition`.
    """
    clase, div_id, gender = classify_competition(competition_name)
    return (div_id, gender) if clase == COMP_LEAGUE else None


# ── Parsers (puros: reciben texto, no tocan la red) ─────────────────────────

_OPTION_RE = re.compile(r"<option[^>]*value\s*=\s*['\"]?([^'\"\s>]+)['\"]?[^>]*>([^<]*)")
_OPTGROUP_RE = re.compile(r"<optgroup[^>]*label\s*=\s*['\"]([^'\"]*)['\"]")
_GROUPS_ARRAY_RE = re.compile(r"var\s+grupos\s*=\s*new\s+Array\s*\((.*?)\)\s*;", re.S)
_JS_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_GRUPO_N_RE = re.compile(r"\bgrupo\s*(\d+)\b")


def parse_seasons(html: str) -> dict[str, str]:
    """`{"2026-2027": "22", ...}` leído del `<select name=temporada>`."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", attrs={"name": "temporada"})
    if sel is None:
        return {}
    out: dict[str, str] = {}
    for opt in sel.find_all("option"):
        label = opt.get_text(strip=True)
        value = (opt.get("value") or "").strip()
        if label and value:
            out.setdefault(label, value)
    return out


def parse_competitions(script: str) -> list[Competition]:
    """Competiciones de la respuesta de `NFG_CmpJornada_Exec`.

    Se recorre el texto en orden para poder asociar cada `<option>` con el
    `<optgroup>` que la precede. La etiqueta del grupo se guarda solo para
    diagnóstico: quien filtra de verdad es `match_division`, porque el
    `<optgroup>` desaparece según qué filtros se manden.
    """
    out: list[Competition] = []
    current_label: str | None = None
    seen: set[str] = set()
    # Un solo barrido con las dos expresiones, ordenado por posición.
    events = [(m.start(), "group", m) for m in _OPTGROUP_RE.finditer(script)]
    events += [(m.start(), "option", m) for m in _OPTION_RE.finditer(script)]
    events.sort(key=lambda e: e[0])
    for _pos, kind, m in events:
        if kind == "group":
            current_label = m.group(1).strip() or None
            continue
        code = m.group(1).strip()
        name = m.group(2).replace("&nbsp;", " ").strip()
        if not code.isdigit() or code == "0" or not name:
            continue
        # Las subcompeticiones (playoffs) van anidadas y su etiqueta empieza
        # por guion. Ver la cabecera del módulo.
        if name.startswith("-"):
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(Competition(code=code, name=name, group_label=current_label))
    return out


# Un "grupo" que en realidad es una fase del calendario. Desde 2026-27 la
# máxima categoría masculina se juega como Torneo Apertura + Torneo Clausura, y
# la PNFG los expone por el mismo `<select>` que los grupos territoriales.
#
# La diferencia importa: los grupos territoriales tienen **equipos distintos**
# y la entrenadora elige el suyo; las fases tienen **los mismos 16** y elegir no
# significa nada. Publicar Liga Prime con un desplegable de "Torneo Apertura /
# Torneo Clausura" le pide al usuario una decisión que no existe — y en el JSON
# de agosto de 2026 salió publicado solo el Clausura, que es la fase que aún no
# se juega.
_PHASE_WORDS = frozenset({"apertura", "clausura", "fase", "vuelta", "torneo"})


def is_phase(name: str) -> bool:
    """¿Este "grupo" es una fase del calendario y no un grupo territorial?"""
    words = set(_words(name))
    return bool(words & _PHASE_WORDS) and not _GRUPO_N_RE.search(
        " ".join(_words(name)))


def parse_groups(script: str) -> list[Group]:
    """Grupos de la respuesta de `NFG_CmpJornada_Exec?codcompeticion=…`.

    El servidor devuelve `var grupos=new Array("0","-- Seleccione --","33836182",
    "Grupo 1", …)`: pares (código, nombre) planos, con un centinela delante.

    El id estable sale del **número que lleva el nombre** ("Grupo 2" → `g2`) y no
    de la posición en la lista. La posición parece equivalente y no lo es: la app
    guarda `Team.groupId`, así que si la PNFG devolviera los grupos en otro orden,
    los ids posicionales moverían a cada equipo al grupo del vecino sin que nada
    fallara visiblemente.
    """
    m = _GROUPS_ARRAY_RE.search(script)
    if not m:
        return []
    items = [s.encode().decode("unicode_escape") if "\\" in s else s
             for s in _JS_STRING_RE.findall(m.group(1))]
    out: list[Group] = []
    for i in range(0, len(items) - 1, 2):
        code, name = items[i].strip(), items[i + 1].strip()
        if not code.isdigit() or code == "0" or not name:
            continue
        n = _GRUPO_N_RE.search(" ".join(_words(name)))
        gid = f"g{n.group(1)}" if n else f"g{len(out) + 1}"
        out.append(Group(id=gid, code=code, name=name))
    return out


# ── Red ─────────────────────────────────────────────────────────────────────
#
# El descubrimiento es **camino crítico**: si falla, `scrape()` no publica nada
# (ver "Fallar cerrado" en `rfef.py`). Así que necesita la misma tolerancia al
# rate-limit que el resto de fetchers, y no la tuvo de entrada — un smoke test
# en vivo se lo comió a la primera.
#
# La PNFG rate-limita por IP devolviendo **200 con el cuerpo vacío**, no un 429.
# Se sale de ahí con una `JSESSIONID` nueva y esperando: los backoffs largos son
# a propósito, el bloqueo dura más que un retry corto.

_BACKOFFS = (15, 30, 60, 120)


class Fetcher:
    """Sesión contra la PNFG que se renueva sola ante el rate-limit.

    Se comparte entre las llamadas del descubrimiento para que, cuando una se
    coma un bloqueo y consiga sesión nueva, las siguientes arranquen ya con la
    buena en vez de volver a tropezar cada una por su cuenta.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or make_session()

    def get(
        self, path: str, params: dict, *, retries: int = 4, label: str = ""
    ) -> str | None:
        tag = label or path
        err = "desconocido"
        for attempt in range(retries + 1):
            try:
                r = self._session.get(BASE_URL + path, params=params,
                                      timeout=_TIMEOUT)
            except requests.RequestException as e:
                err = f"error de red ({e})"
            else:
                if r.status_code != 200:
                    # Un status distinto de 200 no es rate-limit: reintentar no
                    # lo va a arreglar.
                    print(f"  [rfef-disc] {tag}: HTTP {r.status_code}")
                    return None
                if r.content:
                    return r.content.decode("iso-8859-15", errors="replace")
                err = "respuesta vacía (rate-limit o sesión perdida)"

            if attempt < retries:
                backoff = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                print(f"  [rfef-disc] {tag}: {err}; reintento en {backoff}s "
                      f"con sesión nueva ({attempt + 1}/{retries})")
                time.sleep(backoff)
                self._session = make_session()

        print(f"  [rfef-disc] {tag}: agotados los reintentos ({err})")
        return None


def _as_fetcher(session: "requests.Session | Fetcher | None") -> Fetcher:
    """Acepta un `Fetcher`, una `Session` suelta o nada."""
    return session if isinstance(session, Fetcher) else Fetcher(session)


# Resultados de `resolve_season`. La diferencia entre los dos últimos no es
# cosmética: decide si un run fallido debe mandarte un email o no.
#
#   OK            → hay código, se puede scrapear.
#   NOT_PUBLISHED → la PNFG contestó y esa temporada todavía no está en su
#                   catálogo. Es lo **normal** cada julio, cuando el calendario
#                   ya dice "temporada nueva" y la federación aún no la ha
#                   creado. No es una avería.
#   UNAVAILABLE   → no se pudo ni preguntar (rate-limit, red, cambio de HTML).
#                   Eso sí es una avería y tiene que avisar.
#
# Las dos últimas impiden publicar igualmente. Lo que cambia es el ruido: un
# fallo diario que siempre es normal es la forma más rápida de que dejes de
# mirar los emails de Actions, que son la única alerta que hay.
SEASON_OK = "ok"
SEASON_NOT_PUBLISHED = "not_published"
SEASON_UNAVAILABLE = "unavailable"


def resolve_season(
    season: str, *, session: "requests.Session | Fetcher | None" = None
) -> tuple[str | None, str]:
    """`(CodTemporada, estado)` de una temporada "YYYY-YYYY".

    Antes esto devolvía solo el código y, al no encontrarlo, se degradaba a
    "usa la temporada por defecto del servidor" — que es exactamente cómo se
    acabó publicando 2025-26 con etiqueta de 2026-27.
    """
    f = _as_fetcher(session)
    html = f.get(CAL_PATH, {"cod_primaria": COD_PRIMARIA}, label="temporadas")
    if html is None:
        return None, SEASON_UNAVAILABLE

    seasons = parse_seasons(html)
    if not seasons:
        # Respondió, pero sin el `<select>`: o han cambiado el HTML o nos han
        # servido una página de error. No se puede afirmar que la temporada no
        # exista, así que se trata como avería.
        print("  [rfef-disc] la página de calendario no trae <select name=temporada>")
        return None, SEASON_UNAVAILABLE

    code = seasons.get(season)
    if code is None:
        print(f"  [rfef-disc] {season} todavía no está en el catálogo de la PNFG "
              f"(hay {len(seasons)}, la más reciente {next(iter(seasons))})")
        return None, SEASON_NOT_PUBLISHED
    return code, SEASON_OK


def resolve_season_code(
    season: str, *, session: "requests.Session | Fetcher | None" = None
) -> str | None:
    """Solo el `CodTemporada`, para los callers a los que el motivo les da
    igual (`rfef_calendario.resolve_temporada_code`)."""
    return resolve_season(season, session=session)[0]


def list_competitions(
    season_code: str, *, session: "requests.Session | Fetcher | None" = None
) -> list[Competition] | None:
    """Competiciones de fútbol sala de una temporada.

    **None y `[]` no son lo mismo**, y la diferencia decide si el run avisa:
    None es "no se pudo preguntar" (avería); `[]` es "la PNFG dice que todavía
    no hay ninguna", que es lo normal a principios de temporada.
    """
    f = _as_fetcher(session)
    script = f.get(EXEC_PATH, {
        "cod_primaria": COD_PRIMARIA,
        "codtemporada": season_code,
        "Sch_Cod_Agrupacion": AGRUPACION_FUTSAL,
        "Sch_Tipo_Juego": TIPO_JUEGO_FUTSAL,
        "Sch_Codigo_Delegacion": "",
    }, label=f"competiciones t{season_code}")
    return parse_competitions(script) if script is not None else None


def list_groups(
    competition_code: str, *, session: "requests.Session | Fetcher | None" = None
) -> list[Group] | None:
    """Grupos de una competición.

    **None y `[]` no son lo mismo**, igual que en `resolve_season` y en
    `list_competitions`, y por el mismo motivo — sólo que aquí faltaba, y salió
    caro. None es "no se pudo preguntar" (la PNFG rate-limita devolviendo 200
    con el cuerpo vacío); `[]` es "la PNFG contesta que esta competición
    todavía no tiene grupos", que es lo normal antes del sorteo.

    Colapsar los dos en una lista vacía —que es lo que hacía— convierte una
    avería en un "aún no hay nada" y `discover_divisions` **tira la división**:
    el 1 de septiembre de 2026 eso publicó `rfef-segunda-fs-fem` vacía y se
    llevó por delante 48 equipos y 90 jornadas que estaban bien en el JSON
    anterior, con el aviso diciendo "sin publicar en la PNFG" — que era falso.
    """
    f = _as_fetcher(session)
    script = f.get(EXEC_PATH, {
        "cod_primaria": COD_PRIMARIA,
        "codcompeticion": competition_code,
        "Sch_Codigo_Delegacion": "",
    }, label=f"grupos de {competition_code}")
    return parse_groups(script) if script is not None else None
