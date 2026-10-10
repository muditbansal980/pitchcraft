"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

/** Modal shown when a logged-out user tries a restricted action (e.g. generating a plan). */
export function LoginRequiredDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      // A click on the dialog element itself (not its content) is a click on the backdrop.
      onClick={(event) => event.target === dialogRef.current && onClose()}
      aria-labelledby="login-required-title"
      className="m-auto w-[calc(100%-2rem)] max-w-md border-3 border-foreground bg-surface p-0 text-foreground shadow-brutal backdrop:bg-black/60"
    >
      <div className="flex flex-col gap-5 p-6 md:p-8">
        <div className="flex flex-col gap-2">
          <h2 id="login-required-title" className="text-2xl font-bold uppercase">
            Log in to continue
          </h2>
          <p className="font-mono text-sm text-muted">
            Brand research and pitch plans are only available to logged-in users. Log in or create a free
            account to generate a plan.
          </p>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row">
          <Link href="/login" className="brutal-btn h-11 flex-1 px-4">
            Log in
          </Link>
          <Link href="/signup" className="brutal-btn h-11 flex-1 bg-accent-2 px-4">
            Sign up
          </Link>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="self-center font-mono text-sm underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black"
        >
          Not now
        </button>
      </div>
    </dialog>
  );
}
