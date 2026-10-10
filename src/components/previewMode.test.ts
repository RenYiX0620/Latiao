import { describe, expect, it } from "vitest";
import { previewModeFor } from "./PreviewPanel";

/** 预览模式判定（2026-10-10「点审阅不显示」回归）。
 *
 * 现场：write_file 交付的 .md 文本类文件，服务端 /v1/file/preview 返回
 * `kind:"code"` + text、**没有 data_base64**；旧代码把它当 image/pdf：
 * atob("") 建空 blob + setMode("pdf") → 代码渲染分支被空 blob 挡住 → 预览栏空白。
 */
describe("预览模式判定", () => {
  it("文本类（kind=code）走代码渲染，绝不当成二进制", () => {
    expect(previewModeFor({ status: "ok", kind: "code" })).toBe("code");
  });

  it("图/PDF 走各自的二进制渲染", () => {
    expect(previewModeFor({ status: "ok", kind: "image" })).toBe("image");
    expect(previewModeFor({ status: "ok", kind: "pdf" })).toBe("pdf");
    // 未知 kind（服务端将来新增类型）默认按 PDF 处理——保持旧兜底行为
    expect(previewModeFor({ status: "ok", kind: "" })).toBe("pdf");
  });

  it("缺 LibreOffice / 请求失败各有明确出口", () => {
    expect(previewModeFor({ status: "ok", kind: "office", need_soffice: true })).toBe("soffice");
    expect(previewModeFor({ status: "error", message: "invalid path" })).toBe("error");
    expect(previewModeFor(null)).toBe("error");
  });
});
