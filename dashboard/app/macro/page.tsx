import LineChart from "@/components/LineChart";
import { Change, Section, Stat } from "@/components/ui";
import { getHistory, getM2, getNetLiquidity, getSeriesFamily, getSummary, type FamilyPoint } from "@/lib/db";
import { num, signed } from "@/lib/format";

export default async function MacroPage() {
  const [liquidity, m2, fedfunds, nfci, summary, gold, debt] = await Promise.all([
    getNetLiquidity(), getM2(), getHistory("fred_FEDFUNDS"), getHistory("fred_NFCI"), getSummary(),
    getSeriesFamily("wgc_gold_tonnes_"), getSeriesFamily("imf_debt_gdp_"),
  ]);
  const goldRows = goldTable(gold);
  const debtRows = debtTable(debt);
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
      <h1>Macro y liquidez</h1>
      <p className="lead">Liquidez de la Fed, oferta monetaria, tipos y condiciones financieras (EE. UU.); reservas de oro y deuda pública.</p>

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

      <Section title="Reservas oficiales de oro" note="Toneladas · FMI (IRFCL), mensual">
        <div className="table-wrap">
          <table>
            <thead><tr><th>País</th><th>Toneladas</th><th>12 meses</th><th>Desde ene. 2020</th><th>Dato de</th></tr></thead>
            <tbody>
              {goldRows.map((g) => (
                <tr key={g.code}>
                  <td>{COUNTRY[g.code] ?? g.code}</td>
                  <td>{num(g.last, 0)}</td>
                  <td><Delta value={g.chg12} /></td>
                  <td><Delta value={g.chgStart} /></td>
                  <td>{g.date.slice(0, 7)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Deuda pública bruta" note="% del PIB · FMI (WEO), anual · en cursiva, proyecciones">
        <div className="table-wrap">
          <table>
            <thead><tr><th>País</th>{DEBT_YEARS.map((y) => <th key={y}>{y}</th>)}<th>Cambio 2020–{thisYear}</th></tr></thead>
            <tbody>
              {debtRows.map((d) => (
                <tr key={d.code}>
                  <td>{COUNTRY[d.code] ?? d.code}</td>
                  {DEBT_YEARS.map((y) => {
                    const p = d.byYear.get(y);
                    return <td key={y} style={p?.is_forecast ? { fontStyle: "italic" } : undefined}>
                      {p ? num(p.value, 0) : "—"}</td>;
                  })}
                  <td><Delta value={d.chg} invert /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}

const COUNTRY: Record<string, string> = {
  USA: "Estados Unidos", DEU: "Alemania", ITA: "Italia", FRA: "Francia", CHN: "China", RUS: "Rusia",
  IND: "India", CHE: "Suiza", JPN: "Japón", GBR: "Reino Unido", ARG: "Argentina",
};
const thisYear = new Date().getFullYear();
const DEBT_YEARS = [2020, thisYear - 2, thisYear - 1, thisYear, thisYear + 2, thisYear + 5]
  .filter((y, i, a) => a.indexOf(y) === i);

function Delta({ value, invert = false }: { value: number | null; invert?: boolean }) {
  if (value === null) return <>—</>;
  const sign = Math.sign(value) * (invert ? -1 : 1);
  return <span className={sign > 0 ? "up" : sign < 0 ? "down" : "flat"}>{signed(value, 0)}</span>;
}

function byCountry(points: FamilyPoint[]) {
  const m = new Map<string, FamilyPoint[]>();
  for (const p of points) {
    const code = p.series_id.slice(-3);
    if (!m.has(code)) m.set(code, []);
    m.get(code)!.push(p);
  }
  return m;
}

function goldTable(points: FamilyPoint[]) {
  return [...byCountry(points)].map(([code, ps]) => {
    const last = ps.at(-1)!;
    const yearAgo = ps.findLast((p) => p.as_of <= `${Number(last.as_of.slice(0, 4)) - 1}${last.as_of.slice(4)}`);
    return {
      code, date: last.as_of, last: Number(last.value),
      chg12: yearAgo ? Number(last.value) - Number(yearAgo.value) : null,
      chgStart: Number(last.value) - Number(ps[0].value),
    };
  }).sort((a, b) => b.last - a.last);
}

function debtTable(points: FamilyPoint[]) {
  return [...byCountry(points)].map(([code, ps]) => {
    const byYear = new Map(ps.map((p) => [Number(p.as_of.slice(0, 4)), p]));
    const a = byYear.get(2020), b = byYear.get(thisYear);
    return { code, byYear, chg: a && b ? Number(b.value) - Number(a.value) : null };
  }).sort((x, y) => Number(y.byYear.get(thisYear)?.value ?? 0) - Number(x.byYear.get(thisYear)?.value ?? 0));
}
