import Link from "next/link";
import { notFound } from "next/navigation";
import LineChart from "@/components/LineChart";
import { Change, Section, Stat } from "@/components/ui";
import { getAlerts, getHistory, getInstrument, getSeriesInfo, getSummary } from "@/lib/db";
import { RULE_LABELS, num, price } from "@/lib/format";

export default async function AssetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [instrument, summary] = await Promise.all([getInstrument(id), getSummary()]);
  const s = summary.find((x) => x.instrument_id === id);
  if (!instrument || !s) notFound();

  const [history, info, alerts] = await Promise.all([
    getHistory(s.series_id), getSeriesInfo(s.series_id), getAlerts({ instrument: id, limit: 50 }),
  ]);
  const data = history.map((p) => ({ time: p.as_of, value: Number(p.value) }));
  const unofficial = Boolean(info?.attributes?.["unofficial"]);
  const proxy = info?.attributes?.["proxy"] as string | undefined;

  return (
    <>
      <p className="lead" style={{ marginTop: 16 }}><Link href="/">← Mercados</Link></p>
      <h1>{instrument.name}</h1>
      <p className="lead">
        {[instrument.symbol, instrument.exchange, s.unit].filter(Boolean).join(" · ")}
        {unofficial && <> · <span className="tag warn">fuente no oficial</span></>}
        {proxy && <> · <span className="tag">{proxy}</span></>}
      </p>

      <div className="grid-stats" style={{ marginTop: 12 }}>
        <Stat label={`Último (${s.last_date})`} value={price(s.last_value, s.is_rate)}
          sub={<>1 día: <Change value={s.chg_1d} isRate={s.is_rate} /></>} />
        <Stat label="1 mes" value={<Change value={s.chg_1m} isRate={s.is_rate} />}
          sub={<>3 meses: <Change value={s.chg_3m} isRate={s.is_rate} /></>} />
        <Stat label="En el año" value={<Change value={s.chg_ytd} isRate={s.is_rate} />}
          sub={`Rango 52 sem.: ${price(s.lo_52s, s.is_rate)} – ${price(s.hi_52s, s.is_rate)}`} />
        <Stat label="Volatilidad anualizada" value={`${num(s.vol_20, 1)}${s.is_rate ? " pp" : " %"}`}
          sub={`20 sesiones · 60 sesiones: ${num(s.vol_60, 1)}${s.is_rate ? " pp" : " %"}`} />
      </div>

      <Section title="Histórico desde 2020" note={`${data.length} sesiones · fuente: ${info?.source_id ?? "—"}${info?.source_code ? ` (${info.source_code})` : ""}`}>
        <LineChart series={[{ name: instrument.name, data }]} height={380}
          precision={Math.abs(s.last_value) < 10 ? 4 : 2} />
      </Section>

      <Section title="Alertas de este instrumento">
        {alerts.length === 0 ? <p className="muted">Sin alertas registradas.</p> : (
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

      {instrument.notes && <p className="muted">{instrument.notes}</p>}
    </>
  );
}
