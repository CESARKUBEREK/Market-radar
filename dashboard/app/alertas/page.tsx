import Link from "next/link";
import { Section } from "@/components/ui";
import { getAlerts } from "@/lib/db";
import { RULE_LABELS, daysAgo } from "@/lib/format";

const RANGES: [string, string, number][] = [["7d", "7 días", 7], ["30d", "30 días", 30], ["90d", "90 días", 90], ["1a", "Año", 365]];

export default async function AlertsPage({ searchParams }: { searchParams: Promise<{ r?: string; t?: string }> }) {
  const { r = "30d", t } = await searchParams;
  const days = RANGES.find(([k]) => k === r)?.[2] ?? 30;
  const alerts = (await getAlerts({ since: daysAgo(days) })).filter((a) => !t || a.rule_id === t);
  const counts = new Map<string, number>();
  for (const a of alerts) counts.set(a.rule_id, (counts.get(a.rule_id) ?? 0) + 1);
  const href = (nr: string, nt?: string) => `/alertas?r=${nr}${nt ? `&t=${nt}` : ""}`;

  return (
    <>
      <h1>Alertas</h1>
      <p className="lead">Reglas de umbral deterministas (versión threshold-v1.0). Describen hechos; no son señales de compra o venta.</p>
      <div className="filters">
        {RANGES.map(([k, label]) => <Link key={k} href={href(k, t)} className={k === r ? "on" : ""}>{label}</Link>)}
      </div>
      <div className="filters">
        <Link href={href(r)} className={!t ? "on" : ""}>Todas ({alerts.length})</Link>
        {Object.entries(RULE_LABELS).map(([k, label]) => (
          <Link key={k} href={href(r, k)} className={t === k ? "on" : ""}>{label}{counts.get(k) ? ` (${counts.get(k)})` : ""}</Link>
        ))}
      </div>
      <Section title={`${alerts.length} alertas`}>
        {alerts.length === 0 ? <p className="muted">Ninguna en este periodo.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Fecha</th><th className="left">Tipo</th><th className="left">Detalle</th><th className="left">Instrumento</th></tr></thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id}>
                    <td>{a.as_of}</td>
                    <td className="left"><span className="tag">{RULE_LABELS[a.rule_id] ?? a.rule_id}</span></td>
                    <td className="left">{a.title}</td>
                    <td className="left">{a.instrument_id ? <Link href={`/activo/${a.instrument_id}`}>ver</Link> : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
