import { Document, Font, Image, Link, Page, pdf, StyleSheet, Text, View } from "@react-pdf/renderer";
import type { Image as MdImage, ListItem, Paragraph, PhrasingContent, Root, RootContent, Table } from "mdast";
import type { ReactNode } from "react";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

import { absoluteMediaUrl } from "@/lib/media";
import type { BrandPlan } from "@/lib/types";

// Client-only: imported lazily (see lib/report-files.ts) so react-pdf stays out of page bundles.

const ACCENT = "#ffe600";
const INK = "#000000";
const MUTED = "#4a4a4a";

let fontsRegistered = false;

function registerFonts() {
  if (fontsRegistered) return;
  const font = (file: string) => `${window.location.origin}/fonts/${file}`;
  Font.register({
    family: "Noto Sans",
    fonts: [
      { src: font("NotoSans-Regular.ttf") },
      { src: font("NotoSans-Bold.ttf"), fontWeight: 700 },
      { src: font("NotoSans-Italic.ttf"), fontStyle: "italic" },
      { src: font("NotoSans-BoldItalic.ttf"), fontWeight: 700, fontStyle: "italic" },
    ],
  });
  Font.register({
    family: "Space Mono",
    fonts: [{ src: font("SpaceMono-Regular.ttf") }, { src: font("SpaceMono-Bold.ttf"), fontWeight: 700 }],
  });
  // Don't hyphenate words across lines.
  Font.registerHyphenationCallback((word) => [word]);
  fontsRegistered = true;
}

/** Map characters the bundled fonts can't draw (arrows, emoji) to ones they can. */
function clean(text: string): string {
  return text
    .replace(/→|⟶|➔|➜/g, "->")
    .replace(/←|⟵/g, "<-")
    .replace(/[✓✔✅]/g, "*")
    .replace(/[■▪◼]/g, "•")
    .replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, "");
}

const s = StyleSheet.create({
  page: {
    paddingTop: 48,
    paddingBottom: 64,
    paddingHorizontal: 48,
    fontFamily: "Noto Sans",
    fontSize: 10.5,
    lineHeight: 1.5,
    color: INK,
  },
  header: { marginBottom: 20, paddingBottom: 14, borderBottomWidth: 3, borderBottomColor: INK },
  tag: {
    alignSelf: "flex-start",
    marginBottom: 10,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 2,
    borderColor: INK,
    backgroundColor: ACCENT,
    fontFamily: "Space Mono",
    fontWeight: 700,
    fontSize: 9,
  },
  brand: { fontSize: 26, fontWeight: 700, lineHeight: 1.2, textTransform: "uppercase" },
  meta: { marginTop: 4, fontFamily: "Space Mono", fontSize: 8.5, color: MUTED },
  h1: { marginTop: 6, marginBottom: 10, fontSize: 18, fontWeight: 700, lineHeight: 1.25 },
  h2: {
    alignSelf: "flex-start",
    marginTop: 16,
    marginBottom: 8,
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderWidth: 2,
    borderColor: INK,
    backgroundColor: ACCENT,
    fontSize: 12.5,
    fontWeight: 700,
    textTransform: "uppercase",
  },
  h3: { marginTop: 12, marginBottom: 4, fontSize: 11.5, fontWeight: 700 },
  h4: { marginTop: 8, marginBottom: 4, fontWeight: 700 },
  p: { marginBottom: 6 },
  bold: { fontWeight: 700 },
  italic: { fontStyle: "italic" },
  strike: { textDecoration: "line-through" },
  link: { color: INK, textDecoration: "underline" },
  code: { fontFamily: "Space Mono", fontSize: 9 },
  list: { marginBottom: 6 },
  listItem: { flexDirection: "row", marginBottom: 2 },
  bullet: { width: 16 },
  listBody: { flex: 1 },
  quote: { marginVertical: 6, paddingLeft: 10, borderLeftWidth: 4, borderLeftColor: INK, fontStyle: "italic" },
  pre: { marginVertical: 6, padding: 8, borderWidth: 2, borderColor: INK, fontFamily: "Space Mono", fontSize: 8.5 },
  hr: { marginVertical: 12, borderBottomWidth: 2, borderBottomColor: INK },
  figure: { marginVertical: 6, flexDirection: "row", flexWrap: "wrap", gap: 8 },
  image: { width: 230, maxHeight: 230, objectFit: "contain", objectPosition: "left" },
  table: { marginVertical: 8, borderTopWidth: 2, borderLeftWidth: 2, borderColor: INK },
  row: { flexDirection: "row" },
  cell: { flex: 1, padding: 4, borderRightWidth: 2, borderBottomWidth: 2, borderColor: INK, fontSize: 9 },
  headCell: { backgroundColor: ACCENT, fontWeight: 700 },
  sectionTitle: {
    marginTop: 22,
    marginBottom: 8,
    paddingBottom: 4,
    borderBottomWidth: 2,
    borderBottomColor: INK,
    fontFamily: "Space Mono",
    fontWeight: 700,
    fontSize: 10,
    textTransform: "uppercase",
  },
  sourceGroup: { marginBottom: 8 },
  sourceHeading: { fontWeight: 700, fontSize: 9.5, textTransform: "uppercase" },
  source: { fontSize: 9, marginBottom: 2 },
  sourceUrl: { color: MUTED, fontSize: 8 },
  footer: {
    position: "absolute",
    bottom: 28,
    left: 48,
    right: 48,
    height: 22,
    paddingTop: 6,
    borderTopWidth: 1,
    borderTopColor: INK,
    fontFamily: "Space Mono",
    fontSize: 8,
    lineHeight: 1.2,
    color: MUTED,
  },
});

function inline(nodes: PhrasingContent[]): ReactNode[] {
  return nodes.map((node, i) => {
    switch (node.type) {
      case "text":
        return clean(node.value);
      case "strong":
        return <Text key={i} style={s.bold}>{inline(node.children)}</Text>;
      case "emphasis":
        return <Text key={i} style={s.italic}>{inline(node.children)}</Text>;
      case "delete":
        return <Text key={i} style={s.strike}>{inline(node.children)}</Text>;
      case "inlineCode":
        return <Text key={i} style={s.code}>{clean(node.value)}</Text>;
      case "link":
        return (
          <Link key={i} src={node.url} style={s.link}>
            {inline(node.children)}
          </Link>
        );
      case "break":
        return "\n";
      case "image":
        return node.alt ? clean(node.alt) : null;
      default:
        return null;
    }
  });
}

const HEADING_STYLES: Record<number, (typeof s)[keyof typeof s]> = { 1: s.h1, 2: s.h2, 3: s.h3 };

/** Markdown image URL -> data: URL, for the images that could be fetched (JPEG/PNG only). */
type ImageMap = Map<string, string>;

function listItem(item: ListItem, marker: string, key: number, images: ImageMap) {
  return (
    <View key={key} style={s.listItem} wrap={false}>
      <Text style={s.bullet}>{marker}</Text>
      <View style={s.listBody}>{blocks(item.children, images)}</View>
    </View>
  );
}

/** Split a paragraph into its images and the rest (deck visuals come with a "Ref:" credit line). */
function splitImages(node: Paragraph): { imgs: MdImage[]; rest: PhrasingContent[] } {
  const imgs: MdImage[] = [];
  const rest: PhrasingContent[] = [];
  for (const child of node.children) {
    if (child.type === "image") imgs.push(child);
    else rest.push(child);
  }
  return { imgs, rest };
}

function table(node: Table, key: number) {
  return (
    <View key={key} style={s.table}>
      {node.children.map((row, r) => (
        <View key={r} style={s.row} wrap={false}>
          {row.children.map((cell, c) => (
            <Text key={c} style={r === 0 ? [s.cell, s.headCell] : s.cell}>
              {inline(cell.children)}
            </Text>
          ))}
        </View>
      ))}
    </View>
  );
}

function blocks(nodes: RootContent[], images: ImageMap): ReactNode[] {
  return nodes.map((node, i) => {
    switch (node.type) {
      case "heading":
        return (
          <Text key={i} style={HEADING_STYLES[node.depth] ?? s.h4} minPresenceAhead={40}>
            {inline(node.children)}
          </Text>
        );
      case "paragraph": {
        const { imgs, rest } = splitImages(node);
        const loaded = imgs.filter((img) => images.has(img.url));
        if (loaded.length === 0) return <Text key={i} style={s.p}>{inline(node.children)}</Text>;
        // Drop the line break / whitespace left where the image was.
        const start = rest.findIndex((c) => !(c.type === "break" || (c.type === "text" && !c.value.trim())));
        const hasText = start !== -1;
        return (
          <View key={i} wrap={false}>
            <View style={s.figure}>
              {loaded.map((img, n) => (
                // eslint-disable-next-line jsx-a11y/alt-text -- react-pdf Image has no alt
                <Image key={n} src={images.get(img.url)} style={s.image} />
              ))}
            </View>
            {hasText && <Text style={s.p}>{inline(rest.slice(start))}</Text>}
          </View>
        );
      }
      case "list":
        return (
          <View key={i} style={s.list}>
            {node.children.map((item, n) =>
              listItem(item, node.ordered ? `${(node.start ?? 1) + n}.` : "•", n, images),
            )}
          </View>
        );
      case "blockquote":
        return <View key={i} style={s.quote}>{blocks(node.children, images)}</View>;
      case "code":
        return <Text key={i} style={s.pre}>{clean(node.value)}</Text>;
      case "thematicBreak":
        return <View key={i} style={s.hr} />;
      case "table":
        return table(node, i);
      default:
        return null;
    }
  });
}

export type ReportInput = BrandPlan & { createdAt?: string };

type DocumentProps = { report: ReportInput & { createdAt: string }; tree: Root; images: ImageMap };

function ReportDocument({ report, tree, images }: DocumentProps) {
  const date = new Intl.DateTimeFormat("en-GB", { dateStyle: "long" }).format(new Date(report.createdAt));
  const sections = report.research.filter((section) => section.sources.length > 0);

  return (
    <Document title={`${report.brand} – Pitch plan`} author="PitchCraft" creator="PitchCraft" producer="PitchCraft">
      <Page size="A4" style={s.page}>
        <View style={s.header}>
          <Text style={s.tag}>PITCHCRAFT · PITCH PLAN</Text>
          <Text style={s.brand}>{clean(report.brand)}</Text>
          <Text style={s.meta}>Generated {date}</Text>
        </View>

        {blocks(tree.children, images)}

        {report.references.length > 0 && (
          <View wrap={false}>
            <Text style={s.sectionTitle}>Inspired by past work</Text>
            {report.references.map((ref) => (
              <Text key={ref.pitch_id} style={s.source}>
                {clean(`${ref.pitch_id} · ${ref.client} – ${ref.campaign}`)}
              </Text>
            ))}
          </View>
        )}

        {sections.length > 0 && (
          <View>
            <Text style={s.sectionTitle} minPresenceAhead={40}>Research sources</Text>
            {sections.map((section) => (
              <View key={section.title} style={s.sourceGroup}>
                <Text style={s.sourceHeading} minPresenceAhead={20}>{clean(section.title)}</Text>
                {section.sources
                  .filter((source, i, all) => all.findIndex((other) => other.url === source.url) === i)
                  .map((source) => (
                  <View key={source.url} style={s.source} wrap={false}>
                    <Link src={source.url} style={s.link}>
                      {clean(source.title || source.url)}
                    </Link>
                    <Text style={s.sourceUrl}>{source.url}</Text>
                  </View>
                ))}
              </View>
            ))}
          </View>
        )}

        <Text style={s.footer} fixed>
          PitchCraft · {clean(report.brand)} · {date}
        </Text>
      </Page>
    </Document>
  );
}

const MAX_PDF_IMAGES = 40;

function collectImageUrls(node: Root | RootContent, urls: Set<string>) {
  if (node.type === "image") urls.add(node.url);
  if ("children" in node) for (const child of node.children) collectImageUrls(child, urls);
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

/**
 * Fetch the plan's images up front (through the media proxy) so one missing or
 * unsupported image is skipped instead of breaking the PDF. react-pdf only draws JPEG and PNG.
 */
async function loadImages(tree: Root): Promise<ImageMap> {
  const urls = new Set<string>();
  collectImageUrls(tree, urls);
  const images: ImageMap = new Map();
  await Promise.all(
    [...urls].slice(0, MAX_PDF_IMAGES).map(async (url) => {
      const absolute = absoluteMediaUrl(url);
      if (!absolute) return;
      try {
        const res = await fetch(absolute, { signal: AbortSignal.timeout(10_000) });
        const blob = res.ok ? await res.blob() : null;
        if (blob && (blob.type === "image/jpeg" || blob.type === "image/png")) {
          images.set(url, await blobToDataUrl(blob));
        }
      } catch {
        // Unreachable (or cross-origin) image: the PDF just shows it as alt text.
      }
    }),
  );
  return images;
}

export async function renderReportPdf(report: ReportInput): Promise<Blob> {
  registerFonts();
  const createdAt = report.createdAt ?? new Date().toISOString();
  const tree = unified().use(remarkParse).use(remarkGfm).parse(report.plan) as Root;
  const images = await loadImages(tree);
  return pdf(<ReportDocument report={{ ...report, createdAt }} tree={tree} images={images} />).toBlob();
}
