export interface ConversationSummary {
  id: string;
  title: string;
  model: string | null;
  updated_at: string;
}

export interface Source {
  n: number;
  url: string;
  title: string;
  date?: string | null;
  primary?: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  model?: string | null;
  sources?: Source[] | null;
  // Client-only fields while streaming.
  status?: string;
  streaming?: boolean;
  error?: string;
  stats?: { output_tokens?: number; eval_duration_ns?: number };
}

export interface ModelInfo {
  name: string;
  size: number;
  family: string;
  parameter_size: string;
  quantization: string;
  context_length: number | null;
  capabilities: string[];
  chat: boolean;
  description: string;
  description_source: string;
  status: "pending" | "describing" | "ready" | "error";
  error: string;
}

export interface LoadedModel {
  name: string;
  size_vram: number;
  expires_at: string | null;
}

export interface SSEEvent {
  event: string;
  data: any;
}
