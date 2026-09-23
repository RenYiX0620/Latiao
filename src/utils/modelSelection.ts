/**
 * 模型选择与引擎加载的关系判定（2026-09-23，用户发现"对话框显示的模型和引擎里装的不一样"）。
 *
 * 背景：界面上有**两个不同的东西**——① 会话的"选中模型"（下拉框里选的，只是个标签，
 * 发给云端时用它、发给本地引擎时其实被忽略）；② 引擎**真正加载**的模型（模型面板里
 * 那个，只有点加载/停止才会变）。发消息**不会**自动切换引擎（`agent/loop.py` 里本地
 * 回合的 model 名直接取引擎当前加载的那个），所以下拉框可能是个谎。
 *
 * 这里把三条判定抽成纯函数，便于测试：是否需要加载、是否该提示不一致、选项怎么标。
 */
export interface CloudModelLike {
  name: string;
}
export interface EngineStatusLike {
  status?: string;
  model_id?: string;
  model_name?: string;
}

/** 是否是云端模型（按名字与会话选择比对；不在云列表里的非空值视为本地模型）。 */
export function isCloudModel(name: string, cloudModels: CloudModelLike[]): boolean {
  const n = (name || "").trim();
  if (!n) return false;
  return (cloudModels || []).some((m) => m.name === n);
}

/** 已加载模型是否就是这个（本地）：id 或名字任一相等即算。 */
export function engineHasModel(selected: string, engine: EngineStatusLike | null | undefined): boolean {
  const n = (selected || "").trim();
  if (!n || !engine || engine.status !== "running") return false;
  return engine.model_id === n || engine.model_name === n;
}

/** 选了本地模型但它没被加载 → 需要触发一次加载（空值/云端模型不触发）。 */
export function needsEngineLoad(
  selected: string,
  cloudModels: CloudModelLike[],
  engine: EngineStatusLike | null | undefined,
): boolean {
  const n = (selected || "").trim();
  if (!n) return false;
  if (isCloudModel(n, cloudModels)) return false;
  return !engineHasModel(n, engine);
}

/** 会话选择与"实际回答者"不一致时，用于提示的已加载模型名；一致或没引擎时返回 null。 */
export function loadedMismatch(
  selected: string,
  engine: EngineStatusLike | null | undefined,
): string | null {
  if (!engine || engine.status !== "running") return null;
  const loaded = (engine.model_name || engine.model_id || "").trim();
  if (!loaded) return null;
  if (engineHasModel(selected, engine)) return null;
  return loaded;
}
