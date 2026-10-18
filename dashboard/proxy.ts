import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Acceso con usuario y contraseña (HTTP Basic). Uso personal: un único usuario.
// Si falta la contraseña en el entorno, se deniega todo (falla cerrado).
export function proxy(request: NextRequest) {
  const user = process.env.DASHBOARD_USER || "cyrano";
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) {
    return new NextResponse("Falta DASHBOARD_PASSWORD en el entorno.", { status: 503 });
  }
  const header = request.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    const decoded = atob(header.slice(6));
    const sep = decoded.indexOf(":");
    if (sep >= 0 && decoded.slice(0, sep) === user && decoded.slice(sep + 1) === password) {
      return NextResponse.next();
    }
  }
  return new NextResponse("Acceso restringido", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Market Radar", charset="UTF-8"' },
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
