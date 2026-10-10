import Link from "next/link";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-8 px-4 py-12">
      <Link
        href="/create"
        className="border-3 border-foreground bg-accent px-4 py-1.5 text-3xl font-bold tracking-tight text-black uppercase shadow-brutal"
      >
        PitchCraft
      </Link>
      {children}
    </main>
  );
}
