/* ═══════════ Types ═══════════ */

export interface Message {
  id?: string;
  role: "user" | "assistant" | "tool";
  content: string;
  ts?: number;
  thinking?: string;
  type?: "file" | "image" | "tool_call";
  filename?: string;
  imagePreview?: string;
  imageBase64?: string;
  imageMime?: string;
  callId?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolStatus?: "running" | "done" | "error" | "confirming";
  duration?: number;        // 工具耗时（毫秒）
  thinkingDuration?: number; // 思考耗时（毫秒）
  round?: number;            // agent 轮次（round_start 事件，显示"第 N 轮"）
}

export interface PendingFile {
  name: string;
  preview: string;
  type: "image" | "file" | "pdf";
  content: string;
  base64?: string;
  mimeType?: string;
  enReady?: boolean;  // 后台英化是否已完成（发送前据此决定是否等待英文版）
}

export interface SessionInfo {
  id: string;
  name: string;
  messages: Message[];
  selectedModel: string;
  lastActive?: number;
  /** 服务端算好的预览（最后一条有内容的消息前 60 字）——列表不再需要消息体 */
  preview?: string;
  /** 服务端记账：消息条数（本地只加载了部分会话时用于显示） */
  message_count?: number;
  /** 消息是否已从后端载入（懒加载标记；localStorage 路径下一律为 true） */
  loaded?: boolean;
}

export interface IdentityFile {
  name: string;
  exists: boolean;
  content: string;
}

export type ViewId = "chat" | "models" | "tools" | "skills" | "cron" | "channels" | "agents" | "settings" | "logs";

export interface CloudModel {
  name: string;
  /** 仅在用户刚输入、尚未保存时短暂存在；列表加载后不应持有明文 key */
  key?: string;
  /** 服务端是否已存 key（UI 用「已保存/未设置」展示，不回读明文） */
  has_key?: boolean;
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
