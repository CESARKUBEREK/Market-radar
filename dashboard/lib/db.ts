import "server-only";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { connection } from "next/server";

// Lectura en servidor con la clave de servicio. Nunca llega al navegador.
let client: SupabaseClient | null = null;

async function db(): Promise<SupabaseClient> {
  await connection(); // datos en vivo: cada petición lee la base de datos
  if (!client) {
    // trim(): un salto de línea al pegar la variable en Vercel rompe la conexión.
    const url = process.env.SUPABASE_URL?.trim().replace(/\/+$/, "");
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY?.trim();
    if (!url || !key) throw new Error("Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY");
    client = createClient(url, key, { auth: { persistSession: false } });
  }
  return client;
}

export type Summary = {
  instrument_id: string;
  instrument: string;
  category: string;
  series_id: string;
  unit: string;
  is_rate: boolean;
  last_date: string;
  last_value: number;
  chg_1d: number | null;
  chg_1w: number | null;
  chg_1m: number | null;
  chg_3m: number | null;
  chg_ytd: number | null;
  vol_20: number | null;
  vol_60: number | null;
  hi_52s: number | null;
  lo_52s: number | null;
  days_since_last: number;
};

export type Alert = {
  id: number;
  as_of: string;
  rule_id: string;
  kind: string;
  title: string;
  instrument_id: string | null;
  series_id: string | null;
};

export type Point = { as_of: string; value: number };

async function all<T>(build: (c: SupabaseClient) => any, pageSize = 1000): Promise<T[]> {
  // PostgREST devuelve como máximo 1.000 filas por petición: se pagina.
  const c = await db();
  const out: T[] = [];
  for (let from = 0; ; from += pageSize) {
    const { data, error } = await build(c).range(from, from + pageSize - 1);
    if (error) throw new Error(error.message);
    out.push(...(data as T[]));
    if (!data || data.length < pageSize) return out;
  }
}

export async function getSummary(): Promise<Summary[]> {
  return all<Summary>((c) => c.from("v_market_summary").select("*").order("instrument"));
}

export async function getAlerts(opts: { since?: string; instrument?: string; limit?: number } = {}) {
  const c = await db();
  let q = c.from("alerts").select("id,as_of,rule_id,kind,title,instrument_id,series_id")
    .order("as_of", { ascending: false }).order("rule_id");
  if (opts.since) q = q.gte("as_of", opts.since);
  if (opts.instrument) q = q.eq("instrument_id", opts.instrument);
  if (opts.limit) q = q.limit(opts.limit);
  const { data, error } = await q;
  if (error) throw new Error(error.message);
  return (data ?? []) as Alert[];
}

export async function getHistory(seriesId: string): Promise<Point[]> {
  return all<Point>((c) =>
    c.from("v_observations_latest").select("as_of,value")
      .eq("series_id", seriesId).eq("quality_status", "ok").order("as_of"));
}

export async function getInstrument(id: string) {
  const c = await db();
  const { data, error } = await c.from("instruments").select("*").eq("id", id).maybeSingle();
  if (error) throw new Error(error.message);
  return data as { id: string; name: string; category: string; symbol: string | null;
    exchange: string | null; currency: string | null; notes: string | null } | null;
}

export async function getSeriesInfo(seriesId: string) {
  const c = await db();
  const { data, error } = await c.from("series")
    .select("id,name,unit,source_id,source_code,attributes").eq("id", seriesId).maybeSingle();
  if (error) throw new Error(error.message);
  return data as { id: string; name: string; unit: string; source_id: string;
    source_code: string | null; attributes: Record<string, unknown> } | null;
}

export type FamilyPoint = { series_id: string; as_of: string; value: number; is_forecast: boolean };

/** Todas las observaciones vigentes de las series cuyo ID empieza por `prefix`. */
export async function getSeriesFamily(prefix: string): Promise<FamilyPoint[]> {
  return all<FamilyPoint>((c) =>
    c.from("v_observations_latest").select("series_id,as_of,value,is_forecast")
      .like("series_id", `${prefix}%`).eq("quality_status", "ok").order("series_id").order("as_of"));
}

export async function getNetLiquidity() {
  return all<{ as_of: string; fed_balance_b: number; tga_b: number; rrp_b: number;
    net_liquidity_b: number; chg_4w_b: number | null; chg_13w_b: number | null }>(
    (c) => c.from("v_net_liquidity").select("*").order("as_of"));
}

export async function getM2() {
  return all<{ as_of: string; m2_b: number; chg_mom_pct: number | null; chg_yoy_pct: number | null }>(
    (c) => c.from("v_m2").select("*").order("as_of"));
}

export async function getCorrelationsLatest() {
  const c = await db();
  const { data, error } = await c.from("v_correlations_latest").select("*").order("pair_id");
  if (error) throw new Error(error.message);
  return (data ?? []) as { pair_id: string; label: string; as_of: string; corr_60: number;
    min_1y: number | null; max_1y: number | null; corr_3m_ago: number | null }[];
}

export async function getCorrelationHistory(pairId: string) {
  return all<{ as_of: string; corr_60: number }>((c) =>
    c.from("v_correlations").select("as_of,corr_60").eq("pair_id", pairId).order("as_of"));
}

export async function getReports(limit = 60) {
  const c = await db();
  const { data, error } = await c.from("daily_reports")
    .select("report_date,status,sent_at,stale_sources,content").order("report_date", { ascending: false }).limit(limit);
  if (error) throw new Error(error.message);
  return (data ?? []) as { report_date: string; status: string; sent_at: string | null;
    stale_sources: string[]; content: Record<string, unknown> }[];
}

export type Analysis = {
  report_date: string;
  model: string | null;
  titular: string;
  hechos: { texto: string }[];
  interpretaciones: { texto: string; confianza: "alta" | "media" | "baja" }[];
  hipotesis: { si: string; entonces: string; horizonte: string; invalidacion: string }[];
};

/** Lectura del día del informe más reciente que tenga análisis con IA. */
export async function getLatestAnalysis(): Promise<Analysis | null> {
  const c = await db();
  const { data, error } = await c.from("daily_reports")
    .select("report_date,model:content->ai->>model,result:content->ai->result")
    .not("content->ai", "is", null).order("report_date", { ascending: false }).limit(1).maybeSingle();
  if (error) throw new Error(error.message);
  if (!data?.result) return null;
  const r = data.result as Record<string, any>;
  return {
    report_date: data.report_date as string, model: (data.model as string) ?? null, titular: r.titular ?? "",
    hechos: r.hechos ?? [], interpretaciones: r.interpretaciones ?? [], hipotesis: r.hipotesis ?? [],
  };
}

export async function getReportHtml(date: string): Promise<string | null> {
  const c = await db();
  const { data, error } = await c.from("daily_reports").select("html").eq("report_date", date).maybeSingle();
  if (error) throw new Error(error.message);
  return (data?.html as string) ?? null;
}

export async function getSyncStatus() {
  const c = await db();
  const { data, error } = await c.from("provider_sync_logs")
    .select("source_id,status,started_at,finished_at,rows_imported").order("started_at", { ascending: false }).limit(40);
  if (error) throw new Error(error.message);
  const latest = new Map<string, any>();
  for (const r of data ?? []) if (!latest.has(r.source_id)) latest.set(r.source_id, r);
  return [...latest.values()] as { source_id: string; status: string; started_at: string;
    finished_at: string | null; rows_imported: number | null }[];
}

export type CalendarEvent = { id: number; event_at: string; kind: string; instrument_id: string | null;
  country_code: string | null; source_id: string; suppresses_signals: boolean; notes: string | null };

export async function getEvents(fromIso: string, toIso: string, kinds?: string[]) {
  return all<CalendarEvent>((c) => {
    let q = c.from("events_calendar").select("*").gte("event_at", fromIso).lte("event_at", toIso);
    if (kinds?.length) q = q.in("kind", kinds);
    return q.order("event_at").order("id");
  });
}

export type NewsItem = { id: number; title: string; source_name: string; url: string; published_at: string;
  summary: string | null; categories: string[]; relevance: number;
  news_instruments: { instrument_id: string }[] };

export async function getNews(sinceIso: string, minRelevance = 0, limit = 200) {
  const c = await db();
  const { data, error } = await c.from("news")
    .select("id,title,source_name,url,published_at,summary,categories,relevance,news_instruments(instrument_id)")
    .gte("published_at", sinceIso).gte("relevance", minRelevance)
    .order("published_at", { ascending: false }).limit(limit);
  if (error) throw new Error(error.message);
  return (data ?? []) as NewsItem[];
}
