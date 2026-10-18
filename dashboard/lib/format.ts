const nf = (dec: number) =>
  new Intl.NumberFormat("es-ES", { minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: "always" });

export function num(x: number | null | undefined, dec = 2): string {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return "—";
  return nf(dec).format(Number(x));
}

export function price(x: number | null | undefined, isRate = false): string {
  if (x === null || x === undefined) return "—";
  if (isRate) return `${num(x, 2)} %`;
  const v = Math.abs(Number(x));
  return num(x, v < 10 ? 4 : v < 100000 ? 2 : 0);
}

export function change(x: number | null | undefined, isRate = false): { text: string; sign: -1 | 0 | 1 } {
  if (x === null || x === undefined) return { text: "—", sign: 0 };
  const v = Number(x);
  const sign = v > 0.0005 ? 1 : v < -0.0005 ? -1 : 0;
  return { text: `${v > 0 ? "+" : ""}${num(v, 2)} ${isRate ? "pp" : "%"}`, sign };
}

export const CATEGORIES: [string, string][] = [
  ["indices", "Índices"],
  ["acciones", "Acciones"],
  ["metales_materias_primas", "Materias primas"],
  ["cripto", "Cripto"],
  ["divisas", "Divisas"],
  ["tipos_indicadores", "Tipos e indicadores"],
];

export const RULE_LABELS: Record<string, string> = {
  move_3sd: "Movimiento extremo",
  high_52w: "Máximo 52 semanas",
  low_52w: "Mínimo 52 semanas",
  vol_regime: "Volatilidad",
  rate_move: "Tipos",
  net_liquidity_move: "Liquidez",
  fedfunds_change: "Política monetaria",
  macro_release: "Nuevo dato",
};

export function daysAgo(n: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - n);
  return d.toISOString().slice(0, 10);
}

export const STALE_DAYS = 4;

export function signed(x: number | null | undefined, dec = 2): string {
  if (x === null || x === undefined) return "—";
  return `${Number(x) > 0 ? "+" : ""}${num(x, dec)}`;
}
