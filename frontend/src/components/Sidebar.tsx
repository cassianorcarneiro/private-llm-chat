import type { ConversationSummary } from "../types";

interface Props {
  conversations: ConversationSummary[];
  activeId: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onOpenModels: () => void;
  onClose: () => void;
}

function groupLabel(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const days = Math.floor((now.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

export default function Sidebar(p: Props) {
  const groups: [string, ConversationSummary[]][] = [];
  for (const c of p.conversations) {
    const label = groupLabel(c.updated_at);
    const last = groups[groups.length - 1];
    if (last && last[0] === label) last[1].push(c);
    else groups.push([label, [c]]);
  }

  return (
    <>
      <div className={`scrim ${p.open ? "show" : ""}`} onClick={p.onClose} />
      <aside className={`sidebar ${p.open ? "open" : ""}`}>
        <div className="sidebar-head">
          <div className="brand">
            <span className="brand-mark" />
            Local AI Chat
          </div>
          <button className="btn primary block" onClick={p.onNew}>
            + New chat
          </button>
        </div>

        <nav className="conv-list">
          {groups.length === 0 && <p className="muted small pad">No conversations yet.</p>}
          {groups.map(([label, items]) => (
            <section key={label}>
              <h3 className="group-label">{label}</h3>
              {items.map((c) => (
                <div
                  key={c.id}
                  className={`conv-item ${c.id === p.activeId ? "active" : ""}`}
                  onClick={() => p.onSelect(c.id)}
                  title={c.title}
                >
                  <span className="conv-title">{c.title}</span>
                  <span className="conv-actions">
                    <button
                      className="icon-btn"
                      aria-label="Rename"
                      onClick={(e) => {
                        e.stopPropagation();
                        const t = window.prompt("Rename conversation", c.title);
                        if (t && t.trim()) p.onRename(c.id, t.trim());
                      }}
                    >
                      ✎
                    </button>
                    <button
                      className="icon-btn danger"
                      aria-label="Delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm(`Delete "${c.title}"?`)) p.onDelete(c.id);
                      }}
                    >
                      ✕
                    </button>
                  </span>
                </div>
              ))}
            </section>
          ))}
        </nav>

        <div className="sidebar-foot">
          <button className="btn ghost block" onClick={p.onOpenModels}>
            Manage models
          </button>
        </div>
      </aside>
    </>
  );
}
