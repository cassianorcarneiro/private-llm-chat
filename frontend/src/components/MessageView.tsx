import { memo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { ChatMessage } from "../types";

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

      {m.sources && m.sources.length > 0 && (
        <ol className="sources">
          {m.sources.map((s) => (
            <li key={s.n}>
              <span className="n">{s.n}</span>
              <a href={s.url} target="_blank" rel="noopener noreferrer" title={s.url}>
                {s.title}
              </a>
              <span className="host">{safeHost(s.url)}</span>
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

function safeHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

export default memo(MessageView);
