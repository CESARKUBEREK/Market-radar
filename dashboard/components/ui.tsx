import { change } from "@/lib/format";

export function Change({ value, isRate = false }: { value: number | null | undefined; isRate?: boolean }) {
  const c = change(value, isRate);
  return <span className={c.sign > 0 ? "up" : c.sign < 0 ? "down" : "flat"}>{c.text}</span>;
}

export function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  );
}

export function Section({ title, note, children }: { title: string; note?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {note ? <span className="section-note">{note}</span> : null}
      </div>
      {children}
    </section>
  );
}
