import { useRef, useState } from "react";
import { api, readSSE } from "../api";
import type { LoadedModel, ModelInfo } from "../types";
import { untilLabel } from "./VramPanel";

interface Props {
  models: ModelInfo[];
  loaded: LoadedModel[];
  streaming: boolean;
  onUnload: (name?: string) => void;
  onClose: () => void;
  onChanged: () => void;
}

interface PullState {
  name: string;
  status: string;
  completed?: number;
  total?: number;
  error?: string;
}

function gb(bytes: number) {
  return (bytes / 1e9).toFixed(1) + " GB";
}

export default function ModelManager({ models, loaded, streaming, onUnload, onClose, onChanged }: Props) {
  const resident = new Map(loaded.map((l) => [l.name, l]));
  const [name, setName] = useState("");
  const [pull, setPull] = useState<PullState | null>(null);
  const abort = useRef<AbortController | null>(null);

  const startPull = async () => {
    const n = name.trim();
    if (!n || pull) return;
    const ctrl = new AbortController();
    abort.current = ctrl;
    setPull({ name: n, status: "starting" });
    try {
      const res = await api.pullModel(n, ctrl.signal);
      for await (const ev of readSSE(res)) {
        if (ev.event === "progress") {
          setPull({ name: n, status: ev.data.status, completed: ev.data.completed, total: ev.data.total });
        } else if (ev.event === "error") {
          setPull({ name: n, status: "error", error: ev.data.message });
          return;
        } else if (ev.event === "done") {
          setPull(null);
          setName("");
          onChanged();
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError") setPull({ name: n, status: "error", error: String(e?.message ?? e) });
      else setPull(null);
    } finally {
      abort.current = null;
    }
  };

  const pct = pull?.total ? Math.round(((pull.completed ?? 0) / pull.total) * 100) : null;

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Models">
        <header className="modal-head">
          <h2>Models</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="pull-form">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && startPull()}
            placeholder="qwen3:14b  ·  gemma3:12b  ·  hf.co/user/repo:Q4_K_M"
            disabled={!!pull && pull.status !== "error"}
          />
          {pull && pull.status !== "error" ? (
            <button className="btn stop" onClick={() => abort.current?.abort()}>Cancel</button>
          ) : (
            <button className="btn primary" onClick={startPull} disabled={!name.trim()}>Pull</button>
          )}
        </div>
        {pull && (
          <div className="pull-progress">
            <div className="row">
              <span>{pull.name} — {pull.error ? "failed" : pull.status}</span>
              {pct !== null && <span>{pct}%</span>}
            </div>
            {pct !== null && <div className="bar"><div style={{ width: `${pct}%` }} /></div>}
            {pull.error && <div className="error-box">{pull.error}</div>}
          </div>
        )}

        <ul className="model-list">
          {models.length === 0 && <li className="muted">No models installed.</li>}
          {models.map((m) => (
            <li key={m.name} className="model-card">
              <div className="model-row">
                <strong>{m.name}</strong>
                <span className="actions">
                  {resident.has(m.name) && (
                    <button
                      className="link-btn"
                      onClick={() => onUnload(m.name)}
                      disabled={streaming}
                      title={streaming ? "Wait for the answer to finish" : "Free its GPU memory now"}
                    >
                      Unload
                    </button>
                  )}
                  <button
                    className="link-btn"
                    onClick={() => api.describeModel(m.name).then(onChanged)}
                    disabled={m.status === "describing" || m.status === "pending"}
                  >
                    Re-describe
                  </button>
                  <button
                    className="link-btn danger"
                    onClick={() => {
                      if (window.confirm(`Delete ${m.name} from Ollama?`)) api.deleteModel(m.name).then(onChanged);
                    }}
                  >
                    Delete
                  </button>
                </span>
              </div>
              <div className="chips">
                {resident.has(m.name) && (
                  <span className="chip loaded" title={untilLabel(resident.get(m.name)!.expires_at)}>
                    in VRAM · {gb(resident.get(m.name)!.size_vram)}
                  </span>
                )}
                {m.parameter_size && <span className="chip">{m.parameter_size}</span>}
                {m.quantization && <span className="chip">{m.quantization}</span>}
                <span className="chip">{gb(m.size)}</span>
                {m.context_length && <span className="chip">ctx {m.context_length.toLocaleString()}</span>}
                {m.capabilities.map((c) => (
                  <span key={c} className="chip accent">{c}</span>
                ))}
              </div>
              <p className="desc">
                {m.status === "ready" && m.description}
                {(m.status === "pending" || m.status === "describing") && (
                  <span className="muted">Generating description…</span>
                )}
                {m.status === "error" && <span className="danger-text">Description failed: {m.error}</span>}
              </p>
              {m.status === "ready" && m.description_source.startsWith("http") && (
                <a className="source-link" href={m.description_source} target="_blank" rel="noopener noreferrer">
                  source
                </a>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
