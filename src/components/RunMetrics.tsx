import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";
import ContextMeter from "./ContextMeter";

interface RunStats {
  turns: number;
  steps: number;
  llm_seconds: number;
  tool_seconds: number;
  ttft_avg: number | null;
  tps: number | null;
}

/**
 * 底部状态栏的运行指标：`N 轮 · M 步、LLM x秒 · 工具调用 y秒 | 首 token 平均 z 秒 · n tok/s | 缓存命中 p%`
 *
 * 数据由侧车在循环内实测（采样步耗时、首 token 延迟、工具执行耗时、引擎返回的生成 token 数），
 * 随消息变化刷新；拿不到时退回会话内的简单计数，保持状态栏始终有内容。
 */
export default function RunMetrics({ sessionId, refreshKey, fallbackTurns, fallbackToolCalls, fallbackTokens, fallbackLimit, modelWarning }: {
  sessionId?: string;
  refreshKey?: number;
  fallbackTurns: number;
  fallbackToolCalls: number;
  fallbackTokens: number;
  fallbackLimit?: number | null;
  /** 会话选择的模型与实际回答者不一致时的说明；状态行空间够，长句放这里（按钮行放不下） */
  modelWarning?: string | null;
}) {
  const { t } = useTranslation();
  const [stats, setStats] = useState<RunStats | null>(null);

  const load = useCallback(async () => {
    if (!sessionId) return;
    try {
      const r = await authFetch(`/v1/context/stats?session_id=${encodeURIComponent(sessionId)}`);
      const d = await r.json();
      if (d && d.status === "ok") setStats(d as RunStats);
    } catch {
      /* 侧车未就绪：保持退回显示 */
    }
  }, [sessionId]);

  useEffect(() => { void load(); }, [load, refreshKey]);

  const turns = stats?.turns || fallbackTurns;
  const steps = stats?.steps ?? 0;
  const llm = stats?.llm_seconds ?? 0;
  const tool = stats?.tool_seconds ?? 0;
  const ttft = stats?.ttft_avg ?? null;
  const tps = stats?.tps ?? null;

  return (
    <>
      <span>
        {steps > 0
          ? t("chat.run_turns_steps", { turns: `${turns}`, steps: `${steps}` })
          : `${turns}${t("chat.run_turns_only")} · ${fallbackToolCalls}${t("chat.run_tool_calls_only")}`}
      </span>
      {modelWarning && (
        <>
          <span className="statusbar-sep">|</span>
          <span style={{ color: "var(--warning, #f59e0b)" }}>⚠️ {modelWarning}</span>
        </>
      )}
      {steps > 0 && (
        <>
          <span>、{t("chat.run_llm_tool", { llm: llm.toFixed(1), tool: tool.toFixed(1) })}</span>
          <span className="statusbar-sep">|</span>
          <span>
            {ttft !== null
              ? t("chat.run_ttft_tps", { ttft: ttft.toFixed(2), tps: `${tps ?? 0}` })
              : "—"}
          </span>
        </>
      )}
      <span className="statusbar-sep">|</span>
      <ContextMeter
        sessionId={sessionId}
        fallbackTokens={fallbackTokens}
        fallbackLimit={fallbackLimit ?? null}
      />
    </>
  );
}
