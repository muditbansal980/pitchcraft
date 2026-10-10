import Link from "next/link";
import { Suspense } from "react";

import { SidebarAccount } from "@/components/sidebar-account";
import { SidebarNav } from "@/components/sidebar-nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 flex-col md:flex-row">
      <aside className="flex flex-col gap-5 border-b-3 border-foreground bg-surface p-4 md:sticky md:top-0 md:h-screen md:w-64 md:shrink-0 md:gap-8 md:border-r-3 md:border-b-0 md:p-5">
        <Link
          href="/create"
          className="self-start border-3 border-foreground bg-accent px-3 py-1 text-xl font-bold tracking-tight text-black uppercase shadow-brutal-sm"
        >
          PitchCraft
        </Link>
        <SidebarNav />
        <div className="border-t-3 border-foreground pt-4 md:mt-auto">
          <Suspense fallback={<div className="h-9" />}>
            <SidebarAccount />
          </Suspense>
        </div>
      </aside>
      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}
