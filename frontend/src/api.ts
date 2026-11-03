import type { ConversationSummary, ChatMessage, LoadedModel, ModelInfo, SSEEvent } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const enc = (name: string) => name.split("/").map(encodeURIComponent).join("/");

export const api = {
  listConversations: () => fetch("/api/conversations").then(json<ConversationSummary[]>),
  getConversation: (id: string) =>
    fetch(`/api/conversations/${id}`).then(
      json<ConversationSummary & { messages: ChatMessage[] }>,
    ),
  renameConversation: (id: string, title: string) =>
    fetch(`/api/conversations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then(json<ConversationSummary>),
  deleteConversation: (id: string) =>
    fetch(`/api/conversations/${id}`, { method: "DELETE" }).then(json<void>),

  listModels: () => fetch("/api/models").then(json<ModelInfo[]>),
  loadedModels: () => fetch("/api/models/loaded").then(json<LoadedModel[]>),
  unloadModels: (name?: string) =>
    fetch("/api/models/unload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(name ? { name } : {}),
    }).then(json<{ unloaded: string[]; loaded: LoadedModel[] }>),
  deleteModel: (name: string) => fetch(`/api/models/${enc(name)}`, { method: "DELETE" }).then(json<void>),
  describeModel: (name: string) =>
    fetch(`/api/models/${enc(name)}/describe`, { method: "POST" }).then(json<unknown>),
  pullModel: (name: string, signal?: AbortSignal) =>
    fetch("/api/models/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
      signal,
    }),

  chat: (
    body: { conversation_id: string | null; model: string; content: string; web_search: boolean },
    signal: AbortSignal,
  ) =>
    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
};

/** Parse a text/event-stream body. EventSource cannot POST, so we read fetch's stream. */
export async function* readSSE(res: Response): AsyncGenerator<SSEEvent> {
  if (!res.ok || !res.body) {
    await json(res);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}
