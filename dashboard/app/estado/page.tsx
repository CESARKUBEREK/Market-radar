import { createClient } from "@supabase/supabase-js";
import { connection } from "next/server";

// Página de diagnóstico: muestra qué falta sin revelar ningún secreto.
export default async function StatusPage() {
  await connection();
  const url = process.env.SUPABASE_URL ?? "";
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY ?? "";
  const checks: [string, boolean, string][] = [
    ["SUPABASE_URL definida", Boolean(url), url ? url.replace(/^(https:\/\/[a-z0-9]{4}).*(\.supabase\.co).*$/, "$1…$2") : "falta"],
    ["SUPABASE_URL con formato válido", /^https:\/\/[a-z0-9]+\.supabase\.co\/?$/.test(url), "debe ser https://<proyecto>.supabase.co, sin espacios ni rutas"],
    ["SUPABASE_SERVICE_ROLE_KEY definida", Boolean(key), key ? `${key.length} caracteres, empieza por «${key.slice(0, 7)}»` : "falta"],
    ["Sin espacios ni saltos de línea en la clave", key === key.trim(), key === key.trim() ? "correcto" : "la clave tiene espacios o saltos de línea al principio o al final"],
    ["DASHBOARD_PASSWORD definida", Boolean(process.env.DASHBOARD_PASSWORD), "necesaria para el acceso"],
  ];
  let query = "no probada";
  let ok = false;
  if (url && key) {
    try {
      const c = createClient(url.trim(), key.trim(), { auth: { persistSession: false } });
      const { data, error } = await c.from("v_market_summary").select("instrument").limit(3);
      if (error) query = `error de Supabase: ${error.message}${error.code ? ` (código ${error.code})` : ""}`;
      else { ok = true; query = `correcto: ${(data ?? []).length} filas de prueba`; }
    } catch (e) {
      query = `excepción: ${(e as Error).message}`;
    }
  }
  return (
    <>
      <h1>Estado del dashboard</h1>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Comprobación</th><th className="left">Resultado</th><th className="left">Detalle</th></tr></thead>
          <tbody>
            {checks.map(([name, pass, detail]) => (
              <tr key={name}>
                <td>{name}</td>
                <td className={`left ${pass ? "up" : "down"}`}>{pass ? "sí" : "no"}</td>
                <td className="left">{detail}</td>
              </tr>
            ))}
            <tr>
              <td>Consulta a Market Radar</td>
              <td className={`left ${ok ? "up" : "down"}`}>{ok ? "sí" : "no"}</td>
              <td className="left">{query}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}
