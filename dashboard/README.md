# Dashboard de Market Radar

Aplicación Next.js de solo lectura sobre la base de datos Market Radar (Supabase).

- Las consultas se hacen **en el servidor** con la clave de servicio; nunca llega al navegador.
- Todo el sitio está protegido con usuario y contraseña (`proxy.ts`). Si falta la contraseña, deniega el acceso.
- Los datos se leen en cada visita (sin caché).

| Página | Contenido |
| --- | --- |
| `/` | Mercados: resumen por categoría, liquidez, últimas alertas y estado de las fuentes |
| `/activo/[id]` | Ficha de instrumento: histórico desde 2020, estadísticas y alertas |
| `/macro` | Liquidez neta de la Fed, componentes, M2, fondos federales, NFCI |
| `/alertas` | Historial de alertas con filtros |
| `/correlaciones` | Correlaciones de 60 sesiones y su evolución |
| `/informes` | Informes diarios guardados |

## Despliegue en Vercel

1. En vercel.com, **Add New → Project** e importa el repositorio `Market-radar`.
2. En **Root Directory** elige `dashboard`. El framework se detecta solo (Next.js).
3. En **Environment Variables** añade:
   - `SUPABASE_URL` = `https://lwbvzmasicdwctcovwsa.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = la misma clave secreta que en GitHub
   - `DASHBOARD_PASSWORD` = una contraseña larga
   - `DASHBOARD_USER` = usuario (opcional; por defecto `cyrano`)
4. **Deploy**. Cada cambio en `main` se vuelve a desplegar solo.

La región de las funciones es Dublín (`dub1`), junto a la base de datos (eu-west-1).

## Desarrollo local

```bash
cp .env.example .env.local   # y rellena las variables
npm install
npm run dev
```
