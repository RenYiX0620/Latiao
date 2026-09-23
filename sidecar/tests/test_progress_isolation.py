"""进度按会话隔离（agent_loop）：切断唯一的跨会话提示词污染路径。

背景：所有会话共写一个 PROGRESS.md，注入时把它贴到**最后一条用户消息**尾部
（权重最高）→ B 会话能看到 A 会话（含 A 的子代理）的文件/命令输出，答非所问。
"""



import pytest


@pytest.fixture()
def A(monkeypatch, tmp_path):
    """把进度目录指到临时目录。

    注意两点：
    1. 用 monkeypatch 改模块常量，**不要** importlib.reload——重新导入会让别的
       测试模块手里的旧类引用和新模块身份不一致（全量跑时 test_thin_loop 出现
       AttributeError 就是 reload 造成的）。
    2. **规范模块是 `agent.progress`**（2026-09-23 拆分后进度读写搬到这里，
       agent_loop 只 re-export）。路径常量必须打规范模块——只改 agent_loop 的
       同名属性拦不住写入，会落到别处（拆分后首次跑全量就挂了 2 个用例）。
    """
    import agent_loop
    import agent.progress as progress
    for mod in (progress, agent_loop):      # 规范模块 + 旧路径同步，兼容两种读法
        monkeypatch.setattr(mod, "PROGRESS_DIR", tmp_path, raising=False)
        monkeypatch.setattr(mod, "PROGRESS_FILE", tmp_path / "PROGRESS.md", raising=False)
    yield agent_loop


def test_reexport_identity_holds():
    """re-export 必须指向同一个函数对象。

    拆分时用的是"搬走 + 旧位置 re-export"的模式；如果有人哪天在 agent_loop 里
    又复制一份同名实现，就会出现两份会各自漂移的代码（正是审计里"静默漂移"的
    成因）。这条断言把模式钉死。
    """
    import agent_loop
    import agent.progress as progress
    for name in ("_progress_file", "_record_progress", "_rotate_progress_file",
                 "_progress_tail", "_clean_progress_tail"):
        assert getattr(agent_loop, name) is getattr(progress, name), f"{name} 不是同一对象"


def test_session_files_are_separate(A):
    """两个会话写各自的文件：A 的记录不进 B 的文件。"""
    A._record_progress("**run_cmd**\nArgs: `{\"cmd\": \"ls A\"}`\nResult: A-only-data",
                       session_id="session_aaa")
    A._record_progress("**run_cmd**\nArgs: `{\"cmd\": \"ls B\"}`\nResult: B-only-data",
                       session_id="session_bbb")

    pa, pb = A._progress_file("session_aaa"), A._progress_file("session_bbb")
    assert pa != pb and pa.exists() and pb.exists()
    assert "A-only-data" in pa.read_text("utf-8")
    assert "A-only-data" not in pb.read_text("utf-8")
    assert "B-only-data" not in pa.read_text("utf-8")


def test_injection_only_reads_own_session(A):
    """注入尾巴只含本会话内容（旧实现读全局文件尾部，B 会看到 A 的日志）。"""
    A._record_progress("**run_cmd**\nArgs: `{}`\nResult: SECRET-FROM-A", session_id="sess_a")
    A._record_progress("**run_cmd**\nArgs: `{}`\nResult: B-own-line", session_id="sess_b")

    tail_b = A._progress_tail(session_id="sess_b")
    assert "B-own-line" in tail_b or tail_b == ""      # 工具日志块会被过滤掉
    assert "SECRET-FROM-A" not in tail_b


def test_unknown_session_gets_empty_tail(A):
    """全新会话：不注入任何别人的进展（这就是"新会话被带偏"的根因）。"""
    A._record_progress("**mx_query**\nArgs: `{}`\nResult: 半导体板块", session_id="sess_old")
    assert A._progress_tail(session_id="sess_brand_new") == ""


def test_no_session_id_falls_back_to_shared(A):
    """没有 session_id（cron/旧调用）：退回共享文件，行为同旧版。"""
    A._record_progress("**cron**\nArgs: `{}`\nResult: shared-line")   # 无 session_id
    assert A._progress_file() == A.PROGRESS_FILE
    assert A.PROGRESS_FILE.exists()


def test_session_id_sanitized(A):
    """会话 id 走白名单：路径穿越/怪字符不能逃出目录。"""
    p = A._progress_file("../../etc/passwd")
    assert p.parent == A.PROGRESS_DIR                      # 没逃出目录
    assert "/" not in p.name and "\\" not in p.name        # 只有单个文件名段
    assert p.resolve().parent == A.PROGRESS_DIR.resolve()  # 解析后仍在目录里
    assert A._progress_file("session_ok-1.2").name == "PROGRESS.session_ok-1.2.md"


def test_clean_progress_tail_drops_whole_tool_block(A):
    """整块丢弃工具日志：多行结果的续行不能漏给模型（旧实现按行过滤会漏）。"""
    text = (
        "### 2026-09-23T10:00:00\n"
        "**run_cmd**\nArgs: `{\"cmd\": \"ls\"}`\nResult: [\n"
        '  {"name": "secret-file.json", "size": 12},\n'
        '  {"name": "另一条续行"}\n'
        "]\n\n"
    )
    assert A._clean_progress_tail(text) == ""


def test_clean_progress_tail_keeps_human_note(A):
    """人类可读的进展（无工具标记）保留。"""
    text = "### 2026-09-23T10:00:00\n用户要求：把并发改好\n下一步：加测试\n"
    out = A._clean_progress_tail(text)
    assert "把并发改好" in out and "加测试" in out


def test_rotate_is_atomic_and_keeps_tail(A, monkeypatch):
    """轮转：保留尾部（最新），且不留下半截 UTF-8。"""
    p = A._progress_file("sess_rot")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(("x" * 100 + "\n").encode("utf-8") + "结尾记录\n".encode("utf-8")
                  + (b"y" * (1_100_000)))
    A._rotate_progress_file(p)
    data = p.read_bytes()
    assert len(data) < 1_100_000                       # 确实截短了
    data.decode("utf-8")                               # 仍是合法 UTF-8（不会 read_file 全拒）
    assert "结尾记录" in data.decode("utf-8", "replace") or len(data) <= 500 * 1024 + 64
    assert not p.with_suffix(p.suffix + ".tmp").exists()   # 临时文件已换名，不残留
