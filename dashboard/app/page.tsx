import Link from "next/link";
import { Change, Section, Stat } from "@/components/ui";
import { getAlerts, getM2, getNetLiquidity, getSummary, getSyncStatus } from "@/lib/db";
import { CATEGORIES, RULE_LABELS, STALE_DAYS, daysAgo, num, price, signed } from "@/lib/format";

export default async function MarketsPage() {
  const [summary, alerts, liquidity, m2, sync] = await Promise.all([
    getSummary(), getAlerts({ since: daysAgo(10), limit: 12 }), getNetLiquidity(), getM2(), getSyncStatus(),
  ]);
  const liq = liquidity.at(-1);
  const lastM2 = m2.at(-1);
  const stale = summary.filter((s) => s.days_since_last > STALE_DAYS);
  const failed = sync.filter((s) => s.status === "failed" || s.status === "partial");

  return (
    <>
      <h1>Mercados</h1>
      <p className="lead">Último cierre de cada instrumento y su evolución reciente.</p>

      {(stale.length > 0 || failed.length > 0) && (
        <div className="notice">
          {stale.length > 0 && <div>Datos atrasados: {stale.map((s) => s.instrument).join(", ")}.</div>}
          {failed.length > 0 && <div>Fuentes con errores en la última descarga: {failed.map((s) => s.source_id).join(", ")}.</div>}
        </div>
      )}

      <div className="grid-stats" style={{ marginTop: 16 }}>
        <Stat label="Liquidez neta de la Fed" value={liq ? `${num(liq.net_liquidity_b, 0)} mm $` : "—"}
          sub={liq ? `${signed(liq.chg_4w_b, 0)} mm en 4 semanas · ${liq.as_of}` : null} />
        <Stat label="M2 interanual" value={lastM2 ? `${num(lastM2.chg_yoy_pct, 2)} %` : "—"}
          sub={lastM2 ? `${num(lastM2.m2_b, 0)} mm $ · ${lastM2.as_of.slice(0, 7)}` : null} />
        <Stat label="Alertas en 10 días" value={alerts.length} sub={<Link href="/alertas">Ver todas</Link>} />
        <Stat label="Instrumentos" value={summary.length}
          sub={stale.length ? `${stale.length} atrasados` : "Todos al día"} />
      </div>

      {CATEGORIES.map(([cat, label]) => {
        const rows = summary.filter((s) => s.category === cat);
        if (!rows.length) return null;
        return (
          <Section key={cat} title={label}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instrumento</th><th>Último</th><th>1 día</th><th>1 semana</th><th>1 mes</th>
                    <th>3 meses</th><th>Año</th><th>Vol. 20d</th><th>Rango 52 sem.</th><th>Fecha</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.series_id}>
                      <td><Link href={`/activo/${s.instrument_id}`}>{s.instrument}</Link>
                        {s.days_since_last > STALE_DAYS && <> <span className="tag warn">atrasado</span></>}</td>
                      <td>{price(s.last_value, s.is_rate)}</td>
                      <td><Change value={s.chg_1d} isRate={s.is_rate} /></td>
                      <td><Change value={s.chg_1w} isRate={s.is_rate} /></td>
                      <td><Change value={s.chg_1m} isRate={s.is_rate} /></td>
                      <td><Change value={s.chg_3m} isRate={s.is_rate} /></td>
                      <td><Change value={s.chg_ytd} isRate={s.is_rate} /></td>
                      <td>{num(s.vol_20, 1)}{s.is_rate ? " pp" : " %"}</td>
                      <td className="muted">{price(s.lo_52s, s.is_rate)} – {price(s.hi_52s, s.is_rate)}</td>
                      <td className="muted">{s.last_date.slice(5)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        );
      })}

      <Section title="Últimas alertas" note={<Link href="/alertas">Historial completo</Link>}>
        {alerts.length === 0 ? <p className="muted">Sin alertas en los últimos 10 días.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Fecha</th><th className="left">Tipo</th><th className="left">Detalle</th></tr></thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id}>
                    <td>{a.as_of}</td>
                    <td className="left"><span className="tag">{RULE_LABELS[a.rule_id] ?? a.rule_id}</span></td>
                    <td className="left">{a.title}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Estado de las fuentes" note="Última descarga de cada una">
        <div className="table-wrap">
          <table>
            <thead><tr><th>Fuente</th><th>Estado</th><th>Filas</th><th>Inicio (UTC)</th></tr></thead>
            <tbody>
              {sync.map((s) => (
                <tr key={s.source_id}>
                  <td>{s.source_id}</td>
                  <td className={s.status === "ok" ? "up" : "down"}>{s.status}</td>
                  <td>{num(s.rows_imported, 0)}</td>
                  <td className="muted">{s.started_at.slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
