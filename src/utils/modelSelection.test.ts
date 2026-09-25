import { describe, expect, it } from "vitest";
import { engineHasModel, isCloudModel, loadedMismatch, needsEngineLoad } from "./modelSelection";

const CLOUD = [{ name: "deepseek-chat" }, { name: "gpt-4o" }];
const RUN_MLX = { status: "running", model_id: "Qwen3.8-27B-MLX-4bit", model_name: "Qwen3.8-27B-MLX-4bit" };

describe("模型选择 vs 引擎加载", () => {
  it("云端模型不触发本地加载", () => {
    expect(isCloudModel("deepseek-chat", CLOUD)).toBe(true);
    expect(needsEngineLoad("deepseek-chat", CLOUD, RUN_MLX)).toBe(false);
  });

  it("空值（自动检测）不触发加载", () => {
    expect(needsEngineLoad("", CLOUD, RUN_MLX)).toBe(false);
  });

  it("选的就是已加载的那个 → 不重复加载（避免几十秒白等）", () => {
    expect(engineHasModel("Qwen3.8-27B-MLX-4bit", RUN_MLX)).toBe(true);
    expect(needsEngineLoad("Qwen3.8-27B-MLX-4bit", CLOUD, RUN_MLX)).toBe(false);
  });

  it("选了别的本地模型 → 需要加载（这就是「选即加载」）", () => {
    expect(needsEngineLoad("Hermes3.6-35B-A3B-Uncensored-Genesis-Final-APEX", CLOUD, RUN_MLX)).toBe(true);
  });

  it("引擎没运行时选了本地模型也要加载", () => {
    expect(needsEngineLoad("Hermes-35B", CLOUD, { status: "stopped" })).toBe(true);
    expect(needsEngineLoad("Hermes-35B", CLOUD, null)).toBe(true);
  });

  it("不一致时给出「实际回答者」用于提示；一致或没引擎则不提示", () => {
    expect(loadedMismatch("Hermes-35B", RUN_MLX)).toBe("Qwen3.8-27B-MLX-4bit");
    expect(loadedMismatch("Qwen3.8-27B-MLX-4bit", RUN_MLX)).toBe(null);
    expect(loadedMismatch("Hermes-35B", { status: "stopped" })).toBe(null);
    expect(loadedMismatch("", RUN_MLX)).toBe("Qwen3.8-27B-MLX-4bit");
  });
});
