import Link from "next/link";
import { Section } from "@/components/ui";
import { getEvents, getNews } from "@/lib/db";

const KIND: Record<string, string> = {
  fomc: "Fed", cpi: "Inflación", employment: "Empleo", expiry: "Vencimiento", roll: "Vencimiento WTI",
  rebalance: "Rebalanceo", holiday: "Festivo", other: "Macro",
};
const CATEGORY: Record<string, string> = {
  politica_monetaria: "Política monetaria", discurso: "Discurso", estabilidad_financiera: "Estabilidad financiera",
  empresa: "Empresa", supervision: "Supervisión", otros: "Otros",
};
const RANGES: [string, string, number][] = [["7d", "7 días", 7], ["30d", "30 días", 30], ["90d", "90 días", 90]];
const TZ = "Europe/Paris";

const day = (iso: string) => new Intl.DateTimeFormat("es-ES", { timeZone: TZ, weekday: "short", day: "2-digit", month: "short" }).format(new Date(iso));
const hour = (iso: string) => new Intl.DateTimeFormat("es-ES", { timeZone: TZ, hour: "2-digit", minute: "2-digit" }).format(new Date(iso));

export default async function AgendaPage({ searchParams }: { searchParams: Promise<{ r?: string; todas?: string }> }) {
  const { r = "30d", todas } = await searchParams;
  const days = RANGES.find(([k]) => k === r)?.[2] ?? 30;
  const now = new Date();
  const to = new Date(now.getTime() + days * 86400e3);
  const since = new Date(now.getTime() - 14 * 86400e3);
  const minRel = todas ? 0 : 35;
  const [events, news] = await Promise.all([
    getEvents(now.toISOString(), to.toISOString()),
    getNews(since.toISOString(), minRel),
  ]);
  const href = (nr: string, all?: boolean) => `/agenda?r=${nr}${all ? "&todas=1" : ""}`;

  return (
    <>
      <h1>Agenda y noticias</h1>
      <p className="lead">Calendario de bancos centrales, datos macro, festivos y vencimientos, y noticias de fuentes
        oficiales. Las noticias explican las alertas; no las generan. Horas de París.</p>

      <Section title="Próximos eventos" note={`${events.length} en ${days} días`}>
        <div className="filters">
          {RANGES.map(([k, label]) => <Link key={k} href={href(k, Boolean(todas))} className={k === r ? "on" : ""}>{label}</Link>)}
        </div>
        {events.length === 0 ? <p className="muted">Sin eventos en este periodo.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th className="left">Día</th><th>Hora</th><th className="left">Tipo</th><th className="left">Evento</th><th className="left">Mercado</th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id} className={e.kind === "fomc" || e.notes?.startsWith("BCE") ? "strong" : undefined}>
                    <td className="left">{day(e.event_at)}</td>
                    <td>{e.kind === "holiday" ? "" : hour(e.event_at)}</td>
                    <td className="left"><span className="tag">{KIND[e.kind] ?? e.kind}</span></td>
                    <td className="left">{e.notes}{e.suppresses_signals ? <span className="muted"> · picos mecánicos esperables</span> : null}</td>
                    <td className="left">{e.instrument_id ? <Link href={`/activo/${e.instrument_id}`}>{e.country_code ?? e.instrument_id}</Link> : e.country_code}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Noticias oficiales" note="Últimos 14 días · Fed, BCE, Banco de Inglaterra">
        <div className="filters">
          <Link href={href(r)} className={!todas ? "on" : ""}>Relevantes</Link>
          <Link href={href(r, true)} className={todas ? "on" : ""}>Todas</Link>
        </div>
        {news.length === 0 ? <p className="muted">Sin noticias.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th className="left">Fecha</th><th className="left">Fuente</th><th className="left">Titular</th><th className="left">Tema</th><th>Relevancia</th></tr></thead>
              <tbody>
                {news.map((n) => (
                  <tr key={n.id}>
                    <td className="left">{day(n.published_at)}</td>
                    <td className="left">{n.source_name}</td>
                    <td className="left"><a href={n.url} target="_blank" rel="noopener noreferrer">{n.title}</a>
                      {n.news_instruments.length ? <span className="muted"> · {n.news_instruments.map((i) => i.instrument_id).join(", ")}</span> : null}</td>
                    <td className="left"><span className="tag">{CATEGORY[n.categories?.[0]] ?? n.categories?.[0]}</span></td>
                    <td>{n.relevance}</td>
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
