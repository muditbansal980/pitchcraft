import Link from "next/link";

import { LogoutButton } from "@/components/logout-button";
import { getSessionUser } from "@/lib/session";

export async function SidebarAccount() {
  const user = await getSessionUser();

  if (!user) {
    return (
      <div className="flex gap-2">
        <Link href="/login" className="brutal-btn h-9 flex-1 bg-surface px-3 text-xs text-foreground">
          Log in
        </Link>
        <Link href="/signup" className="brutal-btn h-9 flex-1 px-3 text-xs">
          Sign up
        </Link>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-3 md:flex-col md:items-stretch">
      <div className="min-w-0">
        <p className="truncate font-bold">@{user.username}</p>
        <p className="truncate font-mono text-xs text-muted">{user.email}</p>
      </div>
      <LogoutButton />
    </div>
  );
}
