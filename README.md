# Market Radar

Plataforma personal de inteligencia de mercado. Los datos viven en el proyecto
de Supabase **Market Radar**; este repositorio contiene los procesos de ingesta
que se ejecutan en la nube con GitHub Actions.

## Ingesta de FRED

`ingest/fred_ingest.py` descarga las series del catálogo con `source_id = 'fred'`
y las guarda en `observations` con su fecha de publicación real (ALFRED).

| Campo | Cómo se rellena |
| --- | --- |
| `as_of` | Fecha a la que se refiere el dato |
| `vintage` | Fecha de publicación de esa versión en formato AAAAMMDD |
| `published_at` | Esa fecha a las 23:59:59 hora de Nueva York (conservador para el backtest) |
| `value` / `quality_status` | Un "." de FRED se guarda como NULL y `missing` |

Solo se guardan observaciones desde el **1 de enero de 2020**. Las series con
`attributes.vintages = 'latest'` en el catálogo (por ahora, NFCI, que se
recalcula entera cada semana) guardan una sola versión por fecha, la última
publicada; no sirven para un backtest point-in-time.

Tras cada serie, el script guarda en `series.attributes` las unidades y la
frecuencia oficiales de FRED (`fred_units`, `fred_frequency`…) para verificar
el catálogo. Cada ejecución queda en `provider_sync_logs`.

### Puesta en marcha

1. En GitHub: **Settings → Secrets and variables → Actions → New repository secret**, crea:
   - `FRED_API_KEY`: tu clave de la API de FRED.
   - `SUPABASE_URL`: `https://lwbvzmasicdwctcovwsa.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY`: en Supabase, **Project Settings → API Keys**, la clave secreta (service_role). No la compartas ni la pongas en el código.
2. Primera carga: **Actions → Ingesta FRED → Run workflow**, con *backfill* marcado.
3. A partir de ahí se ejecuta sola de lunes a sábado a las 05:17 UTC.

### Ejecución local (opcional)

```bash
pip install -r requirements.txt
export FRED_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
python ingest/fred_ingest.py --backfill --series fred_M2SL,fred_WTREGEN
```

Nota: GitHub desactiva los workflows programados de un repositorio sin actividad
durante 60 días; si ocurre, basta con reactivarlo desde la pestaña Actions.
