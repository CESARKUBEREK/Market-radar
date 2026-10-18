import Link from "next/link";
import { notFound } from "next/navigation";
import { getReportHtml } from "@/lib/db";

export default async function ReportPage({ params }: { params: Promise<{ fecha: string }> }) {
  const { fecha } = await params;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(fecha)) notFound();
  const html = await getReportHtml(fecha);
  if (!html) notFound();
  return (
    <>
      <p className="lead" style={{ marginTop: 16 }}><Link href="/informes">← Informes</Link></p>
      <h1>Informe del {fecha}</h1>
      {/* El HTML lo genera nuestro propio script; se muestra aislado en un iframe sin scripts. */}
      <iframe className="report-frame" sandbox="" srcDoc={html} title={`Informe ${fecha}`} />
    </>
  );
}
