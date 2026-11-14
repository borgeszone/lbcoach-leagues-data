# Plan B: el scraper desde un PC de casa

El scraper corre en GitHub Actions, cada 4 horas. Cada run sale por una IP
distinta del pool de GitHub, y eso es lo que lo hace funcionar: la PNFG deja
unas 15-20 peticiones **por IP** antes de cortar (ver `scrapers/pnfg_pacer.py`),
y un día entero de trabajo son unas 35 —el calendario de cada grupo en una, como
mucho 8 páginas de jornada para las actas y, una vez por semana, qué divisiones
hay—. Así que cada run hace lo que cabe, en tandas de 6 con 15 minutos de
silencio entre ellas, se para al primer corte, y el siguiente se salta los
grupos que se bajaron en las últimas 20 h.

Este plan es para el día en que eso deje de funcionar: **si la PNFG empieza a
bloquear las IP de centros de datos**, o si el cupo baja tanto que ni repartido
cabe. Hoy (14 de noviembre de 2026) no pasa: los 40 últimos runs terminaron en
verde.

## Cómo se sabe que toca

- El run de Actions sale con el aviso **«La PNFG ha cortado»**.
- En `leagues.json`, `warnings` dice «la PNFG cortó a mitad de run».
- El panel enseña grupos con menos jornadas de las que tienen.

**Un aviso suelto no es motivo**: lo que falta lo pone la caché y lo pide el run
siguiente, que sale por otra IP. Toca actuar si se repite varios días
seguidos. Antes del plan B, prueba lo barato en `.github/workflows/scrape.yml`:
bajar `PNFG_ACTAS_MAX` a 4 y `PNFG_BURST` a 4.

## Qué es

Un *self-hosted runner*: un programa de GitHub que se instala en un PC y recoge
los trabajos de Actions de este repo. El workflow es el mismo y publica igual en
gh-pages. Lo único que cambia es que las peticiones salen por la IP de casa.

El precio es que **ese PC tiene que estar encendido** a las horas del cron: cada
4 horas, al minuto 17 (`17 */4 * * *`, en UTC), y cada run dura de 20 a 50
minutos, casi todo en las pausas entre tandas. Si está apagado, el trabajo espera
en cola hasta 24 h y luego falla; como el workflow no deja dos runs a la vez
(`concurrency`), en cola solo queda el último.

**Y hay un segundo precio, que conviene saber antes de activarlo**: el reparto
entre runs se apoya en que cada uno sale por una IP nueva. Con el runner de casa
todos salen por la misma, así que un día de trabajo (~35 peticiones) pasa a
depender de que el cupo de la IP se recargue entre un run y el siguiente. Eso
**no se ha medido**: lo medido es que las pausas de 15 minutos apenas lo
recargan. Si se activa, conviene vigilar los primeros días que los grupos se
siguen completando, y si no, bajar `PNFG_ACTAS_MAX`.

## Activarlo (unos 10 minutos)

En el PC de casa tiene que estar instalado **Git for Windows**, que trae el
`bash` que usan los pasos del workflow. Python no hace falta: lo instala el
propio workflow.

1. En GitHub: **Settings → Actions → Runners → New self-hosted runner**, y elige
   Windows x64.
2. Sigue los comandos que te enseña esa página en PowerShell, en una carpeta
   tipo `C:\actions-runner`. En el paso de `config.cmd`, acepta la etiqueta por
   defecto (`self-hosted`) y responde **sí** a ejecutarlo como servicio, para
   que arranque solo con Windows.
3. En **Settings → Secrets and variables → Actions → Variables**, crea la
   variable `SCRAPER_RUNNER` con el valor `self-hosted`.
4. Lanza el workflow a mano (**Actions → Scrape leagues data → Run workflow**)
   y comprueba que corre en tu PC y que termina publicando.

## Volver a GitHub

Borra la variable `SCRAPER_RUNNER`. El runner de casa puede quedarse instalado
sin hacer nada, o quitarse desde la misma pantalla de Runners.

## Seguridad: no lo actives sin leer esto

GitHub desaconseja los runners propios en repositorios **públicos**, y este lo
es (gh-pages tiene que serlo). Un runner ejecuta el código del workflow en tu
PC, así que cualquier workflow que pudiera lanzar alguien de fuera ejecutaría su
código en tu casa.

Hoy no hay forma de hacerlo: los dos workflows solo se lanzan con el cron o a
mano (`schedule` y `workflow_dispatch`), nunca con un `pull_request`. Para que
siga así:

- **No añadas nunca** un disparador `pull_request` o `pull_request_target` a un
  workflow mientras el runner de casa esté activo.
- En **Settings → Actions → General**, deja marcado que los workflows de forks
  necesiten aprobación.
- Instala el runner con un usuario de Windows sin permisos de administrador si
  puedes.
