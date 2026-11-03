import { memo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { ChatMessage, Source } from "../types";

const mdComponents = {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};

function Markdownish({ text }: { text: string }) {
  return (
    <div className="md">
      <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={mdComponents}>
        {text}
      </Markdown>
    </div>
  );
}

function MessageView({ m }: { m: ChatMessage }) {
  const [copied, setCopied] = useState(false);

  if (m.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{m.content}</div>
      </div>
    );
  }

  const sources = m.streaming ? (m.sources ?? []) : citedSources(m);

  const tps =
    m.stats?.output_tokens && m.stats?.eval_duration_ns
      ? (m.stats.output_tokens / (m.stats.eval_duration_ns / 1e9)).toFixed(1)
      : null;

  return (
    <div className="msg assistant">
      {m.thinking && (
        <details className="thinking">
          <summary>Reasoning</summary>
          <pre>{m.thinking}</pre>
        </details>
      )}

      {m.content ? (
        <Markdownish text={m.content} />
      ) : (
        m.streaming && <div className="typing"><span /><span /><span /></div>
      )}

      {m.status && <div className="status-line">{m.status}</div>}
      {m.error && <div className="error-box">{m.error}</div>}

      {sources.length > 0 && (
        <ol className="sources">
          {sources.map((s) => (
            <li key={s.n}>
              <span className="n">{s.n}</span>
              <a href={s.url} target="_blank" rel="noopener noreferrer" title={s.url}>
                {s.title}
              </a>
              <span className="host">{safeHost(s.url)}</span>
              {s.date && <span className="host">{s.date}</span>}
              {s.primary && <span className="primary-tag">primary</span>}
            </li>
          ))}
        </ol>
      )}

      {!m.streaming && m.content && (
        <div className="msg-meta">
          {m.model && <span>{m.model}</span>}
          {tps && <span>{tps} tok/s</span>}
          <button
            className="link-btn"
            onClick={() => {
              navigator.clipboard?.writeText(m.content).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              });
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Sources the answer actually cites. Matches [1], [1, 3] and [1][2].
 * Falls back to all sources if the model cited none, so evidence is never hidden.
 */
function citedSources(m: ChatMessage): Source[] {
  const all = m.sources ?? [];
  if (!all.length) return all;
  const cited = new Set<number>();
  for (const match of m.content.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    for (const n of match[1].split(",")) cited.add(Number(n.trim()));
  }
  const used = all.filter((s) => cited.has(s.n));
  return used.length ? used : all;
}

function safeHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

export default memo(MessageView);
