/* ═══════════ Types ═══════════ */

export interface Message {
  role: "user" | "assistant" | "tool";
  content: string;
  type?: "file" | "text" | "image" | "audio" | "translation" | "tool_call";
  filename?: string;
  imagePreview?: string;
  imageBase64?: string;
  imageMime?: string;
  audioUrl?: string;
  originalText?: string;
  translatedText?: string;
  callId?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolStatus?: "running" | "done" | "error" | "confirming";
}

export interface PendingFile {
  name: string;
  preview: string;
  type: "image" | "file" | "pdf";
  content: string;
  base64?: string;
  mimeType?: string;
}

export interface SessionInfo {
  id: string;
  name: string;
  messages: Message[];
  selectedModel: string;
}

export interface IdentityFile {
  name: string;
  exists: boolean;
  content: string;
}

export type ViewId = "chat" | "models" | "tools" | "skills" | "cron" | "channels" | "agents" | "settings" | "logs";

export interface CloudModel {
  name: string;
  key: string;
  endpoint: string;
  protocol?: string;
  max_tokens?: number;
}

export interface HFModelResult {
  id: string;
  author: string;
  downloads: number;
  likes: number;
  tags: string[];
  pipeline_tag: string;
  last_modified: string;
}

export interface DownloadState {
  status: string;
  progress: number;
  path: string;
  message: string;
  model_id?: string;
  speed_bps?: number;
  eta_seconds?: number;
  downloaded_bytes?: number;
}

export interface SetupIssue {
  item: string;
  status: string;
  fix: string;
  fix_type?: string;
  fix_pkg?: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  permission: string;
  usage_count: number;
}

export interface LLMStatus {
  backend: string;
  available_backends?: string[];
  status: string;
  model_id: string;
  model_name: string;
  port: number;
  message: string;
  has_image_support: boolean;
  token_limit: number;
  platform?: string;
  gpu_layers?: number;
}
