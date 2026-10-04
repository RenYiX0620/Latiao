import { describe, expect, it } from "vitest";
import {
  endedIcon,
  fmtDuration,
  fmtTime,
  fmtTokens,
  fmtTtft,
  shortModel,
} from "./turnHistory";

describe("fmtTokens", () => {
  it("中文用万、其余用 k/M", () => {
    expect(fmtTokens(0, "zh")).toBe("0");
    expect(fmtTokens(9500, "zh")).toBe("9,500");
    expect(fmtTokens(15000, "zh")).toBe("1.5万");
    expect(fmtTokens(3_120_000, "zh")).toBe("312万");
    expect(fmtTokens(15000, "en")).toBe("15k");
    expect(fmtTokens(1_500_000, "en")).toBe("1.5M");
    expect(fmtTokens(3_120_000, "ja")).toBe("3.1M");
  });
});

describe("fmtDuration", () => {
  it("秒与分钟的两种形态", () => {
    expect(fmtDuration(0)).toBe("—");
    expect(fmtDuration(3200)).toBe("3.2s");
    expect(fmtDuration(42_000)).toBe("42s");
    expect(fmtDuration(125_000)).toBe("2m05s");
  });
});

describe("shortModel", () => {
  it("取末段、去 .gguf、超长截断", () => {
    expect(shortModel("/a/b/Hermes3.6-35B.gguf")).toBe("Hermes3.6-35B");
    expect(shortModel("Qwen3.8-27B-MLX-4bit")).toBe("Qwen3.8-27B-MLX-4bit");
    const long = shortModel("occamy-1.0-abliterated-FIT-REFERENCE-24G-Q5_K_M");
    expect(long.length).toBeLessThanOrEqual(24);
    expect(long.endsWith("…")).toBe(true);
    expect(shortModel("")).toBe("");
  });
});

describe("endedIcon", () => {
  it("已知原因映射图标、未知给空心点", () => {
    expect(endedIcon("completed")).toBe("✓");
    expect(endedIcon("error")).toBe("✗");
    expect(endedIcon("aborted")).toBe("⏹");
    expect(endedIcon("cron")).toBe("🕐");
    expect(endedIcon("stale")).toBe("⏱");
    expect(endedIcon("")).toBe("•");
    expect(endedIcon("whatever")).toBe("•");
  });
});

describe("fmtTime / fmtTtft", () => {
  it("ISO 取 MM-DD HH:MM；首字秒一位小数", () => {
    expect(fmtTime("2026-10-03T10:00:00")).toBe("10-03 10:00");
    expect(fmtTime("")).toBe("");
    expect(fmtTtft(null)).toBe("—");
    expect(fmtTtft(2200)).toBe("2.2s");
  });
});
