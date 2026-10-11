import { mediaUrl } from "@/lib/media";
import type { Deck, Slide, Theme, Visual } from "@/lib/types";

const LAYOUT_LABELS: Record<Slide["layout"], string> = {
  cover: "Cover",
  statement: "Statement",
  from_to: "From → To",
  formula: "Formula",
  card_grid: "Cards",
  divider: "Divider",
  idea: "Idea",
  samples: "Samples",
  ref: "Ref",
  two_column: "Two column",
  table: "Table",
  kpi_table: "KPIs",
  chips: "Chips",
  timeline: "Timeline",
  fin: "Fin",
};

const HEX = /^#[0-9A-Fa-f]{6}$/;

function color(value: string | undefined, fallback: string) {
  return value && HEX.test(value) ? value : fallback;
}

function firstImage(visuals: (Visual | null | undefined)[]): string | null {
  for (const visual of visuals) {
    const url = visual?.kind !== "placeholder" ? mediaUrl(visual?.url) : null;
    if (url) return url;
  }
  return null;
}

function slideImage(slide: Slide, theme: Theme): string | null {
  switch (slide.layout) {
    case "idea":
      return firstImage([slide.visual]);
    case "samples":
    case "ref":
      return firstImage(slide.visuals);
    case "card_grid":
      return firstImage(slide.cards.map((card) => card.visual));
    case "cover":
    case "divider":
    case "fin":
      return firstImage([theme.hero_image]);
    default:
      return null;
  }
}

/** One short line under the title, so tiles read as the slide's content at a glance. */
function slideDetail(slide: Slide): string {
  switch (slide.layout) {
    case "cover":
      return slide.subtitle || slide.tagline || "";
    case "statement":
    case "card_grid":
    case "samples":
    case "table":
    case "kpi_table":
    case "chips":
    case "timeline":
      return slide.strip || "";
    case "from_to":
      return `${slide.from_text} → ${slide.to_text}`;
    case "formula":
      return `${slide.parts.map((part) => part.label).join(" + ")} = ${slide.result.label}`;
    case "divider":
    case "fin":
      return slide.subtitle || "";
    case "idea":
      return slide.hook || slide.format || "";
    case "ref":
      return slide.why || "";
    case "two_column":
      return `${slide.left.heading} | ${slide.right.heading}`;
  }
}

/** Horizontally scrollable 16:9 thumbnails of the deck, in the deck's own theme colours. */
export function SlideStrip({ deck }: { deck: Deck }) {
  if (deck.slides.length === 0) return null;
  const bg = color(deck.theme.bg, "#0E0E0E");
  const text = color(deck.theme.text, "#FFFFFF");
  const accent = color(deck.theme.accent, "#F5B700");
  const muted = color(deck.theme.muted, "#B3B3B3");

  return (
    <section aria-label="Slides" className="flex min-w-0 flex-col gap-2">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="font-mono text-sm font-bold uppercase">
          Deck · {deck.slides.length} slides
        </h3>
        <span className="font-mono text-xs text-muted capitalize">{deck.scale}</span>
      </div>
      <ol className="flex snap-x gap-3 overflow-x-auto overscroll-x-contain pb-3">
        {deck.slides.map((slide, i) => {
          const image = slideImage(slide, deck.theme);
          const detail = slideDetail(slide);
          return (
            <li
              key={i}
              className="relative aspect-video w-44 shrink-0 snap-start overflow-hidden border-3 border-foreground sm:w-52"
              style={{ backgroundColor: bg, color: text }}
              title={`${i + 1}. ${slide.title}`}
            >
              {image && (
                // eslint-disable-next-line @next/next/no-img-element -- proxied media, sized by CSS
                <img
                  src={image}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  className="absolute inset-y-0 right-0 h-full w-1/2 object-cover opacity-70"
                />
              )}
              <div className="relative flex h-full flex-col gap-1 p-2" style={image ? { maxWidth: "62%" } : undefined}>
                <span
                  className="self-start px-1 font-mono text-[9px] leading-tight font-bold uppercase"
                  style={{ backgroundColor: accent, color: bg }}
                >
                  {LAYOUT_LABELS[slide.layout] ?? slide.layout}
                </span>
                <p className="line-clamp-3 text-[11px] leading-tight font-bold uppercase">{slide.title}</p>
                {detail && (
                  <p className="line-clamp-2 text-[9px] leading-tight" style={{ color: muted }}>
                    {detail}
                  </p>
                )}
                <span className="mt-auto font-mono text-[9px]" style={{ color: muted }}>
                  {i + 1}
                </span>
              </div>
              <span aria-hidden="true" className="absolute inset-x-0 bottom-0 h-1" style={{ backgroundColor: accent }} />
            </li>
          );
        })}
      </ol>
    </section>
  );
}
