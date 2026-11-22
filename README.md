# Datos de mercado públicos · Magdalena Finanzas y M&As

Repositorio **exclusivo para datos públicos de mercado**. GitHub Actions lo actualiza en días hábiles y publica:

- `data/live.json` — series diarias/semanales/mensuales con su fecha real, fuente, unidad, estado y licencia (`licencias`).
- `data/history/*.csv` — histórico auxiliar (`date,value`) de cada serie.

**Qué NO contiene (por diseño):** análisis o información interna, datos de FMP (planes personales), cierres de ICE/CZ App (sin permiso de redistribución confirmado) ni claves. El workflow lo verifica antes de publicar.

Los valores son **cierres, fijaciones, referencias o promedios**; ninguno es una cotización en vivo.

## Fuentes y licencias
Ver [`DATA_LICENSES.md`](DATA_LICENSES.md). Cepea/Esalq se publica bajo CC BY-NC 4.0 con atribución; si su uso no comercial no aplica, ponga `ALLOW_NC: "0"` en el workflow.

## Uso
`https://raw.githubusercontent.com/<usuario>/<repo>/main/data/live.json` (HTTPS, CORS abierto). El código (`tools/update_live.py`, biblioteca estándar de Python) es MIT; los datos conservan la licencia de cada proveedor.
