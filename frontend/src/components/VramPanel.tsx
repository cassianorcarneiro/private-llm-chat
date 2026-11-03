import type { LoadedModel } from "../types";

interface Props {
  loaded: LoadedModel[];
  busy: boolean;
  onUnload: (name?: string) => void;
}

export function gb(bytes: number): string {
  return (bytes / 1e9).toFixed(1) + " GB";
}

export function untilLabel(expires: string | null): string {
  if (!expires) return "";
  const ms = new Date(expires).getTime() - Date.now();
  if (!Number.isFinite(ms)) return "";
  if (ms > 86_400_000) return "kept loaded";
  if (ms <= 60_000) return "unloads in <1 min";
  return `unloads in ${Math.round(ms / 60_000)} min`;
}

export default function VramPanel({ loaded, busy, onUnload }: Props) {
  const total = loaded.reduce((s, m) => s + m.size_vram, 0);
  return (
    <div className="vram">
      <div className="vram-head">
        <span className="vram-title">
          <span className={`dot ${loaded.length ? "on" : ""}`} />
          {loaded.length ? `VRAM · ${gb(total)}` : "VRAM free"}
        </span>
        {loaded.length > 0 && (
          <button
            className="link-btn"
            onClick={() => onUnload()}
            disabled={busy}
            title={busy ? "Wait for the answer to finish" : "Unload all models from GPU memory"}
          >
            Unload all
          </button>
        )}
      </div>
      {loaded.map((m) => (
        <div key={m.name} className="vram-row">
          <span className="vram-name" title={m.name}>{m.name}</span>
          <span className="vram-meta" title={untilLabel(m.expires_at)}>{gb(m.size_vram)}</span>
          <button
            className="icon-btn"
            aria-label={`Unload ${m.name}`}
            title={busy ? "Wait for the answer to finish" : `Unload ${m.name} · ${untilLabel(m.expires_at)}`}
            onClick={() => onUnload(m.name)}
            disabled={busy}
          >
            ⏏
          </button>
        </div>
      ))}
    </div>
  );
}
