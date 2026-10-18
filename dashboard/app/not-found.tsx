import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <h1>No encontrado</h1>
      <p className="lead"><Link href="/">Volver a Mercados</Link></p>
    </>
  );
}
