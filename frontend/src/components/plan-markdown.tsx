import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { mediaUrl } from "@/lib/media";

// Markdown headings are shifted down two levels: the page already has an <h1>
// and the result region an <h2>, so the plan's "# Title" renders as an <h3>.
const components: Components = {
  h1: ({ children }) => (
    <h3 className="mt-2 mb-4 text-2xl font-bold tracking-tight uppercase">{children}</h3>
  ),
  h2: ({ children }) => (
    <h4 className="mt-8 mb-3 inline-block border-3 border-foreground bg-accent px-2 py-0.5 text-lg font-bold tracking-tight text-black uppercase">
      {children}
    </h4>
  ),
  h3: ({ children }) => <h5 className="mt-6 mb-2 text-lg font-bold">{children}</h5>,
  h4: ({ children }) => <h6 className="mt-4 mb-2 font-bold">{children}</h6>,
  h5: ({ children }) => <h6 className="mt-4 mb-2 font-bold">{children}</h6>,
  h6: ({ children }) => <h6 className="mt-4 mb-2 font-bold">{children}</h6>,
  p: ({ children }) => <p className="my-3 leading-7">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-3 list-[square] space-y-1.5 pl-6 leading-7">{children}</ul>
  ),
  ol: ({ children, start }) => (
    <ol start={start} className="my-3 list-decimal space-y-1.5 pl-6 leading-7">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-1 marker:text-foreground">{children}</li>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-bold underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-4 border-l-6 border-foreground bg-background py-1 pl-4 italic">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-8 border-t-3 border-foreground" />,
  code: ({ children, className }) => (
    <code
      className={`border-2 border-foreground bg-background px-1 font-mono text-[0.875em] ${className ?? ""}`}
    >
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="my-4 overflow-x-auto border-3 border-foreground bg-background p-4 text-sm [&>code]:border-0 [&>code]:p-0">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-4 overflow-x-auto">
      <table className="w-full border-collapse border-3 border-foreground text-sm">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      style={style}
      className="border-2 border-foreground bg-accent px-3 py-2 text-left font-bold text-black"
    >
      {children}
    </th>
  ),
  img: ({ src, alt }) => {
    const url = mediaUrl(typeof src === "string" ? src : null);
    if (!url) return alt ? <span className="italic">[{alt}]</span> : null;
    return (
      // eslint-disable-next-line @next/next/no-img-element -- proxied deck media of unknown size
      <img
        src={url}
        alt={alt ?? ""}
        loading="lazy"
        decoding="async"
        className="my-3 block h-auto max-h-[28rem] w-auto max-w-full border-3 border-foreground sm:max-w-md"
      />
    );
  },
  td: ({ children, style }) => (
    <td style={style} className="border-2 border-foreground px-3 py-2 align-top">
      {children}
    </td>
  ),
};

export function PlanMarkdown({ children }: { children: string }) {
  return (
    <article className="break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </article>
  );
}
