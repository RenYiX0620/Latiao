/** 本轮花费的展示文案（成本可见，gap 第 4 步）——纯函数，便于单测。
 *
 * 数据来自 `/v1/context/stats` 的 `turn` 块：
 * 输入（已用/预算）、生成、重采次数与原因、"知识提炼"（工具后自动跑的那次模型调用）。
 * 面板只负责把这里的三行渲染出来；没有数据（旧会话/未开始）返回 null，整块隐藏。
 */
export interface TurnCostLike {
  input_tokens: number;
  gen_tokens: number;
  budget: number;
  budget_percent: number | null;
  retries: number;
  retry_kinds: string[];
  refine_calls: number;
  refine_tokens: number;
  by_source?: Record<string, SourceBucket>;
}

export interface SourceBucket {
  requests: number;
  input_tokens: number;
  output_tokens: number;
}

export interface TurnCostView {
  /** "本轮输入" 行：`128k / 400k（32%）`，无预算时只给已用 */
  input: string;
  /** "本轮开销" 行：生成 / 重采 / 知识提炼 */
  cost: string;
  /** 重采原因（有才给，避免空行） */
  retryKinds: string | null;
  /** 来源行：主循环 / 子代理 / 知识提炼（只有 ≥2 类时才给，避免噪音） */
  sources: string | null;
}

type Fmt = (n: number) => string;
type Tr = (key: string, vars?: Record<string, string | number>) => string;

export function turnCostView(turn: TurnCostLike | null | undefined, fmt: Fmt, t: Tr): TurnCostView | null {
  if (!turn) return null;
  const nothingYet =
    !turn.input_tokens && !turn.retries && !turn.refine_calls && !turn.gen_tokens;
  if (nothingYet) return null;
  return {
    input: turn.budget
      ? t("chat.ctx_turn_input_val", {
          used: fmt(turn.input_tokens),
          budget: fmt(turn.budget),
          pct: `${turn.budget_percent ?? 0}`,
        })
      : t("chat.ctx_turn_input_nobudget", { used: fmt(turn.input_tokens) }),
    cost: t("chat.ctx_turn_cost_val", {
      gen: fmt(turn.gen_tokens),
      retries: `${turn.retries}`,
      refine: `${turn.refine_calls}`,
      refineTok: fmt(turn.refine_tokens),
    }),
    retryKinds:
      turn.retry_kinds && turn.retry_kinds.length > 0
        ? t("chat.ctx_turn_retry_kinds", { kinds: turn.retry_kinds.join(", ") })
        : null,
    sources: sourceParts(turn.by_source, fmt, t),
  };
}

/** 来源分片：每片套自己的模板后拼接（各语序不同，别拼句子） */
function sourceParts(by: Record<string, SourceBucket> | undefined, fmt: Fmt, t: Tr): string | null {
  if (!by) return null;
  const parts: string[] = [];
  const main = by["main_turn"];
  if (main && (main.input_tokens || main.output_tokens)) {
    parts.push(t("chat.ctx_src_main", { tok: fmt(main.input_tokens + main.output_tokens) }));
  }
  const sub = by["subagent"];
  if (sub && (sub.input_tokens || sub.output_tokens)) {
    parts.push(t("chat.ctx_src_subagent", {
      tok: fmt(sub.input_tokens + sub.output_tokens), n: `${sub.requests}`,
    }));
  }
  const ref = by["refine"];
  if (ref && (ref.input_tokens || ref.output_tokens)) {
    parts.push(t("chat.ctx_src_refine", {
      tok: fmt(ref.input_tokens + ref.output_tokens), n: `${ref.requests}`,
    }));
  }
  return parts.length >= 2 ? parts.join(" · ") : null;
}
