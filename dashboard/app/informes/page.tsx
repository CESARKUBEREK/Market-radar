import Link from "next/link";
import { Section } from "@/components/ui";
import { getReports } from "@/lib/db";

const STATUS: Record<string, string> = { sent: "enviado", draft: "borrador", failed: "fallo de envío", skipped: "omitido" };

export default async function ReportsPage() {
  const reports = await getReports();
  return (
    <>
      <h1>Informes diarios</h1>
      <p className="lead">Cada informe se guarda tal como se envió.</p>
      <Section title={`${reports.length} informes`}>
        {reports.length === 0 ? <p className="muted">Todavía no hay informes.</p> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Fecha</th><th>Estado</th><th>Alertas</th><th className="left">Datos atrasados</th></tr></thead>
              <tbody>
                {reports.map((r) => (
                  <tr key={r.report_date}>
                    <td><Link href={`/informes/${r.report_date}`}>{r.report_date}</Link></td>
                    <td className={r.status === "sent" ? "up" : r.status === "failed" ? "down" : "muted"}>{STATUS[r.status] ?? r.status}</td>
                    <td>{String(r.content?.["alerts"] ?? "—")}</td>
                    <td className="left muted">{r.stale_sources?.join(", ") || "—"}</td>
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
