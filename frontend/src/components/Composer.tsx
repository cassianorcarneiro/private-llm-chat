import { useEffect, useRef, useState } from "react";
import type { ModelInfo } from "../types";

interface Props {
  models: ModelInfo[];
  model: string;
  onModelChange: (name: string) => void;
  webSearch: boolean;
  onWebSearchChange: (on: boolean) => void;
  streaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

export default function Composer(p: Props) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const chatModels = p.models.filter((m) => m.chat);
  const selected = chatModels.find((m) => m.name === p.model);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 260) + "px";
  }, [text]);

  const submit = () => {
    const t = text.trim();
    if (!t || p.streaming || !p.model) return;
    p.onSend(t);
    setText("");
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={ref}
          rows={1}
          value={text}
          placeholder={p.model ? `Message ${p.model}` : "Install a model to start"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-bar">
          <select
            className="model-select"
            value={p.model}
            onChange={(e) => p.onModelChange(e.target.value)}
            disabled={p.streaming}
            aria-label="Model"
          >
            {chatModels.length === 0 && <option value="">No models installed</option>}
            {chatModels.map((m) => (
              <option key={m.name} value={m.name} title={m.description}>
                {m.name}
                {m.parameter_size ? ` · ${m.parameter_size}` : ""}
              </option>
            ))}
          </select>

          <label className={`toggle ${p.webSearch ? "on" : ""}`} title="Allow web search via SearXNG">
            <input
              type="checkbox"
              checked={p.webSearch}
              onChange={(e) => p.onWebSearchChange(e.target.checked)}
            />
            <span className="toggle-track"><span className="toggle-thumb" /></span>
            Web
          </label>

          <span className="spacer" />

          {p.streaming ? (
            <button className="btn stop" onClick={p.onStop}>
              Stop
            </button>
          ) : (
            <button className="btn primary" onClick={submit} disabled={!text.trim() || !p.model}>
              Send
            </button>
          )}
        </div>
      </div>
      <p className="model-desc">
        {selected
          ? selected.status === "ready"
            ? selected.description
            : selected.status === "error"
              ? "Description unavailable."
              : "Generating description…"
          : ""}
        {p.webSearch && (
          <span className="privacy-note"> Web on: search queries leave this server.</span>
        )}
      </p>
    </div>
  );
}
