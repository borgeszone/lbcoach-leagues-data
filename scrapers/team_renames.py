# -*- coding: utf-8 -*-
"""Un equipo, un nombre: lo que se cambia de nombre a mitad de temporada.

La PNFG nombra a cada equipo como se llame **el día de cada jornada**. Cuando un
club cambia de patrocinador o de nombre a mitad de temporada, el calendario
lleva los dos: en Segunda Femenina G2 de 2026-27, "La Villa Móstoles" juega la
J1 y la J2 y "La Villa" de la J3 en adelante. Como los equipos se deducen del
calendario (`teams_from_calendar`), el grupo salía con **17 equipos para 8
partidos por jornada**, y en la app eran dos rivales.

Dos arreglos, por nodo (división plana o grupo):

1. **Las grafías.** El mismo nombre llega con y sin espacios dentro de las
   comillas (`Les Corts, A.E. "B"` y `Les Corts, A.E. " B "`). Se reescribe el
   calendario a una sola: la del equipo publicado si casa, o la más frecuente.

2. **Los cambios de nombre.** Dos equipos del nodo son el mismo si:

   - **nunca coinciden en una jornada** — dos equipos distintos del mismo grupo
     coinciden en casi todas, porque cada uno descansa como mucho una vez por
     vuelta;
   - entre los dos aparecen en **3 jornadas o más** — con dos jornadas, en un
     grupo impar, el que descansa la J1 y el que descansa la J2 también serían
     disjuntos;
   - y **los nombres se parecen** (`rfef_web._score`, cobertura ≥ 0,5).

   Se queda el nombre de la **última jornada** en que aparece, que es como se
   llama hoy, y los demás viajan en `aliases`, que la app usa para reconocerlo
   (y para juntar los rivales que ya tuviera guardados con cada nombre).

Lo que **no** hace: juntar equipos de divisiones o grupos distintos. El filial y
el primer equipo de un club ("ElPozo Ciudad de Murcia" en Segunda y "ElPozo
Murcia Costa Cálida" en Primera) son dos equipos, y el masculino y el femenino
de Futsal Mataró también.
"""
from __future__ import annotations

from collections import Counter

from scrapers.rfef import _norm
from scrapers.rfef_web import _score


def _names(team: dict) -> list[str]:
    out = [team.get("name"), team.get("officialName"), *(team.get("aliases") or [])]
    return [n for n in out if n]


def _unify_spellings(node: dict) -> int:
    calendar = node.get("calendar") or []
    published = {_norm(n): n for t in node.get("teams") or [] for n in _names(t)}
    by_key: dict[str, Counter] = {}
    for jornada in calendar:
        for m in jornada.get("matches", []):
            for side in ("home", "away"):
                name = m.get(side)
                if name:
                    by_key.setdefault(_norm(name), Counter())[name] += 1
    canon: dict[str, str] = {}
    for key, variants in by_key.items():
        pick = published.get(key) or variants.most_common(1)[0][0]
        for v in variants:
            if v != pick:
                canon[v] = pick
    if not canon:
        return 0
    for jornada in calendar:
        for m in jornada.get("matches", []):
            for side in ("home", "away"):
                if m.get(side) in canon:
                    m[side] = canon[m[side]]
    return len(canon)


def _appearances(node: dict) -> dict[int, set[int]]:
    """Por índice de equipo, en qué jornadas (por posición) juega."""
    teams = node.get("teams") or []
    keys = [{_norm(n) for n in _names(t)} for t in teams]
    out: dict[int, set[int]] = {i: set() for i in range(len(teams))}
    for j, jornada in enumerate(node.get("calendar") or []):
        playing = {_norm(m.get(s) or "") for m in jornada.get("matches", [])
                   for s in ("home", "away")}
        for i, k in enumerate(keys):
            if k & playing:
                out[i].add(j)
    return out


def _similar(a: dict, b: dict) -> bool:
    return any(
        max(_score(x, y)[0], _score(y, x)[0]) >= 0.5
        for x in _names(a) for y in _names(b)
    )


def merge_renamed_teams(node: dict) -> list[str]:
    """Junta en `node` los equipos que son uno con dos nombres. Devuelve qué
    ha juntado, para el log."""
    _unify_spellings(node)
    teams = node.get("teams") or []
    if len(teams) < 2 or not node.get("calendar"):
        return []
    seen = _appearances(node)
    merged: list[str] = []
    gone: set[int] = set()
    for i in range(len(teams)):
        for k in range(i + 1, len(teams)):
            if i in gone or k in gone:
                continue
            a, b = seen[i], seen[k]
            if not a or not b or a & b or len(a | b) < 3:
                continue
            if not _similar(teams[i], teams[k]):
                continue
            keep, drop = (i, k) if max(a) > max(b) else (k, i)
            survivor, loser = teams[keep], teams[drop]
            known = {_norm(n) for n in _names(survivor)}
            aliases = list(survivor.get("aliases") or [])
            for n in _names(loser):
                if _norm(n) not in known:
                    aliases.append(n)
                    known.add(_norm(n))
            if aliases:
                survivor["aliases"] = aliases
            if not survivor.get("logoUrl") and loser.get("logoUrl"):
                survivor["logoUrl"] = loser["logoUrl"]
            seen[keep] = a | b
            gone.add(drop)
            merged.append(f"{loser['name']} → {survivor['name']}")
    if gone:
        node["teams"] = [t for n, t in enumerate(teams) if n not in gone]
    return merged


def merge_renamed_in_categories(categories: list[dict]) -> list[str]:
    out: list[str] = []
    for cat in categories:
        for div in cat.get("divisions", []):
            for msg in merge_renamed_teams(div):
                out.append(f"{div['id']}: {msg}")
            for g in div.get("groups", []):
                for msg in merge_renamed_teams(g):
                    out.append(f"{div['id']}/{g['id']}: {msg}")
    return out
