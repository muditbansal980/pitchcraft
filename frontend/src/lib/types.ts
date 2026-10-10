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

export type BrandPlan = {
  brand: string;
  plan: string;
  research: ResearchSection[];
  references: PitchReference[];
};
