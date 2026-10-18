# Market Radar

Plataforma personal de inteligencia de mercado. Los datos viven en el proyecto
de Supabase **Market Radar**; este repositorio contiene los procesos de ingesta
que se ejecutan en la nube con GitHub Actions.

## Fuentes

| Script | Fuente | Series | `published_at` | Revisiones |
| --- | --- | --- | --- | --- |
| `ingest/fred_ingest.py` | FRED / ALFRED | Macro, tipos, S&P 500, Nasdaq-100, Bitcoin (Coinbase), Brent, WTI, EUR/USD | Fecha real de publicación (ALFRED), 23:59:59 hora de Nueva York | Cada revisión es una fila (`vintage` = AAAAMMDD) |
| `ingest/tiingo_ingest.py` | Tiingo (plan gratuito, uso personal) | RTX, YPF y Alibaba (ADR en NYSE), Russell 2000 vía ETF IWM | Estimada: día de cotización a las 20:00 hora de Nueva York | Se sobrescribe (`vintage` = 1) |
| `ingest/ecb_ingest.py` | BCE | CHF/EUR (1 / tipo de referencia EUR/CHF) | Estimada: día a las 16:30 hora de Fráncfort | Se sobrescribe (`vintage` = 1) |

Reglas comunes:

- Solo se guardan observaciones desde el **1 de enero de 2020**.
- Un dato ausente se guarda como `NULL` con `quality_status = 'missing'`, nunca como cero.
- Tiingo guarda el cierre **sin ajustar**, que no cambia con dividendos ni splits.
- En FRED, las series con `attributes.vintages = 'latest'` (por ahora NFCI)
  guardan una sola versión por fecha; no sirven para un backtest point-in-time.
- Cada ejecución queda registrada en `provider_sync_logs`.

## Puesta en marcha

Secretos del repositorio (**Settings → Secrets and variables → Actions**):

| Secreto | Valor |
| --- | --- |
| `SUPABASE_URL` | `https://lwbvzmasicdwctcovwsa.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Clave secreta de Market Radar (Supabase → Project Settings → API Keys) |
| `FRED_API_KEY` | Clave de la API de FRED |
| `TIINGO_API_KEY` | Clave de la API de Tiingo |

El workflow **Ingesta diaria** se ejecuta de lunes a sábado a las 05:17 UTC.
Para una carga completa: **Actions → Ingesta diaria → Run workflow**, elige la
fuente y marca *backfill*.

## Carga manual (oro, plata, cobre)

Estas series tienen `source_id = 'manual'`. Para añadir cierres, en Supabase
abre **SQL Editor** y adapta esta plantilla:

```sql
insert into public.observations (series_id, as_of, vintage, published_at, value)
values
  ('px_gold_close',   '2026-10-16', 1, '2026-10-16 23:00+00', 4250.10),
  ('px_silver_close', '2026-10-16', 1, '2026-10-16 23:00+00', 52.30)
on conflict (series_id, as_of, vintage)
do update set value = excluded.value, published_at = excluded.published_at,
              ingested_at = now();
```

`published_at` es cuándo se conoció ese cierre. Los valores del ejemplo son
ilustrativos.

## Ejecución local (opcional)

```bash
pip install -r requirements.txt
export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... FRED_API_KEY=... TIINGO_API_KEY=...
python ingest/tiingo_ingest.py --backfill --series px_rtx_close
```

Nota: GitHub desactiva los workflows programados de un repositorio sin actividad
durante 60 días; si ocurre, basta con reactivarlo desde la pestaña Actions.
