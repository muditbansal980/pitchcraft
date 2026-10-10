"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { PASSWORD_MIN_LENGTH, USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH, validateLogin, validateSignup } from "@/lib/auth-rules";

type Mode = "login" | "signup";

const COPY = {
  login: {
    title: "Log in",
    submit: "Log in",
    pending: "Logging in...",
    switchText: "No account?",
    switchLink: "Sign up",
    switchHref: "/signup",
  },
  signup: {
    title: "Sign up",
    submit: "Create account",
    pending: "Creating account...",
    switchText: "Already have an account?",
    switchLink: "Log in",
    switchHref: "/login",
  },
} as const;

export function AuthForm({ mode }: { mode: Mode }) {
  const router = useRouter();
  const copy = COPY[mode];
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedEmail = email.trim();
    const invalid =
      mode === "signup"
        ? validateSignup(username.trim(), trimmedEmail, password)
        : validateLogin(trimmedEmail, password);
    if (invalid) {
      setError(invalid);
      return;
    }

    setPending(true);
    setError(null);
    try {
      const res = await fetch(`/api/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          mode === "signup"
            ? { username: username.trim(), email: trimmedEmail, password }
            : { email: trimmedEmail, password },
        ),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(data?.error ?? "Something went wrong. Please try again.");
        return;
      }
      router.replace("/create");
      router.refresh();
    } catch {
      setError("Could not reach the server. Please try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="brutal-box flex w-full max-w-md flex-col gap-5 p-6 md:p-8">
      <h1 className="text-3xl font-bold uppercase">{copy.title}</h1>

      {mode === "signup" && (
        <Field label="Username" htmlFor="username">
          <input
            id="username"
            type="text"
            autoComplete="username"
            required
            minLength={USERNAME_MIN_LENGTH}
            maxLength={USERNAME_MAX_LENGTH}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="brutal-input h-12"
          />
        </Field>
      )}

      <Field label="Email" htmlFor="email">
        <input
          id="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="brutal-input h-12"
        />
      </Field>

      <Field
        label="Password"
        htmlFor="password"
        hint={mode === "signup" ? `At least ${PASSWORD_MIN_LENGTH} characters` : undefined}
      >
        <input
          id="password"
          type="password"
          autoComplete={mode === "signup" ? "new-password" : "current-password"}
          required
          minLength={mode === "signup" ? PASSWORD_MIN_LENGTH : undefined}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="brutal-input h-12"
        />
      </Field>

      {error && (
        <p role="alert" className="border-3 border-foreground bg-danger px-3 py-2 font-bold text-black">
          {error}
        </p>
      )}

      <button type="submit" disabled={pending} className="brutal-btn h-12 text-base">
        {pending ? copy.pending : copy.submit}
      </button>

      <p className="font-mono text-sm">
        {copy.switchText}{" "}
        <Link href={copy.switchHref} className="font-bold underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black">
          {copy.switchLink}
        </Link>
      </p>
    </form>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="font-mono text-sm font-bold uppercase">
        {label}
      </label>
      {children}
      {hint && <p className="font-mono text-xs text-muted">{hint}</p>}
    </div>
  );
}
