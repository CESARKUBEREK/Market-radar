import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Market Radar",
  description: "Inteligencia de mercado de uso personal",
  robots: { index: false, follow: false },
};

const NAV = [
  ["/", "Mercados"],
  ["/macro", "Macro"],
  ["/alertas", "Alertas"],
  ["/agenda", "Agenda"],
  ["/correlaciones", "Correlaciones"],
  ["/informes", "Informes"],
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>
        <header className="topbar">
          <div className="wrap topbar-inner">
            <Link href="/" className="brand">Market Radar</Link>
            <nav>
              {NAV.map(([href, label]) => (
                <Link key={href} href={href}>{label}</Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="wrap">{children}</main>
        <footer className="wrap footer">
          Uso personal. Cobre, MOVE, DXY, divisas, Rheinmetall, MERVAL y TA-35 proceden de Yahoo, fuente no oficial.
          Variaciones en %, salvo tipos (pp). Volatilidad anualizada.
        </footer>
      </body>
    </html>
  );
}
