import type { BrandDossier, DossierSource } from "@/lib/types";

const MAX_FACTS = 6;

const KIND_LABELS: Record<DossierSource["kind"], string> = {
  site: "Site",
  blog: "Blog",
  news: "News",
  campaign: "Campaign",
  social: "Social",
  other: "Web",
};

const VIA_LABELS: Record<DossierSource["via"], string> = {
  firecrawl: "Firecrawl",
  fallback: "Scraped",
  tavily: "Tavily",
};

function safeHref(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null;
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function Badge({ children, accent }: { children: React.ReactNode; accent?: boolean }) {
  return (
    <span
      className={`shrink-0 border-2 border-foreground px-1 font-mono text-[10px] leading-tight font-bold uppercase ${
        accent ? "bg-accent text-black" : "bg-background"
      }`}
    >
      {children}
    </span>
  );
}

const linkClass = "underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black";

/** What the deep research read and the strongest sourced facts it found. */
export function DossierPanel({ dossier }: { dossier: BrandDossier }) {
  const facts = (dossier.facts ?? []).filter((fact) => fact.text).slice(0, MAX_FACTS);
  const sources = (dossier.sources ?? []).filter((source) => safeHref(source.url));
  if (facts.length === 0 && sources.length === 0) return null;
  const pagesRead = dossier.pages_read ?? sources.length;

  return (
    <section aria-label="Deep research" className="flex min-w-0 flex-col gap-3 border-3 border-foreground p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono text-sm font-bold uppercase">Deep research</h3>
        <span className="font-mono text-xs text-muted">
          {pagesRead} {pagesRead === 1 ? "page" : "pages"} read
          {dossier.depth === "thin" ? " · thin coverage" : ""}
        </span>
      </div>

      {facts.length > 0 && (
        <div className="flex flex-col gap-2">
          <h4 className="text-sm font-bold uppercase">Key facts</h4>
          <ul className="flex flex-col gap-2 text-sm">
            {facts.map((fact) => {
              const href = safeHref(fact.source_url);
              return (
                <li key={fact.id} className="flex min-w-0 items-start gap-2">
                  {fact.number && <Badge accent>{fact.number}</Badge>}
                  <p className="min-w-0 break-words">
                    {fact.text}{" "}
                    {href && (
                      <a href={href} target="_blank" rel="noopener noreferrer" className={`font-mono text-xs ${linkClass}`}>
                        {fact.source_title || hostname(href)}
                      </a>
                    )}
                  </p>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {sources.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer list-none text-sm font-bold uppercase select-none hover:bg-accent hover:text-black [&::-webkit-details-marker]:hidden">
            <span aria-hidden="true" className="mr-2 inline-block transition-transform group-open:rotate-90">
              ▶
            </span>
            Pages read ({sources.length})
          </summary>
          <ul className="mt-2 flex flex-col gap-1.5 text-sm">
            {sources.map((source) => (
              <li key={source.url} className="flex min-w-0 items-start gap-2">
                <Badge>{KIND_LABELS[source.kind] ?? source.kind}</Badge>
                <a href={source.url} target="_blank" rel="noopener noreferrer" className={`min-w-0 break-words ${linkClass}`}>
                  {source.title || hostname(source.url)}
                </a>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-muted uppercase">
                  {VIA_LABELS[source.via] ?? source.via}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
