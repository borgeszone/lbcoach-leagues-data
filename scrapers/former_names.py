# -*- coding: utf-8 -*-
"""Los nombres que un club ha tenido esta temporada, para que la app los reconozca.

La federación cambia el nombre de un equipo a mitad de temporada (patrocinador
nuevo, o la PNFG que lo escribe de otra forma) y el JSON publicado pasa a decir
el nuevo. La app ya había guardado el viejo como rival, y con él se queda: el
viejo no sale en ninguna liga, así que para la app es un rival tecleado a mano y
lo enseña **en todos los equipos** (§6.85 del informe). En noviembre de 2026 un
móvil tenía «Industrias Santa Coloma», «Inagroup El Ejido», «Osasuna Magna» y
«FS García», cuatro nombres de Primera que ya no se publicaban.

`team_renames` junta los dos nombres cuando están **en el mismo JSON**. Esto
cubre el otro caso: el nombre viejo ya no está en ninguna parte del JSON de hoy.
Se guarda lo aprendido en `data/former-names.json`, que el workflow commitea como
las demás cachés, y en cada run:

1. **Se aprende** comparando el JSON publicado con el que se va a publicar, nodo
   a nodo (división plana o grupo). Dos nombres son del mismo club sólo por
   hechos, nunca por parecido (los dos Mataró, §6.54):

   - **comparten un nombre**: el equipo de antes y el de hoy tienen el mismo
     nombre corto, oficial o alias ("Inagroup El Ejido" y "El Ejido FS" llevan
     los dos el oficial "Sur Seeds El Ejido F.S.");
   - **ocupan el mismo hueco del calendario**: en la misma jornada, contra el
     mismo rival y del mismo lado, en **dos jornadas o más** y siempre el mismo
     ("FS García" juega la J11 en casa del Inter, y hoy ese partido lo juega
     Catgas Energia).

2. **Se aplica**: cada equipo publica en `aliases` los nombres de su club que ya
   no son de nadie.

Dos guardas, y las dos impiden lo mismo, juntar dos clubes: un grupo de nombres
que toque a **dos equipos de hoy** del mismo nodo no se aprende ni se aplica, y
un alias que sea el nombre de **otro equipo de hoy** en cualquier división no se
publica.

Lo que **no** hace: reconocer a un equipo de la temporada pasada. Ése no tiene
nombre nuevo, se fue.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from scrapers.rfef import _norm

STORE_PATH = Path(__file__).parent.parent / "data" / "former-names.json"

# Para el hueco del calendario: con una sola jornada, un aplazamiento o un
# error de la federación bastaría para casar a dos clubes.
MIN_SLOTS = 2


def _names(team: dict) -> list[str]:
    out = [team.get("name"), team.get("officialName"), *(team.get("aliases") or [])]
    return [n for n in out if n]


def _nodes(categories: list[dict]):
    """(clave, nodo) de cada división plana y cada grupo."""
    for cat in categories:
        for div in cat.get("divisions", []):
            if div.get("teams"):
                yield div["id"], div
            for g in div.get("groups", []) or []:
                yield f"{div['id']}/{g['id']}", g


def _slot_key(j: dict) -> tuple[str, int]:
    return (j.get("phase") or "", j.get("jornada"))


# ── El almacén ───────────────────────────────────────────────────────────────


def load_store(season: str, path: Path = STORE_PATH) -> dict:
    """{nodo: [[nombres de un club], …]}, vacío si es de otra temporada."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("season") != season:
        return {}
    nodes = data.get("nodes") or {}
    return {k: [list(g) for g in v if isinstance(g, list)] for k, v in nodes.items()}


def save_store(store: dict, season: str, path: Path = STORE_PATH) -> None:
    payload = {
        "_comment": (
            "Nombres que cada club ha tenido esta temporada, aprendidos "
            "comparando cada JSON publicado con el siguiente "
            "(scrapers/former_names.py). Se publican como `aliases`. Se "
            "regenera solo; una línea mala se borra a mano."
        ),
        "season": season,
        "nodes": {k: sorted(sorted(g) for g in v) for k, v in sorted(store.items()) if v},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _merge(groups: list[list[str]], names: list[str], current: dict[str, int]) -> bool:
    """Junta `names` con los grupos que comparten alguno. No lo hace si el
    resultado tocaría a dos equipos de hoy (`current`: clave → índice)."""
    keys = {_norm(n) for n in names}
    touching = [g for g in groups if keys & {_norm(n) for n in g}]
    merged = list(names)
    for g in touching:
        merged.extend(g)
    owners = {current[_norm(n)] for n in merged if _norm(n) in current}
    if len(owners) > 1:
        return False
    for g in touching:
        groups.remove(g)
    seen: dict[str, str] = {}
    for n in merged:
        seen.setdefault(_norm(n), n)
    groups.append(list(seen.values()))
    return True


# ── Aprender ─────────────────────────────────────────────────────────────────


def _slot_match(old_node: dict, new_node: dict, old_keys: set[str],
                current: dict[str, int]) -> int | None:
    """El equipo de hoy que ocupa los huecos del calendario que ocupaba el
    viejo, o None si no hay al menos `MIN_SLOTS` y todos dicen el mismo."""
    new_by_slot = {_slot_key(j): j for j in new_node.get("calendar") or []}
    hits: list[int] = []
    for j in old_node.get("calendar") or []:
        nj = new_by_slot.get(_slot_key(j))
        if not nj:
            continue
        for m in j.get("matches", []):
            for side, other in (("home", "away"), ("away", "home")):
                if _norm(m.get(side) or "") not in old_keys:
                    continue
                rival = _norm(m.get(other) or "")
                if rival not in current:
                    continue
                for nm in nj.get("matches", []):
                    if _norm(nm.get(other) or "") == rival:
                        who = _norm(nm.get(side) or "")
                        hits.append(current.get(who, -1))
    if len(hits) < MIN_SLOTS or len(set(hits)) != 1 or hits[0] < 0:
        return None
    return hits[0]


def learn(store: dict, published_categories: list[dict],
          new_categories: list[dict]) -> None:
    """Añade al almacén lo que se ve al pasar de `published` a `new`.

    Se guardan **todos** los equipos publicados esta temporada con sus nombres
    —también los que hoy no están, con los suyos solos—, no sólo los que cambian
    de un JSON al siguiente: hay clubes que un run publica
    y el siguiente no («Industrias Santa Coloma» entraba y salía de Primera, y
    «Catgas Energia» apareció en un run en que no estaba), y sólo comparando
    con el anterior el nombre viejo y el nuevo no coinciden nunca."""
    old_nodes = dict(_nodes(published_categories))
    for key, node in _nodes(new_categories):
        teams = node.get("teams") or []
        if not teams:
            continue
        current = {_norm(n): i for i, t in enumerate(teams) for n in _names(t)}
        groups = store.setdefault(key, [])
        old = old_nodes.get(key)
        for ot in (old or {}).get("teams") or []:
            names = _names(ot)
            keys = {_norm(n) for n in names}
            owners = {current[k] for k in keys if k in current}
            if len(owners) > 1:
                continue
            if not owners:
                who = _slot_match(old, node, keys, current)
                if who is not None:
                    names = names + _names(teams[who])
            _merge(groups, names, current)


# ── Aplicar ──────────────────────────────────────────────────────────────────


def apply(store: dict, categories: list[dict]) -> list[str]:
    """Publica en `aliases` los nombres viejos de cada club. Devuelve, para el
    log, qué equipo lleva qué nombres de antes."""
    everywhere: dict[str, int] = {}
    for _, node in _nodes(categories):
        for t in node.get("teams") or []:
            for n in _names(t):
                everywhere.setdefault(_norm(n), id(t))
    out: list[str] = []
    for key, node in _nodes(categories):
        groups = store.get(key) or []
        teams = node.get("teams") or []
        if not groups or not teams:
            continue
        current = {_norm(n): i for i, t in enumerate(teams) for n in _names(t)}
        for g in groups:
            owners = {current[_norm(n)] for n in g if _norm(n) in current}
            if len(owners) != 1:
                continue
            team = teams[owners.pop()]
            known = {_norm(n) for n in _names(team)}
            aliases = list(team.get("aliases") or [])
            fresh: list[str] = []
            for n in g:
                k = _norm(n)
                if k in known or everywhere.get(k, id(team)) != id(team):
                    continue
                aliases.append(n)
                known.add(k)
                fresh.append(n)
            if aliases:
                team["aliases"] = aliases
            if fresh:
                out.append(f"{key}: {team['name']} ← {', '.join(fresh)}")
    return out


# ── Sembrar desde el historial ───────────────────────────────────────────────


def seed(paths: list[Path], season: str, since: str = "") -> dict:
    """Recorre JSON publicados de `season` en orden de `lastUpdated` y aprende
    de cada par consecutivo. `since` descarta los anteriores a esa fecha ISO."""
    docs = []
    for p in paths:
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("version") == season and d.get("lastUpdated", "") >= since:
            docs.append(d)
    docs.sort(key=lambda d: d.get("lastUpdated", ""))
    store: dict = {}
    for prev, new in zip([{}] + docs, docs):
        learn(store, prev.get("categories", []), new.get("categories", []))
    return store


if __name__ == "__main__":
    # python -m scrapers.former_names SEASON SINCE fichero.json [fichero.json …]
    season, since, *files = sys.argv[1:]
    store = seed([Path(f) for f in files], season, since)
    save_store(store, season)
    print(f"{sum(len(v) for v in store.values())} clubes vistos en {len(store)} nodos")
