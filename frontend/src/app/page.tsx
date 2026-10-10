"use client";

import { useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

type Source = {
  title: string;
  url: string;
  snippet: string;
};

type ResearchSection = {
  title: string;
  summary: string;
  sources: Source[];
};

type PitchReference = {
  pitch_id: string;
  campaign: string;
  client: string;
};

type BrandPlanResponse = {
  brand?: string;
  plan?: string;
  research?: ResearchSection[];
  references?: PitchReference[];
  error?: string;
};

// Markdown headings are shifted down two levels: the page already has an <h1>
// and the result region an <h2>, so the plan's "# Title" renders as an <h3>.
const markdownComponents: Components = {
  h1: ({ children }) => (
    <h3 className="mt-2 mb-4 text-2xl font-semibold tracking-tight">{children}</h3>
  ),
  h2: ({ children }) => (
    <h4 className="mt-8 mb-3 border-b border-zinc-200 pb-1 text-xl font-semibold tracking-tight dark:border-zinc-800">
      {children}
    </h4>
  ),
  h3: ({ children }) => <h5 className="mt-6 mb-2 text-lg font-semibold">{children}</h5>,
  h4: ({ children }) => <h6 className="mt-4 mb-2 font-semibold">{children}</h6>,
  h5: ({ children }) => <h6 className="mt-4 mb-2 font-semibold">{children}</h6>,
  h6: ({ children }) => <h6 className="mt-4 mb-2 font-semibold">{children}</h6>,
  p: ({ children }) => <p className="my-3 leading-7">{children}</p>,
  ul: ({ children }) => <ul className="my-3 list-disc space-y-1.5 pl-6 leading-7">{children}</ul>,
  ol: ({ children, start }) => (
    <ol start={start} className="my-3 list-decimal space-y-1.5 pl-6 leading-7">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-1 marker:text-zinc-400">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="underline underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-4 border-l-4 border-zinc-300 pl-4 text-zinc-600 italic dark:border-zinc-700 dark:text-zinc-400">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-8 border-zinc-200 dark:border-zinc-800" />,
  code: ({ children, className }) => (
    <code
      className={`rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[0.875em] dark:bg-zinc-800 ${className ?? ""}`}
    >
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="my-4 overflow-x-auto rounded-lg bg-zinc-100 p-4 text-sm dark:bg-zinc-900 [&>code]:bg-transparent [&>code]:p-0">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-4 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      style={style}
      className="border-b border-zinc-300 px-3 py-2 text-left font-semibold dark:border-zinc-700"
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td style={style} className="border-b border-zinc-200 px-3 py-2 align-top dark:border-zinc-800">
      {children}
    </td>
  ),
};

export default function Home() {
  const [brand, setBrand] = useState("");
  const [response, setResponse] = useState<BrandPlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!brand.trim()) return;

    setLoading(true);
    setResponse(null);
    setCopied(false);
    try {
      const res = await fetch("/api/brand-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brand }),
      });
      const data: BrandPlanResponse | null = await res.json().catch(() => null);
      if (!res.ok || !data) {
        setResponse({ error: data?.error ?? "Plan generation failed. Please try again." });
      } else {
        setResponse(data);
      }
    } catch {
      setResponse({ error: "Could not reach the server. Please try again." });
    } finally {
      setLoading(false);
    }
  }

  async function handleCopy() {
    if (!response?.plan) return;
    try {
      await navigator.clipboard.writeText(response.plan);
      setCopied(true);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (e.g. insecure context); fail silently.
    }
  }

  const research = response?.research?.filter((section) => section.sources.length > 0) ?? [];
  const references = response?.references ?? [];

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">PitchCraft</h1>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
        <label htmlFor="brand" className="sr-only">
          Brand name
        </label>
        <input
          id="brand"
          type="text"
          value={brand}
          onChange={(e) => setBrand(e.target.value)}
          placeholder="Enter brand name"
          className="h-11 flex-1 rounded-lg border border-zinc-300 bg-transparent px-4 outline-none focus:border-zinc-900 dark:border-zinc-700 dark:focus:border-zinc-100"
        />
        <button
          type="submit"
          disabled={loading || !brand.trim()}
          className="h-11 rounded-lg bg-foreground px-6 font-medium text-background transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Generating..." : "OK"}
        </button>
      </form>

      <section
        aria-live="polite"
        aria-busy={loading}
        className="min-h-32 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <div className="mb-2 flex items-center justify-between gap-3">
          <h2 className="text-sm font-medium text-zinc-500">
            {response?.plan ? `Plan for ${response.brand ?? brand}` : "Response"}
          </h2>
          {response?.plan && !loading && (
            <button
              type="button"
              onClick={handleCopy}
              className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-600 transition-colors hover:border-zinc-900 hover:text-foreground dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-100"
            >
              {copied ? "Copied" : "Copy plan"}
            </button>
          )}
        </div>

        {loading ? (
          <div className="flex flex-col gap-1">
            <p className="text-zinc-500">Researching the brand and drafting the plan…</p>
            <p className="text-sm text-zinc-400 dark:text-zinc-600">
              This usually takes 10–30 seconds.
            </p>
          </div>
        ) : response?.error ? (
          <p className="text-red-600 dark:text-red-400">{response.error}</p>
        ) : response?.plan ? (
          <div className="flex flex-col gap-6">
            <article className="break-words">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {response.plan}
              </ReactMarkdown>
            </article>

            {references.length > 0 && (
              <p className="text-xs text-zinc-500">
                Inspired by past work:{" "}
                {references
                  .map((ref) => `${ref.pitch_id} ${ref.client} – ${ref.campaign}`)
                  .join(" · ")}
              </p>
            )}

            {research.length > 0 && (
              <details className="group rounded-lg border border-zinc-200 dark:border-zinc-800">
                <summary className="cursor-pointer list-none px-4 py-3 text-sm font-medium text-zinc-500 select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
                  <span
                    aria-hidden="true"
                    className="mr-2 inline-block transition-transform group-open:rotate-90"
                  >
                    ›
                  </span>
                  Research sources
                </summary>
                <div className="flex flex-col gap-4 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
                  {research.map((section) => (
                    <div key={section.title} className="flex flex-col gap-1">
                      <h3 className="text-sm font-semibold">{section.title}</h3>
                      <ul className="flex flex-col gap-1 text-sm">
                        {section.sources.map((source) => (
                          <li key={source.url}>
                            <a
                              href={source.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-zinc-500 underline underline-offset-2 hover:text-foreground"
                            >
                              {source.title || source.url}
                            </a>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        ) : (
          <p className="text-zinc-500">Enter a brand name and press OK to generate a plan.</p>
        )}
      </section>
    </main>
  );
}
