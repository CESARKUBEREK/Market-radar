import LineChart from "@/components/LineChart";
import { Change, Section, Stat } from "@/components/ui";
import { getHistory, getM2, getNetLiquidity, getSummary } from "@/lib/db";
import { num, signed } from "@/lib/format";

export default async function MacroPage() {
  const [liquidity, m2, fedfunds, nfci, summary] = await Promise.all([
    getNetLiquidity(), getM2(), getHistory("fred_FEDFUNDS"), getHistory("fred_NFCI"), getSummary(),
  ]);
  const liq = liquidity.at(-1);
  const lastM2 = m2.at(-1);
  const ff = fedfunds.at(-1);
  const ffPrev = fedfunds.at(-2);
  const lastNfci = nfci.at(-1);
  const us2 = summary.find((s) => s.instrument_id === "us02y");
  const us10 = summary.find((s) => s.instrument_id === "us10y");
  const t = (d: string) => d;

  return (
    <>
      <h1>Macro y liquidez (EE. UU.)</h1>
      <p className="lead">Liquidez de la Fed, oferta monetaria, tipos y condiciones financieras.</p>

      <div className="grid-stats" style={{ marginTop: 12 }}>
        <Stat label="Liquidez neta de la Fed" value={liq ? `${num(liq.net_liquidity_b, 0)} mm $` : "—"}
          sub={liq ? `${signed(liq.chg_4w_b, 0)} mm en 4 sem. · ${signed(liq.chg_13w_b, 0)} mm en 13 sem.` : null} />
        <Stat label="M2" value={lastM2 ? `${num(lastM2.m2_b, 0)} mm $` : "—"}
          sub={lastM2 ? `${signed(lastM2.chg_yoy_pct)} % interanual · ${lastM2.as_of.slice(0, 7)}` : null} />
        <Stat label="Fondos federales" value={ff ? `${num(ff.value)} %` : "—"}
          sub={ff && ffPrev ? <>Mes: <Change value={Number(ff.value) - Number(ffPrev.value)} isRate /> · {ff.as_of.slice(0, 7)}</> : null} />
        <Stat label="Diferencial 10 años − 2 años"
          value={us2 && us10 ? `${signed(Number(us10.last_value) - Number(us2.last_value))} pp` : "—"}
          sub={us10 && us2 ? `10 años ${num(us10.last_value)} % · 2 años ${num(us2.last_value)} %` : null} />
        <Stat label="Condiciones financieras (NFCI)" value={lastNfci ? num(lastNfci.value) : "—"}
          sub={lastNfci ? `Negativo = más laxas que la media · ${lastNfci.as_of}` : null} />
      </div>

      <div className="two-col">
        <Section title="Liquidez neta de la Fed" note="Balance − cuenta del Tesoro − repos inversos, mm $">
          <LineChart height={280} precision={0}
            series={[{ name: "Liquidez neta", data: liquidity.map((l) => ({ time: t(l.as_of), value: Number(l.net_liquidity_b) })) }]} />
        </Section>
        <Section title="Componentes" note="mm $">
          <LineChart height={280} precision={0} series={[
            { name: "Balance", data: liquidity.map((l) => ({ time: l.as_of, value: Number(l.fed_balance_b) })) },
            { name: "TGA", data: liquidity.map((l) => ({ time: l.as_of, value: Number(l.tga_b) })) },
            { name: "Repos inv.", data: liquidity.map((l) => ({ time: l.as_of, value: Number(l.rrp_b) })) },
          ]} />
        </Section>
        <Section title="M2, variación interanual" note="%">
          <LineChart height={260} zeroLine series={[{ name: "M2 interanual",
            data: m2.filter((m) => m.chg_yoy_pct !== null).map((m) => ({ time: m.as_of, value: Number(m.chg_yoy_pct) })) }]} />
        </Section>
        <Section title="Fondos federales" note="%">
          <LineChart height={260} series={[{ name: "FEDFUNDS",
            data: fedfunds.map((p) => ({ time: p.as_of, value: Number(p.value) })) }]} />
        </Section>
        <Section title="Condiciones financieras (NFCI)" note="Índice; 0 = media histórica">
          <LineChart height={260} zeroLine series={[{ name: "NFCI",
            data: nfci.map((p) => ({ time: p.as_of, value: Number(p.value) })) }]} />
        </Section>
      </div>

      <Section title="Pendiente en este panel">
        <p className="muted">Reservas oficiales de oro (9 países) y deuda pública/PIB (8 países): las series están en el
          catálogo pero aún no se descargan.</p>
      </Section>
    </>
  );
}
