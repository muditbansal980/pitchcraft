import type { ResearchSection } from "@/lib/types";

/** Research can return the same article twice; keep the first of each URL (also keeps React keys unique). */
function uniqueByUrl<T extends { url: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => !seen.has(item.url) && seen.add(item.url));
}

export function ResearchSources({ research }: { research: ResearchSection[] }) {
  const sections = research
    .map((section) => ({ ...section, sources: uniqueByUrl(section.sources) }))
    .filter((section) => section.sources.length > 0);
  if (sections.length === 0) return null;

  return (
    <details className="group border-3 border-foreground">
      <summary className="cursor-pointer list-none px-4 py-3 font-mono text-sm font-bold uppercase select-none hover:bg-accent hover:text-black [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="mr-2 inline-block transition-transform group-open:rotate-90">
          ▶
        </span>
        Research sources
      </summary>
      <div className="flex flex-col gap-4 border-t-3 border-foreground px-4 py-3">
        {sections.map((section, i) => (
          <div key={`${i}-${section.title}`} className="flex flex-col gap-1">
            <h3 className="text-sm font-bold uppercase">{section.title}</h3>
            <ul className="flex flex-col gap-1 text-sm">
              {section.sources.map((source) => (
                <li key={source.url}>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black"
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
  );
}
