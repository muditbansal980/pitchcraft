"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/create", label: "New research", icon: "+" },
  { href: "/your-researches", label: "Your researches", icon: "≡" },
];

export function SidebarNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="flex gap-3 md:flex-col">
      {LINKS.map(({ href, label, icon }) => {
        const active = pathname === href;
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={`flex flex-1 items-center gap-3 border-3 border-foreground px-3 py-2.5 font-bold uppercase transition-transform md:flex-none ${
              active
                ? "translate-x-0.5 translate-y-0.5 bg-accent text-black"
                : "bg-surface shadow-brutal-sm hover:-translate-x-px hover:-translate-y-px"
            }`}
          >
            <span aria-hidden="true" className="font-mono text-lg leading-none">
              {icon}
            </span>
            <span className="text-sm">{label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
