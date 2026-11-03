import { useCallback, useEffect, useRef, useState } from "react";
import { api, readSSE } from "./api";
import Sidebar from "./components/Sidebar";
import MessageView from "./components/MessageView";
import Composer from "./components/Composer";
import ModelManager from "./components/ModelManager";
import type { ChatMessage, ConversationSummary, ModelInfo } from "./types";

const MODEL_KEY = "local-ai-chat.model";

function loadModel(): string {
  try {
    return localStorage.getItem(MODEL_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function App() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState<string>(loadModel);
  // Off by default on every load: web search is the only path where data leaves the server.
  const [webSearch, setWebSearch] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [showModels, setShowModels] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const refreshConversations = useCallback(() => {
    api.listConversations().then(setConversations).catch((e) => setBanner(String(e.message ?? e)));
  }, []);

  const refreshModels = useCallback(() => {
    api
      .listModels()
      .then((ms) => {
        setModels(ms);
        setBanner(null);
      })
      .catch((e) => setBanner(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    refreshConversations();
    refreshModels();
  }, [refreshConversations, refreshModels]);

  // Poll while descriptions are being generated or the manager is open.
  const describing = models.some((m) => m.status === "pending" || m.status === "describing");
  useEffect(() => {
    if (!describing && !showModels) return;
    const t = setInterval(refreshModels, 4000);
    return () => clearInterval(t);
  }, [describing, showModels, refreshModels]);

  // Keep a valid chat model selected.
  useEffect(() => {
    const chat = models.filter((m) => m.chat);
    if (chat.length && !chat.some((m) => m.name === model)) setModel(chat[0].name);
  }, [models, model]);

  useEffect(() => {
    try {
      if (model) localStorage.setItem(MODEL_KEY, model);
    } catch {
      /* storage unavailable */
    }
  }, [model]);

  // Auto-scroll only if the user has not scrolled up.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const stop = () => abortRef.current?.abort();

  const selectConversation = async (id: string) => {
    stop();
    setSidebarOpen(false);
    try {
      const conv = await api.getConversation(id);
      setActiveId(id);
      setMessages(conv.messages);
      stickToBottom.current = true;
      if (conv.model && models.some((m) => m.name === conv.model && m.chat)) setModel(conv.model);
    } catch (e: any) {
      setBanner(String(e.message ?? e));
    }
  };

  const newConversation = () => {
    stop();
    setActiveId(null);
    setMessages([]);
    setSidebarOpen(false);
  };

  const updateLast = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => (ms.length ? [...ms.slice(0, -1), fn(ms[ms.length - 1])] : ms));

  const send = async (text: string) => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStreaming(true);
    stickToBottom.current = true;
    setMessages((ms) => [
      ...ms,
      { id: `tmp-u-${Date.now()}`, role: "user", content: text },
      { id: `tmp-a-${Date.now()}`, role: "assistant", content: "", thinking: "", model, streaming: true },
    ]);

    try {
      const res = await api.chat(
        { conversation_id: activeId, model, content: text, web_search: webSearch },
        ctrl.signal,
      );
      for await (const ev of readSSE(res)) {
        const d = ev.data;
        switch (ev.event) {
          case "meta":
            setActiveId(d.conversation_id);
            if (d.is_new) refreshConversations();
            break;
          case "status":
            updateLast((m) => ({ ...m, status: d.text }));
            break;
          case "sources":
            updateLast((m) => ({ ...m, sources: d.sources }));
            break;
          case "thinking":
            updateLast((m) => ({ ...m, thinking: (m.thinking ?? "") + d.text }));
            break;
          case "token":
            updateLast((m) => ({ ...m, content: m.content + d.text, status: "" }));
            break;
          case "stats":
            updateLast((m) => ({ ...m, stats: d }));
            break;
          case "error":
            updateLast((m) => ({ ...m, error: d.message }));
            break;
          case "done":
            updateLast((m) => ({ ...m, id: d.message_id }));
            refreshConversations();
            break;
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError") updateLast((m) => ({ ...m, error: String(e?.message ?? e) }));
    } finally {
      updateLast((m) => ({ ...m, streaming: false, status: "" }));
      setStreaming(false);
      abortRef.current = null;
      refreshConversations();
    }
  };

  const activeTitle = conversations.find((c) => c.id === activeId)?.title;

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        open={sidebarOpen}
        onSelect={selectConversation}
        onNew={newConversation}
        onRename={(id, title) => api.renameConversation(id, title).then(refreshConversations)}
        onDelete={(id) =>
          api.deleteConversation(id).then(() => {
            if (id === activeId) newConversation();
            refreshConversations();
          })
        }
        onOpenModels={() => {
          setShowModels(true);
          setSidebarOpen(false);
        }}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="main">
        <header className="topbar">
          <button className="icon-btn menu" onClick={() => setSidebarOpen(true)} aria-label="Menu">
            ☰
          </button>
          <span className="topbar-title">{activeTitle ?? "New chat"}</span>
        </header>

        {banner && (
          <div className="banner" onClick={() => setBanner(null)}>
            {banner}
          </div>
        )}

        <div className="scroll" ref={scrollRef} onScroll={onScroll}>
          {messages.length === 0 ? (
            <div className="empty">
              <span className="brand-mark big" />
              <h1>What are we working on?</h1>
              <p className="muted">Runs entirely on this server. Web search is off unless you turn it on.</p>
            </div>
          ) : (
            <div className="thread">
              {messages.map((m) => (
                <MessageView key={m.id} m={m} />
              ))}
            </div>
          )}
        </div>

        <Composer
          models={models}
          model={model}
          onModelChange={setModel}
          webSearch={webSearch}
          onWebSearchChange={setWebSearch}
          streaming={streaming}
          onSend={send}
          onStop={stop}
        />
      </main>

      {showModels && (
        <ModelManager models={models} onClose={() => setShowModels(false)} onChanged={refreshModels} />
      )}
    </div>
  );
}
