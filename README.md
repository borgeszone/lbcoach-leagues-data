# lbcoach-leagues-data

Genera y publica `leagues.json` — la fuente de datos que la app **GoalDash / lbcoach** descarga al crear un equipo para autorrellenar la lista de rivales con sus escudos.

## Cómo funciona

```
┌─────────────────────────────────────┐
│ scrape.py                           │
│   ├─ scrapers/rfef.py               │  PDFs oficiales RFEF + fallback
│   ├─ scrapers/fcf.py                │  Lee data/fcf-manual.json
│   └─ scrapers/badges.py             │  Wikipedia → escudos
└─────────────────────────────────────┘
              │
              ▼
       output/leagues.json
              │
              ▼
       Branch `gh-pages`
              │
              ▼
  https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json
              │
              ▼
       App Flutter (lbcoach)
```

## Uso local

```bash
pip install -r requirements.txt
python scrape.py
# Salida: output/leagues.json
```

Opciones:
- `--season 2026-2027` — cambiar la temporada
- `--no-badges` — saltar la resolución de escudos (más rápido para iterar)

## Mantenimiento por temporada

### RFEF (automático)
El scraper descarga los PDFs de calendario oficial de rfef.es siguiendo el patrón de URL estable `{YEAR}-07/Calendario_{COMP}_{SEASON}.pdf`. Si la web bloquea o cambia el formato, cae al `data/rfef-fallback.json` que contiene los nombres canónicos hardcodeados.

**Cuando cambia la temporada:** GitHub Actions corre el cron mensual, descarga los nuevos PDFs y actualiza el JSON. Si la temporada cambia y el PDF aún no está publicado, el fallback con la lista de equipos del año anterior sigue funcionando hasta que alguien actualice `data/rfef-fallback.json`.

### FCF (manual, ~30 min/año)
Edita `data/fcf-manual.json`:
1. Para cada división donde juegues, rellena el array `teams` con los rivales de la temporada actual
2. Si juegas en un grupo territorial concreto, duplica la división con un id específico (ej. `fcf-1cat-fs-fem-grup-2`)
3. Los escudos los puedes dejar como `null` — el resolver de Wikipedia los rellenará si encuentra el club

Push al repo y GitHub Actions corre solo (o trigger manual desde la UI).

### Novedades de federación (manual)
La app muestra "Novedades de tu liga" (notificación local + bandeja con badge) a partir del campo `notices` del JSON. Se mantienen a mano en `data/notices-manual.json` y `scrape.py` las inyecta en la categoría/división correspondiente:

```jsonc
{
  "categories": {                         // novedad para TODA la federación
    "rfef": [
      { "id": "circ-2026-07",             // id estable (imprescindible para dedup)
        "title": "Nueva normativa de sanciones",
        "body": "…",                      // opcional
        "url": "https://www.rfef.es/…",   // opcional (se abre en el navegador)
        "date": "2026-07-06" }            // opcional (YYYY-MM-DD)
    ]
  },
  "divisions": {                          // novedad solo para una división
    "rfef-segunda-fs-fem": [ { "id": "…", "title": "…" } ]
  }
}
```

- La clave es el `id` de una categoría (`rfef`/`fcf`) o de una división (mira los ids en `output/leagues.json`).
- El `id` de cada novedad debe ser **estable**: la app deduplica por él para no repetir la notificación. Cambiar el `id` = novedad nueva.
- El fichero incluye entradas `ejemplo`; bórralas cuando confirmes el flujo.

## GitHub Actions

El workflow `.github/workflows/scrape.yml`:
- **Cron:** día 1 de cada mes a las 06:00 UTC
- **Manual:** desde la pestaña Actions → "Scrape leagues data" → "Run workflow"
- **Output:** publica `output/leagues.json` en la branch `gh-pages`

URL pública del JSON tras el primer despliegue:
```
https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json
```

Esa URL es la que usará la app Flutter. Configurarla en `lib/leagues_service.dart` cuando se implemente la Fase B.

## Setup inicial (primera vez)

```bash
# 1. Crear repo en GitHub (público)
gh repo create lbcoach-leagues-data --public --source=. --push

# 2. Crear la branch gh-pages
git checkout --orphan gh-pages
git rm -rf .
echo "leagues data" > index.html
git add index.html
git commit -m "init gh-pages"
git push origin gh-pages
git checkout main

# 3. Trigger manual del workflow
gh workflow run "Scrape leagues data"

# 4. Verificar
gh run watch
curl https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json | jq .version
```

## Estructura del JSON publicado

```json
{
  "version": "2025-2026",
  "lastUpdated": "2025-08-15T06:00:00+00:00",
  "categories": [
    {
      "id": "rfef",
      "name": "Liga Española",
      "source": "rfef.es",
      "divisions": [
        {
          "id": "rfef-primera-fs-masc",
          "name": "Primera División FS",
          "gender": "masculino",
          "teams": [
            { "name": "Barça", "logoUrl": "https://upload.wikimedia.org/..." }
          ]
        }
      ]
    },
    { "id": "fcf", "name": "Liga Catalana", "divisions": [...] }
  ]
}
```
