import Link from "next/link";
import LineChart from "@/components/LineChart";
import { Section } from "@/components/ui";
import { getCorrelationHistory, getCorrelationsLatest } from "@/lib/db";
import { num } from "@/lib/format";

export default async function CorrelationsPage({ searchParams }: { searchParams: Promise<{ p?: string }> }) {
  const latest = await getCorrelationsLatest();
  const { p } = await searchParams;
  const selected = latest.find((c) => c.pair_id === p) ?? latest[0];
  const history = selected ? await getCorrelationHistory(selected.pair_id) : [];

  return (
    <>
      <h1>Correlaciones</h1>
      <p className="lead">Correlación móvil de 60 sesiones entre rendimientos diarios. Es una relación estadística, no causal.</p>
      <Section title="Pares">
        <div className="table-wrap">
          <table>
            <thead><tr><th>Par</th><th>Actual</th><th>Hace 3 meses</th><th>Mín. 1 año</th><th>Máx. 1 año</th><th></th><th>Dato de</th></tr></thead>
            <tbody>
              {latest.map((c) => {
                const pos = c.min_1y !== null && c.max_1y !== null && c.max_1y > c.min_1y
                  ? (c.corr_60 - c.min_1y) / (c.max_1y - c.min_1y) : null;
                const flag = pos === null ? "" : pos <= 0.05 ? "mínimo del año" : pos >= 0.95 ? "máximo del año" : "";
                return (
                  <tr key={c.pair_id}>
                    <td><Link href={`/correlaciones?p=${c.pair_id}`}>{c.label}</Link></td>
                    <td><strong>{num(c.corr_60)}</strong></td>
                    <td>{num(c.corr_3m_ago)}</td>
                    <td>{num(c.min_1y)}</td>
                    <td>{num(c.max_1y)}</td>
                    <td>{flag && <span className="tag warn">{flag}</span>}</td>
                    <td className="muted">{c.as_of}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Section>
      {selected && (
        <Section title={selected.label} note="Correlación de 60 sesiones; −1 a +1">
          <LineChart height={320} zeroLine series={[{ name: selected.label,
            data: history.map((h) => ({ time: h.as_of, value: Number(h.corr_60) })) }]} />
        </Section>
      )}
    </>
  );
}
