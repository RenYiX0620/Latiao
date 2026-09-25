"""Tool System Module — plugin loading, seeding, and dispatch."""
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("tool_system")

# ═══════════════════════════════════════════════════════
#  Plugin System: auto-scan sidecar/plugins/ for tool .py files
# ═══════════════════════════════════════════════════════

PLUGINS_DIR = Path(__file__).parent / "plugins"

# Embedded plugin source code for first-run seeding
_SEED_PLUGINS = {
    'read_file.py': r'''"""Read the contents of a file at the given path. Supports ~ expansion."""

import os
import re

# 敏感路径判定**与 run_cmd 共享一份**（cmd_safety.sensitive_read_block）：
# 此前两条路不一致——`cat ~/.local-ai-os/config.json` 被拦，read_file 同一路径
# 免确认读通，明文 API key 因此进了模型上下文/日志/报告（09-23 真机复现，审计 P1）
# 兼容导出：旧代码/测试可能引用这两个常量名（实际判定统一走 sensitive_read_block）
from cmd_safety import _BLOCKED_DIR_SUBSTRINGS as _BLOCKED_SUBSTRINGS  # noqa: F401
from cmd_safety import _BLOCKED_FILE_NAMES as _BLOCKED_FILE_NAMES  # noqa: F401
from cmd_safety import sensitive_read_block

MAX_READ_SIZE = 10000  # chars before truncation

NAME = "read_file"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path. Large files are truncated.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."}
            },
            "required": ["path"]
        }
    }
}


def summarize_progress_tail(text: str, limit: int = 10) -> str:
    """从 PROGRESS.md 尾部提取最近条目，生成中文摘要（可测纯函数）。

    PROGRESS.md 600KB+ 且 2/3 是英文工具日志，直接读会把本地模型带偏成
    英文（09-03 新会话英文事故）。启动协议只需要"了解最近进度"，摘要即可。
    """
    entries = []
    for ln in text.splitlines():
        m = re.match(r"^###\s+(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})\S*\s+(\w+)", ln)
        if m:
            entries.append((f"{m.group(1)[5:]} {m.group(2)}", m.group(3)))
    entries = entries[-limit:]
    if not entries:
        return ""
    out = ["最近工作记录（自动摘要）："]
    for ts, tool in entries:
        out.append(f"- {ts} {tool}")
    return "\n".join(out)


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析（模型常发 "." 或相对路径，
        # 此前直接拒绝并误报"路径穿越"）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args.get("path") or args.get("file") or "")
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
    # 敏感路径（密钥目录/凭据文件/.env 家族/辣条自身 config.json）一律拒绝
    _blocked = sensitive_read_block(p)
    if _blocked:
        return _blocked
    try:
        # 先检测是否为二进制文件(xlsx/zip/png 等),避免 utf-8 codec 报错
        # 让模型困惑。读前 1KB 探测 NUL 字节或已知二进制魔数。
        _BINARY_EXTS = {".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
                        ".bmp", ".webp", ".zip", ".gz", ".tar", ".7z", ".rar",
                        ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2",
                        ".ttf", ".otf", ".icns", ".ico", ".class", ".so", ".dylib", ".dll", ".exe"}
        ext = os.path.splitext(p)[1].lower()
        is_binary = ext in _BINARY_EXTS
        if not is_binary:
            # 探测文件内容:前 1024 字节含 NUL -> 二进制
            with open(p, "rb") as bf:
                head = bf.read(1024)
            if b"\x00" in head:
                is_binary = True
        if is_binary:
            return (f"⚠️ 这是二进制文件({ext or '未知格式'}),无法作为文本读取。\n"
                    f"文件: {p}\n"
                    f"如需查看数据,请读取对应的文本格式文件(如 _raw.json / _description.txt)。")

        # PROGRESS.md 特例：返回最近条目中文摘要而非原始内容
        try:
            from config import PROGRESS_DIR
            if os.path.realpath(p) == os.path.realpath(str(PROGRESS_DIR / "PROGRESS.md")):
                with open(p, "rb") as pf:
                    pf.seek(max(0, os.path.getsize(p) - 8192))
                    tail = pf.read().decode("utf-8", errors="replace")
                summary = summarize_progress_tail(tail)
                if summary:
                    return (summary
                            + "\n\n（PROGRESS.md 共 60 万+ 字符，以上即最近进度摘要，"
                            + "无需再次读取；直接开始执行任务。）")
        except Exception:
            pass  # 摘要失败回退到常规读取，不阻断

        with open(p, "r", encoding="utf-8") as f:
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，"
                + f"仅显示前 {MAX_READ_SIZE} 字符。如需完整内容请分段读取)"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {p}"
    except UnicodeDecodeError:
        # 容错降级：文件混入少量非 UTF-8 字节（如历史轮转切在多字节汉字
        # 中间留下的残缺字节）时仍读出内容，残缺处用 � 替代——
        # 比整个拒绝更利于断点续作（22:46 事故）。
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_READ_SIZE + 1)
            if len(content) > MAX_READ_SIZE:
                est_lines = content.count("\n")
                return (
                    content[:MAX_READ_SIZE]
                    + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行)"
                )
            return content
        except Exception as e2:
            return f"错误：{e2}"
    except Exception as e:
        return f"错误：{e}"
''',
    'write_file.py': r'''"""Write text content to a file. Creates parent directories if needed."""
import os

_BLOCKED_DIRS = ("/etc", "/System", "/usr", "/bin", "/sbin", "/var", "/private/etc")
# 敏感目录/文件名清单统一走 cmd_safety 单点（read_file.py 早已如此）。此前这里留了
# 5 项本地拷贝，与 fallback 的 9 项漂移：~/.netrc、.git-credentials、.env.local
# 在本路径（实际生效的那条）曾放行（2026-09-24 复查发现）。
from cmd_safety import _BLOCKED_DIR_SUBSTRINGS as _BLOCKED_SUBSTRINGS  # noqa: F401
from cmd_safety import sensitive_write_name_block

NAME = "write_file"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text to a file (parent dirs created). Path ending in .docx creates a REAL Word document, .pdf creates a REAL PDF, .xlsx creates a REAL Excel workbook — all from Markdown (headings, lists, **bold**, | tables |) — use this for reports, do NOT write a .py generator script. ⚠️ Requires user confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path. End with .docx for Word, .xlsx for Excel."},
                "content": {"type": "string", "description": "File text. For .docx: Markdown body (title as # heading). For .xlsx: Markdown tables or TSV rows."}
            },
            "required": ["path", "content"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析（模型常发 "." 或相对路径，
        # 此前直接拒绝并误报"路径穿越"）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)




def _md_to_docx_zip(text: str) -> bytes:
    """把 Markdown 风格正文打成**合法 .docx**（纯标准库，不依赖 python-docx）。

    支持：# 标题 1–3 级、- / * 列表、空行分段、**粗体**（无样式近似为正文）。
    """
    import zipfile
    import io
    import re as _re2

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    paras = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            paras.append(("Heading3", esc(line[4:].strip())))
        elif line.startswith("## "):
            paras.append(("Heading2", esc(line[3:].strip())))
        elif line.startswith("# "):
            paras.append(("Heading1", esc(line[2:].strip())))
        elif line.startswith("#### "):
            paras.append(("Heading3", esc(line[5:].strip())))
        elif _re2.match(r"^\s*[-*] ", line):
            paras.append(("ListParagraph", esc(_re2.sub(r"^\s*[-*] ", "", line))))
        else:
            # 去掉 **粗体** 星号
            body = _re2.sub(r"\*\*(.+?)\*\*", r"\1", line.strip())
            paras.append(("Normal", esc(body)))
    if not paras:
        paras = [("Normal", "")]

    body_xml = []
    for style, txt in paras:
        if style == "ListParagraph":
            txt = "• " + txt
        body_xml.append(
            f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{txt}</w:t></w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body_xml)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


_DOCX_LATIN_FONT = "Arial"   # macOS/Windows 默认都有；中文另有 eastAsia（见 _docx_cjk_font）


def _style_profile(text: str) -> dict:
    """按**正文语言**选报告规范（不看 UI 语言：中文用户也会写英文报告）。

    中文报告：标题居中、正文小四(12pt)、**首行缩进 2 字符**、1.5 倍行距、段间距 0；
    日文：同上但缩进 1 字符；其它（英/俄…）：无缩进、段后 6pt、1.15 倍行距。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    kana = any("\u3040" <= ch <= "\u30ff" for ch in text)
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if kana:
        return {"body_pt": 12, "indent_pt": 12, "space_after": 0, "line": 1.5}
    if cjk >= 20 and cjk * 4 >= latin:
        return {"body_pt": 12, "indent_pt": 24, "space_after": 0, "line": 1.5}
    return {"body_pt": 11, "indent_pt": 0, "space_after": 6, "line": 1.15}


def _docx_cjk_font() -> str:
    """按平台挑一个该平台默认就有的中文字体名（英文名，避免区域差异）。
    口径：Word **按字体名引用、不嵌字体** → 写该平台一定存在的名字最稳；
    "跨端完全一致"由 PDF 路径（reportlab + 随包 OFL 字体）承担。
    """
    import platform
    if platform.system() == "Windows":
        return "Microsoft YaHei"
    if platform.system() == "Darwin":
        return "PingFang SC"
    return "Noto Sans SC"


def _apply_fonts(run, cjk: str) -> None:
    """中文字体必须落到 w:eastAsia —— 只设 font.name 对中文不生效（走主题字体）。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    run.font.name = _DOCX_LATIN_FONT
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    for attr in ("w:ascii", "w:hAnsi"):
        rFonts.set(qn(attr), _DOCX_LATIN_FONT)
    rFonts.set(qn("w:eastAsia"), cjk)


def _is_numeric(cell: str) -> bool:
    """数字列（含千分位/百分号/正负号）→ 右对齐。"""
    import re as _rn
    s = cell.strip().replace(",", "").replace("%", "").replace("+", "")
    return bool(s) and bool(_rn.fullmatch(r"-?\d+(\.\d+)?", s))


def _md_to_docx(text: str) -> bytes | None:
    """Markdown → 带完整样式的 .docx（python-docx）。库缺失返回 None，由调用方降级。"""
    try:
        import io
        from docx import Document
        from docx.shared import Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError:
        return None

    cjk = _docx_cjk_font()
    doc = Document()

    # 页面：A4 + 2.5cm 边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    for m in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(sec, m, Cm(2.5))

    # 报告规范（按正文语言）：中文=小四(12pt)/首行缩进 2 字符/1.5 倍行距/段间距 0
    prof = _style_profile(text)
    normal = doc.styles["Normal"]
    normal.font.size = Pt(prof["body_pt"])
    normal.font.name = _DOCX_LATIN_FONT
    normal.paragraph_format.space_after = Pt(prof["space_after"])
    normal.paragraph_format.line_spacing = prof["line"]
    if prof["indent_pt"]:
        normal.paragraph_format.first_line_indent = Pt(prof["indent_pt"])
    _nrf = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi"):
        _nrf.set(qn(attr), _DOCX_LATIN_FONT)
    _nrf.set(qn("w:eastAsia"), cjk)
    for name, size, align in (("Heading 1", 22, WD_ALIGN_PARAGRAPH.CENTER),
                              ("Heading 2", 16, None),
                              ("Heading 3", 14, None)):
        st = doc.styles[name]
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor(0x1F, 0x2A, 0x37)
        st.paragraph_format.space_before = Pt(8)
        # 文档标题（# 一级）居中：中文报告习惯；二级/三级保持左对齐
        st.paragraph_format.space_after = Pt(12 if align is not None else 6)
        if align is not None:
            st.paragraph_format.alignment = align
        _srf = st.element.get_or_add_rPr().get_or_add_rFonts()
        for attr in ("w:ascii", "w:hAnsi"):
            _srf.set(qn(attr), _DOCX_LATIN_FONT)
        _srf.set(qn("w:eastAsia"), cjk)

    # 页脚页码（PAGE 域）
    footer_p = sec.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _fld = OxmlElement("w:fldSimple")
    _fld.set(qn("w:instr"), "PAGE")
    footer_p._p.append(_fld)

    def add_par(text_line: str, style: str | None = None, indent: bool = False):
        par = doc.add_paragraph(style=style)
        if indent:
            par.paragraph_format.left_indent = Cm(0.75)
        if style is not None or indent:
            # 标题/列表/引用不吃正文的"首行缩进 2 字符"（那是正文段落的规矩）
            par.paragraph_format.first_line_indent = Pt(0)
        for i, seg in enumerate(text_line.split("**")):   # 行内 **粗体**
            if not seg:
                continue
            r = par.add_run(seg)
            if i % 2 == 1:
                r.bold = True
            _apply_fonts(r, cjk)
        return par

    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # 表格：| a | b | + 分隔行
        if line.startswith("|") and line.endswith("|") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt.startswith("|") and set(nxt) <= set("|-: "):
                rows: list[list[str]] = []
                j = i
                while j < len(lines) and lines[j].strip().startswith("|"):
                    cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    if not (cells and set("".join(cells)) <= set("-: ")):
                        rows.append(cells)
                    j += 1
                if rows:
                    width = len(rows[0])
                    t = doc.add_table(rows=len(rows), cols=width)
                    t.style = "Table Grid"
                    t.autofit = False
                    numeric_cols = {
                        c for c in range(width)
                        if all(_is_numeric(r[c]) for r in rows[1:] if c < len(r)) and len(rows) > 1
                    }
                    for ri, row in enumerate(rows):
                        for ci, cell in enumerate(row[:width]):
                            tc = t.cell(ri, ci)
                            par = tc.paragraphs[0]
                            par.paragraph_format.first_line_indent = Pt(0)  # 单元格不缩进
                            r = par.add_run(cell)
                            _apply_fonts(r, cjk)
                            if ri == 0:
                                r.bold = True
                                shd = OxmlElement("w:shd")
                                shd.set(qn("w:fill"), "D9E2F3")
                                tc._tc.get_or_add_tcPr().append(shd)
                            elif ci in numeric_cols:
                                par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    trPr = t.rows[0]._tr.get_or_add_trPr()   # 表头跨页重复
                    trPr.append(OxmlElement("w:tblHeader"))
                    doc.add_paragraph()
                i = j
                continue
        if line.startswith("#"):
            lvl = min(len(line) - len(line.lstrip("#")), 3)
            add_par(line.lstrip("#").strip(), style=f"Heading {lvl}")
        elif line.startswith(("- ", "* ", "+ ")):
            add_par(line[2:].strip(), style="List Bullet")
        elif len(line) > 2 and line[0].isdigit() and line[1] in ".、)":
            add_par(line[2:].strip(), style="List Number")
        elif line.startswith("> "):
            par = add_par(line[2:].strip(), indent=True)
            for r in par.runs:
                r.italic = True
        elif len(line) >= 3 and set(line) <= set("-—="):
            pass                                          # 分割线
        else:
            add_par(line)
        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_PDF_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")


def _pdf_fonts() -> tuple[str, str, bool]:
    """(regular, bold, 是否内嵌)。优先随包 OFL TTF（Noto Sans SC，可再分发）；
    没有就退回 reportlab 内置 CID 中文字体（不内嵌，靠阅读器替换，字形随阅读器）。"""
    from reportlab.pdfbase import pdfmetrics
    reg = os.path.join(_PDF_FONT_DIR, "NotoSansSC-Regular.ttf")
    bold = os.path.join(_PDF_FONT_DIR, "NotoSansSC-Bold.ttf")
    if os.path.exists(reg):
        try:
            from reportlab.pdfbase.ttfonts import TTFont
            pdfmetrics.registerFont(TTFont("LatiaoCJK", reg))
            if os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("LatiaoCJK-Bold", bold))
                return "LatiaoCJK", "LatiaoCJK-Bold", True
            return "LatiaoCJK", "LatiaoCJK", True
        except Exception:
            pass
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light", False


def _md_to_pdf(text: str) -> bytes | None:
    """Markdown → PDF（reportlab）。库缺失返回 None；样式沿用 _style_profile 的语言档案。"""
    try:
        import io
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except ImportError:
        return None

    reg_font, bold_font, embedded = _pdf_fonts()
    prof = _style_profile(text)
    body_pt = prof["body_pt"]
    leading = body_pt * prof["line"]
    hs = {"1": 22, "2": 16, "3": 14}
    st_h = {k: ParagraphStyle(f"h{k}", fontName=bold_font, fontSize=v, leading=v * 1.3,
                              alignment=TA_CENTER if k == "1" else 0,
                              spaceBefore=8, spaceAfter=12 if k == "1" else 6,
                              textColor=colors.HexColor("#1F2A37")) for k, v in hs.items()}
    st_body = ParagraphStyle("body", fontName=reg_font, fontSize=body_pt, leading=leading,
                             firstLineIndent=prof["indent_pt"], spaceAfter=prof["space_after"],
                             wordWrap="CJK")
    st_list = ParagraphStyle("li", parent=st_body, firstLineIndent=0, leftIndent=14, spaceAfter=2)
    st_cell = ParagraphStyle("cell", parent=st_body, firstLineIndent=0, spaceAfter=0,
                             leftIndent=0, fontSize=body_pt - 1, leading=(body_pt - 1) * 1.3,
                             wordWrap="CJK")
    st_cell_r = ParagraphStyle("cellr", parent=st_cell, alignment=TA_RIGHT)
    st_head = ParagraphStyle("th", parent=st_cell, fontName=bold_font)

    def esc(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts = s.split("**")                     # 行内 **粗体** → <b>
        return "".join(f"<b>{p}</b>" if i % 2 else p for i, p in enumerate(parts))

    flow = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|") and line.endswith("|") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt.startswith("|") and set(nxt) <= set("|-: "):
                rows: list[list[str]] = []
                j = i
                while j < len(lines) and lines[j].strip().startswith("|"):
                    cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    if not (cells and set("".join(cells)) <= set("-: ")):
                        rows.append(cells)
                    j += 1
                if rows:
                    width = len(rows[0])
                    num_cols = {c for c in range(width)
                                if len(rows) > 1 and all(_is_numeric(r[c]) for r in rows[1:] if c < len(r))}
                    data = []
                    for ri, row in enumerate(rows):
                        cells = []
                        for ci in range(width):
                            cell = row[ci] if ci < len(row) else ""
                            style = st_head if ri == 0 else (st_cell_r if ci in num_cols else st_cell)
                            cells.append(Paragraph(esc(cell), style))
                        data.append(cells)
                    t = Table(data, repeatRows=1, hAlign="LEFT")
                    t.setStyle(TableStyle([
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BFBFBF")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E2F3")),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]))
                    flow.append(t)
                    flow.append(Spacer(1, 10))
                i = j
                continue
        if line.startswith("#"):
            lvl = str(min(len(line) - len(line.lstrip("#")), 3))
            flow.append(Paragraph(esc(line.lstrip("#").strip()), st_h[lvl]))
        elif line.startswith(("- ", "* ", "+ ")):
            flow.append(Paragraph("• " + esc(line[2:].strip()), st_list))
        elif len(line) > 2 and line[0].isdigit() and line[1] in ".、)":
            flow.append(Paragraph(esc(line), st_list))
        elif line.startswith("> "):
            flow.append(Paragraph("<i>" + esc(line[2:].strip()) + "</i>", st_list))
        elif len(line) >= 3 and set(line) <= set("-—="):
            pass
        else:
            flow.append(Paragraph(esc(line), st_body))
        i += 1

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(bold_font if embedded else reg_font, 9)
        canvas.drawCentredString(A4[0] / 2.0, 1.4 * cm, str(canvas.getPageNumber()))
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, topMargin=2.5 * cm, bottomMargin=2.5 * cm,
                      leftMargin=2.5 * cm, rightMargin=2.5 * cm,
                      title="Latiao", author="Latiao").build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _sheet_name(title: str, n: int) -> str:
    import re as _rs
    name = _rs.sub(r'[\\/*?:\[\]]', "", title).strip()
    return name[:31] or f"Sheet{n}"


def _tables_to_xlsx(text: str, dest: str) -> str:
    """Markdown 表格 / TSV → 真 .xlsx（openpyxl，随包自带）。

    多个表格 → 多个工作表；表格前的 # 标题用作表名。
    """
    from openpyxl import Workbook

    def split_row(line: str):
        s = line.strip()
        if not s or s.startswith("#"):
            return None
        if s.startswith("|") or s.count("|") >= 2:
            return [c.strip() for c in s.strip("|").split("|")]
        if "\t" in s:
            return [c.strip() for c in s.split("\t")]
        return None

    def is_sep(line: str) -> bool:
        s = line.strip().replace("|", "").replace(" ", "").replace(":", "")
        return bool(s) and all(c in "-+" for c in s)

    tables: list[tuple[str, list[list[str]]]] = []
    title = ""
    cur: list[list[str]] = []

    def flush():
        nonlocal cur
        if cur:
            tables.append((title, cur))
            cur = []

    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.strip().startswith("#"):
            flush()
            title = raw.strip().lstrip("#").strip()
            continue
        if is_sep(raw):
            continue
        row = split_row(raw)
        if row is None:
            flush()
            continue
        cur.append(row)
    flush()

    if not tables:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        tables = [("", [[ln] for ln in lines] or [[""]])]

    wb = Workbook()
    wb.remove(wb.active)
    for i, (name, rows) in enumerate(tables, 1):
        ws = wb.create_sheet(_sheet_name(name, i))
        for row in rows:
            ws.append(row)

    wb.save(dest)
    total = sum(len(r) for _, r in tables)
    return f"{len(tables)} 个工作表，共 {total} 行"


def execute(args: dict) -> str:
    content = args.get("content")
    if content is None:
        content = args.get("text", "")
    content = str(content)
    path_arg = args.get("path") or args.get("file") or ""
    p = _safe_path(path_arg)
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
    # 系统目录一律拒绝写入
    if any(p == d or p.startswith(d + os.sep) for d in _BLOCKED_DIRS):
        return f"⛔ Blocked: 不允许写入系统目录 - {p}"
    # 敏感目录（密钥/凭证）一律拒绝
    if any(s in p for s in _BLOCKED_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {p}"
    # 敏感文件名一律拒绝（含 .env 家族，模板除外）——清单在 cmd_safety 单点
    _fn_block = sensitive_write_name_block(p)
    if _fn_block:
        return _fn_block
    # sidecar 的 plugins/ 目录一律拒绝（写入后下次启动会被 import 执行 = RCE）
    # 自动加载目录（extensions/skills）与应用配置：写进去的代码下次启动即执行（审计 P1 ⑧）
    try:
        from cmd_safety import sensitive_write_block
        _blk = sensitive_write_block(p)
        if _blk:
            return _blk
    except Exception:
        # fail-closed：封印不可用时拒绝写入（此前 pass = 放行，审计复查）
        return "⛔ Blocked: 写入封印不可用，已拒绝"
    sidecar_plugins = os.path.realpath(os.path.dirname(__file__))
    if p == sidecar_plugins or p.startswith(sidecar_plugins + os.sep):
        return f"⛔ Blocked: 不允许写入插件目录 - {p}"
    # .docx：正文按 Markdown 风格打包成**合法 Word**（纯标准库 OOXML）
    # （2026-09-24：此前模型只能写 .docx.py 生成脚本，用户拿到的不是 Word）。
    # .pdf：Markdown → PDF（reportlab；版式沿用同一套语言档案）
    if p.lower().endswith(".pdf") and not content.startswith("%PDF"):
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            blob = _md_to_pdf(content)
            if blob is None:
                return ("⛔ 无法生成 PDF：reportlab 未安装（导入失败）。"
                        "可先写 .docx，再让用户用 Office 另存为 PDF。")
            with open(p, "wb") as f:
                f.write(blob)
            return (
                f"✅ 已生成 PDF：{p}（{len(blob)} 字节｜引擎：reportlab）\n"
                "支持 Markdown：# 标题（一级居中）、- 列表、**加粗**、| 表格 |。"
            )
        except Exception as e:
            return f"错误：生成 PDF 失败：{e}"
    if p.lower().endswith(".docx") and not content.startswith("PK\x03\x04"):
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            # 排版引擎：python-docx 优先（真样式/真表格/中文字体），
            # 库缺失 → 现有裸 OOXML 降级；结果里注明用的是哪个引擎（可验证信号）
            blob = None
            engine = "python-docx"
            try:
                blob = _md_to_docx(content)
            except Exception:
                blob = None
            if blob is None:
                engine = "裸 OOXML（python-docx 不可用，已降级）"
                blob = _md_to_docx_zip(content)
            with open(p, "wb") as f:
                f.write(blob)
            return (
                f"✅ 已生成 Word 文档：{p}（{len(blob)} 字节，"
                f"{content.count(chr(10)) + 1} 行正文 → .docx｜引擎：{engine}）\n"
                "支持 Markdown：# 标题（1–3 级）、- 列表、1. 有序列表、**加粗**、"
                "| 表格 |（自动表头/数字右对齐/跨页重复）。"
            )
        except Exception as e:
            return f"错误：生成 .docx 失败：{e}"
    # .xlsx：Markdown 表格 / TSV → 真 Excel（openpyxl，随包自带）
    if p.lower().endswith(".xlsx") and not content.startswith("PK\x03\x04"):
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            info = _tables_to_xlsx(content, p)
            return (
                f"✅ 已生成 Excel 表格：{p}（{info}）\n"
                "支持 Markdown 表格、TSV；多个表格 → 多个工作表，表格前 # 标题作表名。"
            )
        except Exception as e:
            return f"错误：生成 .xlsx 失败：{e}"
    import re as _re
    _bin_name = _re.search(r"\.(xls|pptx?|pdf|odt|ods|odp)(\.|$)", p, _re.I)
    if _bin_name and not content.startswith("PK\x03\x04"):
        return (
            f"⛔ {p} 是 {(_bin_name.group(1))} 二进制格式，write_file 不能直接写文本。\n"
            "Word（.docx）/ Excel（.xlsx）已支持直接写正文；\n"
            "演示文稿请写 gen.py 后 run_cmd 执行，交付真正文件。"
        )
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{p}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"
''',
    'list_dir.py': r'''"""List the contents of a directory."""
import os

NAME = "list_dir"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory to list."}
            },
            "required": ["path"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析：模型常发 "."（列当前目录），
        # 此前直接拒绝并误报"路径穿越"（18:13 事故——自检任务第一步就被拦）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args.get("path") or args.get("directory") or ".")
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
''',
    'run_cmd.py': r'''"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import shlex
import subprocess

# 安全不变量单点定义（破坏/混淆/白名单/敏感路径），
# fallback 与 seed 共用同一模块，消除三处漂移（审计 P0）。
from cmd_safety import (
    child_env,
    DESTRUCTIVE_PATTERNS,
    OBFUSCATION_PATTERNS,
    SAFE_CMD_RE,
    check_cmd_with_script,
)

# 保留旧名字导出：test_security 等引用这些名字
_DESTRUCTIVE_PATTERNS = DESTRUCTIVE_PATTERNS
_OBFUSCATION_PATTERNS = OBFUSCATION_PATTERNS
_ALWAYS_ALLOWED = SAFE_CMD_RE

NAME = "run_cmd"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_cmd",
        "description": "Run ONE single command and return its output (no shell). ⚠️ Requires user confirmation. Destructive commands are always blocked. Do NOT use shell operators like && | ; > < — they are not supported; call this tool once per command instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "A single command with arguments, e.g. 'python3 -m pytest -v'. No shell operators (&& | ; >)."}
            },
            "required": ["cmd"]
        }
    }
}

# Shell operators that are NOT supported because we run with shell=False.
# If these slip through, shlex.split() passes them as literal arguments and the
# command silently misbehaves (e.g. "cd x && pytest" only runs `cd`, exits 0,
# prints nothing — the agent then hallucinates success). Detect and reject loudly.
def _reject_shell_operators(cmd: str) -> str | None:
    """引号外的 shell 操作符才拦；引号内（python -c "...;..."）不误杀。"""
    from cmd_safety import find_unsupported_shell_op, unsupported_op_message
    op = find_unsupported_shell_op(cmd)
    if not op:
        return None
    return unsupported_op_message(op)



def _run_pipeline(left: str, right: str, timeout: int) -> str:
    import shlex as _shlex
    import subprocess as _sp
    from cmd_safety import child_env
    p1 = _sp.Popen(_shlex.split(left), shell=False, stdout=_sp.PIPE, stderr=_sp.PIPE,
                   text=True, env=child_env())
    p2 = _sp.Popen(_shlex.split(right), shell=False, stdin=p1.stdout, stdout=_sp.PIPE,
                   stderr=_sp.PIPE, text=True, env=child_env())
    if p1.stdout:
        p1.stdout.close()
    try:
        out, err = p2.communicate(timeout=timeout)
    except _sp.TimeoutExpired:
        p1.kill()
        p2.kill()
        return "超时"
    p1.wait(timeout=5)
    body = (out or "").strip()
    if p2.returncode != 0:
        body += f"\n(退出码: {p2.returncode})"
        if err and err.strip():
            body += f"\n{err.strip()}"
    return body or "(无输出)"


def execute(args: dict) -> str:
    cmd = (args.get("cmd") or args.get("command", "")).strip()

    # ── Reject unsupported shell syntax FIRST (before whitelist shortcut) ──
    rejected = _reject_shell_operators(cmd)
    if rejected:
        return rejected



    # 单管道：Popen A | B（shell=False 两侧各自 exec）
    _pipe = None
    try:
        from cmd_safety import split_pipeline
        _pipe = split_pipeline(cmd)
    except Exception:
        _pipe = None

    # 尾部 stdout 重定向：shell=False 等价实现（写文件）
    _redir = None
    try:
        from cmd_safety import split_stdout_redirect
        _redir = split_stdout_redirect(cmd)
    except Exception:
        _redir = None
    if _redir:
        cmd, _target, _append = _redir

    # ── Whitelist fast path for simple safe commands ──
    # 整条命令必须完全匹配白名单形态，且命中后仍走完整安全检查——
    # 此前只校验首 token 且命中即整条直行，"env curl ..." 可绕过全部
    # 黑名单执行任意命令（P0）。env/printenv 已从白名单移除。
    if SAFE_CMD_RE.match(cmd) and len(cmd) < 200:
        denied = check_cmd_with_script(cmd)
        if denied:
            return denied
        try:
            r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, env=child_env(), timeout=10)
            if _redir:
                with open(_target, "a" if _append else "w", encoding="utf-8") as _f:
                    _f.write(r.stdout or "")
                return f"已写入 {_target}（{len(r.stdout or '')} 字符）" + (f"\n{r.stderr.strip()}" if r.stderr.strip() else "")
            return r.stdout.strip() or r.stderr.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"超时: {cmd}"
        except Exception as e:
            return f"错误：{e}"

    # ── Full safety check for everything else（含脚本内容审查）──
    denied = check_cmd_with_script(cmd)
    if denied:
        return denied

    # ── Length limit ──
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"

    # ── Execute ──
    if _pipe:
        return _run_pipeline(_pipe[0], _pipe[1], 300)
    # 30s 会截断 npm install/构建类长任务——放宽到 300s（P2-15）
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, env=child_env(), timeout=300)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\n{r.stderr.strip()}"
        if _redir:
            body = r.stdout or ""
            with open(_target, "a" if _append else "w", encoding="utf-8") as _f:
                _f.write(body)
            return (f"已写入 {_target}（{len(body)} 字符）"
                    + (f"\n退出码: {r.returncode}" if r.returncode else "")
                    + (f"\n{r.stderr.strip()}" if r.stderr and r.stderr.strip() else ""))
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return (f"超时: 命令已运行 5 分钟被截断。长任务请拆分为多步执行，"
                f"或改用后台方式（nohup ... &）。\n命令: {cmd}")
    except Exception as e:
        return f"错误：{e}"
''',
    'open_folder.py': r'''"""Open a folder in Finder (macOS only)."""
import os
import platform
import subprocess

NAME = "open_folder"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "open_folder",
        "description": "Open a folder in Finder (macOS only).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the folder to open."}
            },
            "required": ["path"]
        }
    }
}

IS_MACOS = platform.system() == "Darwin"


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析（此前直接拒绝并误报"路径穿越"）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args.get("path") or args.get("directory") or "")
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
    if IS_MACOS:
        subprocess.Popen(["open", p])
        return f"✅ 已在 Finder 中打开：{p}"
    # Fallback: list directory on non-macOS
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
''',
    'open_app.py': r'''"""Open an application by name. macOS native, Windows via start."""
import platform
import re as _re
import subprocess

IS_WINDOWS = platform.system() == "Windows"

NAME = "open_app"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": "Open a macOS application by name. Use this when the user asks to open an app. Supports both English names (Photos, Safari, Mail) and Chinese names (照片/相册, 浏览器, 邮件).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "App name in English or Chinese (e.g., 'Photos', 'Safari', '照片', '浏览器')."}
            },
            "required": ["name"]
        }
    }
}

_APP_ALIASES = {
    "照片": "Photos", "相册": "Photos", "photo": "Photos",
    "音乐": "Music", "music": "Music",
    "浏览器": "Safari", "safari": "Safari",
    "邮件": "Mail", "mail": "Mail",
    "日历": "Calendar", "calendar": "Calendar",
    "备忘录": "Notes", "notes": "Notes",
    "提醒": "Reminders", "reminders": "Reminders",
    "计算器": "Calculator", "calculator": "Calculator",
    "终端": "Terminal", "terminal": "Terminal",
    "设置": "System Settings", "系统设置": "System Settings", "偏好设置": "System Settings",
    "App Store": "App Store", "app store": "App Store",
    "地图": "Maps", "maps": "Maps",
    "天气": "Weather", "weather": "Weather",
    "时钟": "Clock", "clock": "Clock",
    "查找": "Find My", "find my": "Find My",
}


def execute(args: dict) -> str:
    name = str(args.get("name") or args.get("app") or "")
    resolved = _APP_ALIASES.get(name, name)
    if IS_WINDOWS:
        # Windows 那条走 cmd /c start：cmd.exe 会二次解释命令行，应用名里的
        # & | ^ < > " % 会被当命令分隔符/转义（模型可以塞 "x & del ..."）。
        # 应用名不需要这些字符，直接拒绝（审查 2026-09-23）。
        if _re.search(r'[&|^<>"%\r\n\t]', resolved):
            return f"⛔ 应用名含不允许的字符：{resolved}"
        try:
            subprocess.Popen(["cmd", "/c", "start", "", resolved])
            return f"✅ 已打开：{resolved}"
        except Exception as e:
            return f"无法打开 {resolved}: {e}"
    try:
        r = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            err = r.stderr.strip() or "应用不存在或无法打开"
            return f"❌ 无法打开 {resolved}: {err}"
        return f"✅ 已打开应用：{resolved}"
    except subprocess.TimeoutExpired:
        return f"❌ 打开 {resolved} 超时"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"
''',
    'search_files.py': r'''"""Search for files matching a glob pattern in a directory."""
import glob as glob_mod
import os

NAME = "search_files"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "Search for files matching a glob pattern in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Absolute path to the directory to search in."},
                "pattern": {"type": "string", "description": "Glob pattern to match (e.g., '*.py', '**/*.md')."}
            },
            "required": ["directory", "pattern"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    directory = _safe_path(args.get("directory") or args.get("path") or "")
    if directory is None:
        return "⛔ Blocked: path traversal not allowed"
    pattern = str(args.get("pattern") or args.get("query") or "")
    # pattern 不允许是绝对路径或包含 '..'（否则可逃出 directory）
    if os.path.isabs(pattern) or ".." in pattern.split("/") or ".." in pattern.split("\\"):
        return f"⛔ Blocked: pattern 不允许是绝对路径或包含 '..' - {pattern}"
    try:
        search_path = os.path.join(directory, pattern)
        matches = glob_mod.glob(search_path, recursive=True)
        if not matches:
            return f"No files matching '{pattern}' found in {directory}"
        lines = []
        for m in sorted(matches)[:50]:
            icon = "📁" if os.path.isdir(m) else "📄"
            lines.append(f"  {icon} {m}")
        result = f"Search results for '{pattern}' in {directory}:\n" + "\n".join(lines)
        if len(matches) > 50:
            result += f"\n  ... and {len(matches) - 50} more results"
        return result
    except Exception as e:
        return f"Error searching files: {e}"
''',
    'tavily_search.py': r'''"""Search the web using Tavily Search API."""
import json
import os
import subprocess
import sys as _sys
from pathlib import Path

import httpx

# 插件目录加入 sys.path：execute() 里懒加载兄弟模块 _time_guard 用
_sys.path.insert(0, str(Path(__file__).parent))

NAME = "tavily_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "首选网络搜索工具（Tavily API，已配置 Key）。需要实时信息、最新新闻、当前事实、或超出训练数据的内容时优先使用本工具。返回标题、URL、内容摘要、发布时间和 AI 摘要。同一任务只调用一次搜索即可，不要与 web_search/bing_search 并行调用。⚠️ 问“今天/最新/本周/盘前/收盘”这类**时效性问题**时，务必带 topic=\"news\"（或让工具自动判定），否则可能返回几个月前的旧文章，数字会严重失真。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and use keywords."
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Search depth: 'basic' (faster, 1-2s) or 'advanced' (thorough, 5-10s). Default: basic."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (1-10). Default: 5."
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "检索类型：'news' 只返回**近期新闻**（配合 days 用，时效问题必须选它）；'general' 为通用检索。不填时由工具按问题里的时间词自动判定。"
                },
                "days": {
                    "type": "integer",
                    "description": "topic='news' 时回溯的天数（1-30，默认 7）。问“今天/昨日”用 1-3，问“本周”用 7。"
                },
            },
            "required": ["query"],
        },
    },
}

CONFIG_FILE = Path.home() / ".local-ai-os" / "config.json"

# 时效性问法：命中就自动切到 news 检索（09-20 事故：问“周五大盘”却拿到三个月前的旧新闻）
_RECENT_HINTS = (
    "今天", "今日", "最新", "昨日", "昨天", "昨晚", "本周", "这周", "上周", "盘前", "盘后",
    "收盘", "刚刚", "现在", "目前", "实时", "最新消息",
    "today", "latest", "yesterday", "tonight", "this week", "last week", "close", "closing",
    "now", "current", "breaking",
)


def _needs_recent(query: str) -> bool:
    q = (query or "").lower()
    return any(h in q for h in _RECENT_HINTS)


def _get_api_key() -> str | None:
    """Read Tavily API key from config file or environment variable."""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key
    # Try macOS Keychain via security CLI
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("tavily_api_key")
    except Exception:
        pass
    return None


async def execute(args: dict) -> str:
    api_key = _get_api_key()
    if not api_key:
        return (
            "⚠️ Tavily API Key 未配置。\n"
            "请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。\n"
            "免费注册：https://tavily.com"
        )

    query = str(args.get("query") or args.get("q") or "")
    search_depth = args.get("search_depth", "basic")
    # 模型常给 "high"/"deep" 等非法值 → 映射为 advanced，避免 HTTP 400 整轮失败
    if search_depth not in ("basic", "advanced"):
        search_depth = "advanced"
    try:
        n = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        n = 5
    max_results = max(1, min(n, 10))

    # 时效判定：显式 topic 优先，否则按问题里的时间词自动选（09-20 修复）
    topic = str(args.get("topic") or "").strip().lower()
    if topic not in ("general", "news"):
        topic = "news" if _needs_recent(query) else "general"
    days = 0
    if topic == "news":
        try:
            days = int(args.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            days = 3 if any(h in (query or "").lower()
                            for h in ("今天", "今日", "昨天", "昨日", "刚刚", "today", "latest")) else 7
        days = max(1, min(days, 30))

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "topic": topic,
    }
    if topic == "news":
        payload["days"] = days

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search"),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            answer = data.get("answer", "")

            if not results and not answer:
                return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

            _scope = f"（新闻检索·近 {days} 天）" if topic == "news" else ""
            lines = [f"🔍 Tavily 搜索: {query} {_scope}\n".rstrip() + "\n"]

            if answer:
                lines.append(f"📝 {answer}\n")

            if results:
                lines.append(f"📎 共 {len(results)} 条结果:\n")
                for i, r in enumerate(results, 1):
                    title = r.get("title", "No title")
                    url = r.get("url", "")
                    content = r.get("content", "")
                    if len(content) > 300:
                        content = content[:300] + "..."
                    pub = r.get("published_date") or r.get("published") or ""
                    lines.append(f"{i}. **{title}**")
                    if pub:
                        lines.append(f"   🗓 发布时间: {pub}")
                    lines.append(f"   {url}")
                    lines.append(f"   {content}\n")

            out = "\n".join(lines)
            # 数据时效提醒（09-20）：网页正文里的数字往往不是目标日期的，
            # 弱模型会直接照抄 → 必须明示"先核对发布日期"
            if topic == "news":
                out += (f"\n\n⚠️ 以上是**近 {days} 天**的新闻检索结果。引用其中任何数字前，"
                        "先核对该条的**发布时间**与你需要的日期是否一致；不一致或找不到目标日期时，"
                        "必须写明「未获取到该日数据」，不要用近似数字替代。")
            # 搜索词日期滑差自检：query 带旧日期时提示核对相对时间换算（防锚定）
            from _time_guard import check_query_date
            guard = check_query_date(query)
            if guard:
                out += "\n\n" + guard
            return out

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"
''',
}

def _seed_default_plugins():
    """Create default plugin files on first run; refresh stale seeds the user hasn't modified.

    Manifest (PLUGINS_DIR/.seed_manifest.json) records filename → sha256 of the seed
    source at write time. On startup:
      - file missing                    → write seed, record hash
      - file exists, no manifest entry  → **把当前内容记为基线**（不覆盖）——
        此前直接跳过，manifest 恒为 {}，seed 更新永远不下发（审计 A6）
      - file hash == manifest hash      → user didn't touch it → overwrite with new seed
      - file hash != manifest hash      → user modified it → leave it alone

    09-23（审计 A5）：内嵌副本必须与 sidecar/plugins/ 现行文件逐字节一致——
    由 tests/test_seed_plugins_sync.py 守着。否则"让 seed 生效"的第一步就是
    用旧副本把现行插件覆写回退（实测 run_cmd 长任务超时 300s→30s、
    read_file 丢掉 PROGRESS 摘要）。
    """
    try:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path = PLUGINS_DIR / ".seed_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                manifest = {}
        except Exception:
            manifest = {}
        for filename, source in _SEED_PLUGINS.items():
            # 每个文件独立兜底：插件目录不可写（只读安装位置）时，不能因为第 1 个
            # 文件失败就跳过后面所有文件
            try:
                filepath = PLUGINS_DIR / filename
                new_hash = _sha256(source)
                if not filepath.exists():
                    _atomic_write(filepath, source)
                    manifest[filename] = new_hash
                    continue
                try:
                    current_hash = _sha256(filepath.read_text(encoding="utf-8"))
                except Exception:
                    continue
                old_hash = manifest.get(filename)
                if old_hash is None:
                    # 无基线：只有"内容与现行 seed 完全一致"（=未改动）才建基线，
                    # 后续升级才可能刷新它。不一致时无法区分"用户手改"与"旧版本
                    # 残留"——保守保留原样且不建基线，绝不静默覆盖用户的改动
                    # （宁可少刷新，也不丢用户的编辑）。审计 A6 的修复点在这里：
                    # 旧实现无条件跳过 → manifest 恒为 {} → seed 更新永不下发。
                    if current_hash == new_hash:
                        manifest[filename] = current_hash
                    else:
                        logger.info(
                            "插件 %s 与现行 seed 不一致且无基线：保留原样（不自动刷新）",
                            filename)
                    continue
                if current_hash == old_hash:
                    # 用户没改过旧 seed → 用新 seed 覆盖并更新 manifest
                    if current_hash != new_hash:
                        logger.info("刷新插件 seed: %s", filename)
                        _atomic_write(filepath, source)
                    manifest[filename] = new_hash
                # else: 用户改过了 → 跳过不动
            except Exception:
                logger.warning("seed 写入失败（跳过该文件）: %s", filename, exc_info=True)
        _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception:
        logger.warning("Failed to seed default plugins", exc_info=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(filepath: Path, content: str):
    """先写临时文件再 os.replace，避免崩溃留下半截文件。"""
    tmp_path = filepath.with_name(filepath.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, filepath)


def load_plugins(fallback_tools, fallback_dispatch, fallback_permissions):
    """
    Scan plugins/ for .py files exporting NAME, DEFINITION, PERMISSION, execute().
    Returns (tools, dispatch, permissions, hooks).
    Fallback definitions are merged in for any tool name the plugins don't provide.
    """
    _seed_default_plugins()

    plugins = []

    def _load_py(f: Path, tag: str):
        spec = importlib.util.spec_from_file_location(f"plugin_{tag}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not all(hasattr(mod, attr) for attr in ("NAME", "DEFINITION", "PERMISSION")):
            return None
        if not hasattr(mod, "execute") or not callable(mod.execute):
            return None
        return mod

    if PLUGINS_DIR.exists():
        for f in sorted(PLUGINS_DIR.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                mod = _load_py(f, f.stem)
                if mod is not None:
                    plugins.append(mod)
            except Exception:
                logger.warning("Failed to load plugin", exc_info=True)

    # ── 已安装扩展（~/.local-ai-os/extensions/<name>/<version>/plugin.py）──
    # 与内置插件同构加载；重名时先加载者赢（内置插件优先）。
    try:
        from extension_manager import active_extension_dirs
        for ext_dir in active_extension_dirs():
            f = ext_dir / "plugin.py"
            if not f.exists():
                continue
            try:
                mod = _load_py(f, f"{ext_dir.parent.name}_{ext_dir.name}")
                if mod is not None:
                    plugins.append(mod)
            except Exception:
                logger.warning("Failed to load extension plugin %s", f, exc_info=True)
    except Exception:
        logger.warning("Extension manager unavailable, skipping extension plugins", exc_info=True)

    tools = []
    dispatch = {}
    permissions = {}
    hooks = {}

    for mod in plugins:
        name = mod.NAME
        if name in dispatch:
            # 重名插件：先加载的赢，跳过后者
            logger.warning("Duplicate plugin NAME %r in %s — keeping the first one loaded", name, getattr(mod, "__file__", "?"))
            continue
        tools.append(mod.DEFINITION)
        dispatch[name] = mod.execute
        permissions[name] = mod.PERMISSION
        if hasattr(mod, "HOOKS") and isinstance(mod.HOOKS, dict):
            hooks[name] = mod.HOOKS

    # 合并 fallback：插件没有的工具名用 fallback 补齐（而不是有一个插件就全丢）
    for name, func in (fallback_dispatch or {}).items():
        if name not in dispatch:
            dispatch[name] = func
    for name, perm in (fallback_permissions or {}).items():
        if name not in permissions:
            permissions[name] = perm
    for tool_def in (fallback_tools or []):
        fname = tool_def.get("function", {}).get("name") if isinstance(tool_def, dict) else None
        if fname and fname in dispatch and not any(
            isinstance(t, dict) and t.get("function", {}).get("name") == fname for t in tools
        ):
            tools.append(tool_def)

    return tools, dispatch, permissions, hooks
