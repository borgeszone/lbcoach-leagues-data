"""Caché persistente del calendario: `actaUrl` por partido y jornadas enteras.

Dos cachés en el mismo fichero, con la misma promesa: **cobertura
monotónicamente creciente**. La PNFG rate-limita devolviendo 200 con el cuerpo
vacío, así que un run desafortunado se queda sin datos que el run anterior sí
tenía. Guardarlos hace que un bloqueo pase de ser *pérdida de datos* a ser *no
hay novedad*, que es lo correcto.

## 1. `actaUrl` por partido

`NFG_CmpJornada` es la única fuente que trae `CodActa`. Si el scrape fresco no
lo consigue —porque caímos al PDF, que solo trae jornada + fecha +
enfrentamientos— se recupera el que guardamos antes. Una vez la federación
publica el `CodActa` de un partido, queda **permanente para el resto de la
temporada**: la URL depende solo de ese número, que la PNFG no cambia.

Clave: `{comp}|{grupo}|J{jornada}|{home_norm}|{away_norm}`.

## 2. Jornadas enteras

Guarda los partidos de cada jornada tal y como los devolvió la PNFG. Cubre dos
casos que antes se perdían enteros:

- **El grupo que falla a medias.** Primera masculina publicó 14 de 15 jornadas
  el 2026-08-31: la J12 se perdió y el PDF ni se consultó, porque el fallback
  es `if not calendar` — un booleano sobre la lista, no una comprobación de
  completitud. Con la caché, esa jornada la rellena el run anterior.
- **El grupo que falla del todo.** Hoy eso significa caer al PDF (día sin hora)
  o publicar sin calendario.

**Solo se guarda lo que viene fresco de la PNFG**, nunca lo derivado del PDF.
Si se guardara el PDF, una jornada con hora acabaría sobrescrita por la misma
jornada sin ella y la caché iría hacia atrás, que es justo lo contrario de lo
que este fichero promete.

### Qué gana a qué al fusionar

Manda **lo fresco**, y la caché solo rellena lo que falta. Es lo que permite que
un aplazamiento se corrija: cuando esa jornada vuelva a bajarse, la fecha nueva
pisa a la vieja. La única excepción es la jornada que ya está pero **sin hora en
ningún partido** (típicamente derivada del PDF) teniendo la caché una versión
con hora: ahí gana la caché, o un run que cayera al PDF tiraría las horas que ya
teníamos.

### Por qué la clave lleva comp y grupo, y no el id de división

Los códigos de competición de la PNFG **cambian cada temporada**. Eso convierte
el prefijo de la clave en una frontera de temporada gratis: las entradas del año
pasado no se pueden leer por accidente este año, porque nadie va a preguntar por
ellas. Con el id estable de la app (`rfef-segunda-fs-fem`) haría falta un campo
de temporada, y acordarse de comprobarlo.

Se commitea al repo en `data/calendar-cache.json` para que el siguiente run
arranque caliente.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "calendar-cache.json"

# Sección de las jornadas dentro del JSON. Empieza por guion bajo a propósito:
# el loader de las actas descarta las claves que empiezan así **y** los valores
# que no son cadena, así que las dos cachés conviven en el mismo fichero plano
# sin migrarlo y sin romperle el fichero a una versión anterior del scraper.
CALENDARS_KEY = "_calendars"

_cache: dict[str, str] | None = None
_calendars: dict[str, dict[str, list[dict]]] | None = None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _key(comp: str, grupo: str, jornada: int, home: str, away: str) -> str:
    return f"{comp}|{grupo}|J{jornada}|{_norm(home)}|{_norm(away)}"


def _group_key(comp: str, grupo: str) -> str:
    return f"{comp}|{grupo}"


def _ensure_loaded() -> None:
    global _cache, _calendars
    if _cache is not None and _calendars is not None:
        return
    raw: dict = {}
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
    # Actas: claves planas de valor cadena. El filtro descarta de un plumazo
    # tanto el comentario del fichero como la sección de jornadas.
    _cache = {k: v for k, v in raw.items()
              if not k.startswith("_") and isinstance(v, str)}
    _calendars = _load_calendars(raw.get(CALENDARS_KEY))


def _load_calendars(raw) -> dict[str, dict[str, list[dict]]]:
    """Valida la sección de jornadas al leerla.

    Una entrada corrupta se descarta sola, sin llevarse a las demás: la caché es
    una optimización, y quedarse sin ella tiene que costar un run lento, nunca un
    run roto.
    """
    out: dict[str, dict[str, list[dict]]] = {}
    if not isinstance(raw, dict):
        return out
    for gkey, jornadas in raw.items():
        if not isinstance(gkey, str) or not isinstance(jornadas, dict):
            continue
        clean: dict[str, list[dict]] = {}
        for num, matches in jornadas.items():
            if not isinstance(num, str) or not num.isdigit():
                continue
            if not isinstance(matches, list):
                continue
            ok = [m for m in matches
                  if isinstance(m, dict) and m.get("home") and m.get("away")]
            if ok:
                clean[num] = ok
        if clean:
            out[gkey] = clean
    return out


def lookup(comp: str | int, grupo: str | int, jornada: int, home: str, away: str) -> str | None:
    """Devuelve el `actaUrl` previamente cacheado para este partido, o None."""
    _ensure_loaded()
    assert _cache is not None
    return _cache.get(_key(str(comp), str(grupo), jornada, home, away))


def store(comp: str | int, grupo: str | int, jornada: int, home: str, away: str,
          acta_url: str) -> None:
    """Guarda el `actaUrl` recién extraído en la caché en memoria. El flush a
    disco se hace al final del run con `save_cache()`."""
    if not acta_url:
        return
    _ensure_loaded()
    assert _cache is not None
    _cache[_key(str(comp), str(grupo), jornada, home, away)] = acta_url


def lookup_jornadas(comp: str | int, grupo: str | int) -> dict[int, list[dict]]:
    """Las jornadas cacheadas de un grupo: `{numero: [partido, ...]}`.

    Devuelve **copias**: quien fusione va a mutar los partidos (añadirles el
    `actaUrl`), y hacerlo sobre la caché en memoria la contaminaría con datos que
    después se persistirían como si hubieran venido de la PNFG.
    """
    _ensure_loaded()
    assert _calendars is not None
    stored = _calendars.get(_group_key(str(comp), str(grupo)), {})
    return {int(num): [dict(m) for m in matches] for num, matches in stored.items()}


def store_jornadas(comp: str | int, grupo: str | int, calendar: list[dict]) -> int:
    """Guarda las jornadas **recién bajadas de la PNFG**. Devuelve cuántas.

    Fusiona por número de jornada en vez de reemplazar el grupo entero: el modo
    `--teams-only` solo baja dos jornadas, y reemplazar dejaría la caché con dos
    de treinta cada vez que corre el cron rápido — o sea, la vaciaría seis días
    de cada siete.

    No se guarda una jornada sin partidos: es lo que devuelve una página que
    contestó pero no traía nada, y cachearla convertiría un fallo en un dato.
    """
    _ensure_loaded()
    assert _calendars is not None
    gkey = _group_key(str(comp), str(grupo))
    bucket = _calendars.setdefault(gkey, {})
    n = 0
    for jornada in calendar:
        num = jornada.get("jornada")
        matches = jornada.get("matches") or []
        if not isinstance(num, int) or not matches:
            continue
        rows = [{"home": m["home"], "away": m["away"], "date": m.get("date")}
                for m in matches if m.get("home") and m.get("away")]
        if not rows:
            continue
        bucket[str(num)] = rows
        n += 1
    if not bucket:
        _calendars.pop(gkey, None)
    return n


def save_cache() -> None:
    """Persiste la caché a `data/calendar-cache.json`. Llamado al final del run
    desde `scrape.py`."""
    _ensure_loaded()
    payload = {
        "_comment": (
            "Cache persistente del calendario auto-generada por "
            "scrapers.calendar_cache. NO editar a mano. Las claves planas son "
            "actaUrls por partido; la seccion _calendars guarda las jornadas "
            "enteras tal y como las devolvio la PNFG. Cada run fusiona lo fresco "
            "con esta cache para que un rate-limit no borre datos que ya "
            "teniamos. Si una entrada deja de ser valida, borrala y la proxima "
            "ejecucion la volvera a resolver. Las claves llevan dentro el codigo "
            "de competicion, que cambia cada temporada, asi que las entradas de "
            "temporadas pasadas quedan inertes (no se podan solas)."
        ),
        **{k: v for k, v in (_cache or {}).items()},
        CALENDARS_KEY: _calendars or {},
    }
    CACHE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
