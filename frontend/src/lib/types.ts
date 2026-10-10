export type Source = {
  title: string;
  url: string;
  snippet: string;
};

export type ResearchSection = {
  title: string;
  summary: string;
  sources: Source[];
};

export type PitchReference = {
  pitch_id: string;
  campaign: string;
  client: string;
};

// --- Deck (mirrors backend/app/schemas/deck.py, WORKPLAN C4) ---------------

/** Requested plan size; "auto" lets the strategist pick from the brand and brief. */
export type PlanScale = "auto" | "focused" | "standard" | "flagship";
export type ResolvedScale = Exclude<PlanScale, "auto">;

export const PLAN_SCALES: readonly PlanScale[] = ["auto", "focused", "standard", "flagship"];

export type ThemeName = "bold_dark" | "cinematic" | "pop_meme" | "editorial" | "sport_energy";

export type Visual = {
  want: "ref" | "meme" | "mock";
  brief: string;
  query?: string | null;
  meme_template_id?: string | null;
  meme_boxes?: string[];
  kind: "ref" | "meme" | "mock" | "placeholder";
  /** Backend media path, e.g. "/media/<sha1>.jpg". Render it through `mediaUrl()`. */
  url?: string | null;
  source_url?: string | null;
  credit?: string | null;
};

export type Theme = {
  name: ThemeName;
  bg: string;
  surface: string;
  accent: string;
  accent_2: string;
  text: string;
  muted: string;
  heading_font: string;
  body_font: string;
  hero_image?: Visual | null;
};

type SlideBase = {
  title: string;
  notes?: string;
};

export type FormulaPart = { label: string; text: string };
export type Card = { title: string; body?: string; tags?: string[]; visual?: Visual | null };
export type Column = { heading: string; bullets?: string[] };
export type ChipGroup = { label: string; chips?: string[] };
export type Phase = { name: string; when?: string; points?: string[] };

export type CoverSlide = SlideBase & { layout: "cover"; subtitle?: string; tagline?: string };
export type StatementSlide = SlideBase & { layout: "statement"; strip?: string; body?: string };
export type FromToSlide = SlideBase & { layout: "from_to"; strip?: string; from_text: string; to_text: string };
export type FormulaSlide = SlideBase & { layout: "formula"; parts: FormulaPart[]; result: FormulaPart };
export type CardGridSlide = SlideBase & { layout: "card_grid"; strip?: string; cards: Card[] };
export type DividerSlide = SlideBase & { layout: "divider"; subtitle?: string };
export type IdeaSlide = SlideBase & {
  layout: "idea";
  concept_note: string;
  hook?: string;
  format?: string;
  creator?: string;
  scalability?: string;
  visual?: Visual | null;
};
export type SamplesSlide = SlideBase & { layout: "samples"; strip?: string; visuals: Visual[] };
export type RefSlide = SlideBase & { layout: "ref"; why?: string; visuals: Visual[] };
export type TwoColumnSlide = SlideBase & { layout: "two_column"; left: Column; right: Column };
export type TableSlide = SlideBase & { layout: "table"; strip?: string; headers: string[]; rows: string[][] };
export type KpiTableSlide = SlideBase & { layout: "kpi_table"; strip?: string; headers: string[]; rows: string[][] };
export type ChipsSlide = SlideBase & { layout: "chips"; strip?: string; groups: ChipGroup[] };
export type TimelineSlide = SlideBase & { layout: "timeline"; strip?: string; phases: Phase[] };
export type FinSlide = SlideBase & { layout: "fin"; subtitle?: string };

export type Slide =
  | CoverSlide
  | StatementSlide
  | FromToSlide
  | FormulaSlide
  | CardGridSlide
  | DividerSlide
  | IdeaSlide
  | SamplesSlide
  | RefSlide
  | TwoColumnSlide
  | TableSlide
  | KpiTableSlide
  | ChipsSlide
  | TimelineSlide
  | FinSlide;

export type SlideLayout = Slide["layout"];

export type Deck = {
  title: string;
  brand: string;
  scale: ResolvedScale;
  theme: Theme;
  slides: Slide[];
};

export type BrandPlan = {
  brand: string;
  plan: string;
  research: ResearchSection[];
  references: PitchReference[];
  /** v2 plans only; older saved researches don't have these. */
  deck?: Deck | null;
  scale?: ResolvedScale;
  brief?: string | null;
};
