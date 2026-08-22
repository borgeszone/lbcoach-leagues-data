"""Resolver de URLs de escudos de equipos.

Cascada de resolución (la primera que tenga éxito gana):

1. **Allowlist curada** (`data/badges-overrides.json`): el maintainer añade
   manualmente entradas cuando un club no aparece en el portal oficial o su
   nombre no casa letra por letra. Es la fuente autoritativa.

   Cada entrada es un objeto `{url, fuente, titular, licencia, verificado}` —
   los campos de procedencia los pide `IP-004` de la auditoría legal. Se sigue
   aceptando el valor en forma de cadena suelta (era el formato original), así
   que un fichero viejo se lee igual; lo que no se puede es **escribir** en el
   formato viejo, porque entonces la entrada entra sin procedencia y el hueco
   deja de verse.

2. **Mapa del portal oficial** (`futsal.rfef.es`), inyectado por `scrape.py` al
   empezar el run con `inject_rfef_shields`.

3. **Caché persistente** (`data/badges-cache.json`): lo que el mapa del portal
   dio en runs anteriores. Se commitea al repo para que un run en el que el
   portal no responda no pierda cobertura.

4. `None` → la app muestra su placeholder genérico.

**Aquí había dos fuentes más y se han retirado** (agosto de 2026): imágenes de
artículos de Wikipedia, filtradas por nombre de fichero, y una búsqueda de
imágenes en DuckDuckGo por scrape de HTML. Las dos las señala `IP-004` como
procedencia desconocida —no se consultaba el campo de licencia de Wikimedia en
ningún momento, y el endpoint interno de DDG es probable incumplimiento de sus
términos— y son el punto 2 del plan de la auditoría. Su coste real al retirarlas
era casi nulo: el camino que las usaba ya iba con `trusted_only` en la práctica
totalidad de las divisiones. **No reintroducirlas**: si hace falta cobertura, se
añade una entrada curada con su procedencia, que es la vía que sí se puede
defender.

Consecuencia de esa retirada, y es la que sostiene el resto del fichero: **todo
lo que puede entrar en la caché viene ya de una fuente oficial**. Se valida por
dominio al leer y al escribir (`_TRUSTED_HOSTS`), así que la garantía es
estructural y no depende de que nadie vuelva a colar un buscador.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path(__file__).parent.parent / "data"
OVERRIDES_PATH = DATA_DIR / "badges-overrides.json"
CACHE_PATH = DATA_DIR / "badges-cache.json"

# Dominios de los que se acepta un escudo. Son los dos de la federación: el
# portal de la LNFS y el servidor de ficheros de la PNFG, que es de donde salen
# los escudos de las filas de clasificación.
#
# La allowlist curada **no** pasa por aquí a propósito: la valida su propio
# fichero, entrada por entrada y con procedencia escrita. Esto es para lo que se
# resuelve solo, que es lo que no lleva nadie mirando.
_TRUSTED_HOSTS = (
    "futsal.rfef.es",
    "rfef.filesnovanet.es",
)


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _is_trusted_url(url: object) -> bool:
    """¿Es una URL de la que se acepta un escudo sin que nadie la haya mirado?

    Se compara el **host exacto** o un subdominio suyo, no `in`: con
    `"futsal.rfef.es" in url` valdría cualquier dominio que lleve eso dentro de
    la ruta o del nombre, que es el agujero clásico de esta comprobación.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _TRUSTED_HOSTS)


# ── Persistencia: overrides + caché ─────────────────────────────────────────

_overrides: dict[str, str] | None = None
_cache: dict[str, str] | None = None


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _override_url(value: object) -> str | None:
    """Saca la URL de una entrada de la allowlist, en cualquiera de sus dos
    formas: objeto con procedencia (la actual) o cadena suelta (la original).

    Una entrada sin `url` utilizable devuelve None en vez de reventar: este
    fichero se edita a mano, y una entrada a medias tiene que costar un escudo,
    no el run entero.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            return url.strip() or None
    return None


def _ensure_loaded() -> None:
    global _overrides, _cache
    if _overrides is None:
        _overrides = {}
        for k, v in _load_json(OVERRIDES_PATH).items():
            if k.startswith("_"):
                continue
            url = _override_url(v)
            if url:
                _overrides[k] = url
    if _cache is None:
        # Se filtra al **leer**, no solo al escribir: el fichero está commiteado
        # en el repo y arrastra entradas de cuando la cascada buscaba en
        # Wikipedia y en DuckDuckGo. Sin este filtro, la purga habria que
        # repetirla a mano cada vez que alguien recuperara un fichero viejo.
        #
        # Los `null` de "intentado y sin resultado" también se caen: ya no hay
        # nada que reintentar, así que lo único que harían es cortar la búsqueda
        # antes de tiempo y confundir a quien lea el fichero.
        _cache = {
            k: v
            for k, v in _load_json(CACHE_PATH).items()
            if not k.startswith("_") and _is_trusted_url(v)
        }


def _save_cache() -> None:
    """Persiste la caché. Se llama al terminar el run desde scrape.py."""
    _ensure_loaded()
    payload = {
        "_comment": (
            "Cache auto-generada por scrapers.logo_resolver: lo que el portal "
            "oficial (futsal.rfef.es) dio en runs anteriores, para que un run en "
            "el que no responda no pierda cobertura. NO editar a mano (usa "
            "data/badges-overrides.json para entradas curadas). Solo entran URLs "
            "de los dominios de la federacion; cualquier otra cosa se descarta al "
            "leer y al escribir, que es lo que impide que vuelvan a colarse "
            "resultados de Wikipedia o de un buscador (IP-004). Si una URL deja "
            "de funcionar, borra esa entrada y el proximo run la volvera a poner "
            "si el portal la sigue publicando."
        ),
        **{k: v for k, v in (_cache or {}).items() if _is_trusted_url(v)},
    }
    CACHE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_cache() -> None:
    """Punto público para flush manual desde scrape.py al final del run."""
    _save_cache()


# ── Resolver principal ──────────────────────────────────────────────────────

def lookup_override(team_name: str) -> str | None:
    """Devuelve solo la entrada curada del equipo.

    Útil para flujos donde ya tenemos un escudo de buena calidad (p. ej. la
    clasificación de la PNFG) y solo queremos permitir que el maintainer haga
    override puntual.
    """
    if not team_name or not team_name.strip():
        return None
    _ensure_loaded()
    val = _overrides.get(_norm(team_name))
    return val or None


# Mapa runtime con escudos del portal, inyectado por scrape.py al inicio del run.
_rfef_shields: dict[str, str] = {}


def inject_rfef_shields(shields: dict[str, str]) -> None:
    """Inyecta el dict de escudos extraídos de futsal.rfef.es. El orchestrator
    llama una vez al inicio del run.

    Se filtra por dominio aquí también: el mapa lo construye un parser sobre
    HTML de terceros, así que es una entrada más que no conviene creer a ciegas
    —y de aquí salen las escrituras en la caché, que sí se persisten.
    """
    global _rfef_shields
    _rfef_shields = {k: v for k, v in shields.items() if _is_trusted_url(v)}


def resolve_logo_url(team_name: str, *, trusted_only: bool = False) -> str | None:
    """Devuelve la URL del escudo del equipo o None si nada la tiene.

    `trusted_only` se conserva por compatibilidad con los dos caminos que lo
    pasan, pero **desde la retirada de Wikipedia y DDG ya no cambia el
    resultado**: todas las fuentes que quedan son autoritativas. Se deja porque
    el nombre documenta la intención de quien llama —el camino del calendario no
    tiene ninguna fila oficial que confirme la asociación nombre → escudo— y
    porque el día que alguien añada una fuente nueva, este flag es donde tiene
    que quedarse fuera.
    """
    if not team_name or not team_name.strip():
        return None
    _ensure_loaded()
    key = _norm(team_name)

    # 1. Allowlist curada del maintainer.
    if key in _overrides:
        return _overrides[key] or None

    # 2. Mapa del portal oficial de esta ejecución.
    if key in _rfef_shields:
        url = _rfef_shields[key]
        _cache[key] = url
        return url

    # 3. Lo que el portal dio en runs anteriores. Ya validado por dominio al
    #    cargar, así que vale igual con `trusted_only`.
    if key in _cache:
        return _cache[key]

    return None
