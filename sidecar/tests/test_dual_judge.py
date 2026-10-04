"""dual_judge.py 的封闭验收：一致率计算、评委故障容错、提示词构造、JSON 提取。

不联网：judge_fn 注入假评委；extract 用脚本内副本（该副本与
sidecar/agent/parsing._extract_first_json_object 是同一手法的两份实现，
两边都要有自己的回归）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import dual_judge as dj  # noqa: E402


class TestExtractCopy(unittest.TestCase):
    def test_think_block_and_prose(self):
        s = '</think>\n好的：{"pass": true, "score": 90, "reason": "一致"}'
        self.assertTrue(dj.extract_first_json_object(s)["pass"])

    def test_brace_inside_string(self):
        s = '{"reason": "含{花括号}的引用"}'
        self.assertIn("花括号", dj.extract_first_json_object(s)["reason"])


class TestBuildJudgeMessages(unittest.TestCase):
    def test_roles_and_material(self):
        msgs = dj.build_judge_messages(
            {"question": "上证指数今天多少点？", "answer": "3100 点", "reference": "3100.5"},
            "规则一")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("评分规则", msgs[0]["content"])
        self.assertIn("规则一", msgs[0]["content"])
        self.assertIn('{"pass"', msgs[0]["content"])  # 输出格式约束在 system 里
        user = msgs[1]["content"]
        self.assertIn("上证指数今天多少点", user)
        self.assertIn("3100.5", user)
        self.assertIn("3100 点", user)

    def test_no_reference_omits_line(self):
        msgs = dj.build_judge_messages({"question": "q", "answer": "a"}, "r")
        self.assertNotIn("参考答案", msgs[1]["content"])


def _always(pass_, score):
    def fn(cfg, item, rubric):
        return {"pass": pass_, "score": score, "reason": "fake"}
    return fn


def _explode(cfg, item, rubric):
    raise RuntimeError("boom")


JUDGES = [("judge1", {"model": "a"}), ("judge2", {"model": "b"})]
ITEMS = [{"id": str(i), "question": "q", "answer": "a"} for i in range(4)]


class TestScoreAgreement(unittest.TestCase):
    def test_full_agreement(self):
        out = dj.score(ITEMS, JUDGES, "r", judge_fn=_always(True, 90))
        self.assertEqual(out["summary"]["agreement_rate"], 1.0)
        self.assertEqual(out["summary"]["n_scored_both"], 4)
        self.assertEqual(out["summary"]["pass_rate"]["judge1"], 1.0)

    def test_full_disagreement(self):
        def fn(cfg, item, rubric):
            return (_always(True, 90)(cfg, item, rubric)
                    if cfg["model"] == "a" else _always(False, 20)(cfg, item, rubric))
        out = dj.score(ITEMS, JUDGES, "r", judge_fn=fn)
        self.assertEqual(out["summary"]["agreement_rate"], 0.0)
        self.assertEqual(len(out["disagreements"]), 4)

    def test_judge_failure_excluded_from_agreement(self):
        # 评委1 挂掉 → 该条不计入一致率，但不拖垮整批
        calls = {"n": 0}
        def fn(cfg, item, rubric):
            if cfg["model"] == "a":
                calls["n"] += 1
                raise RuntimeError("boom")
            return {"pass": True, "score": 80, "reason": "ok"}
        out = dj.score(ITEMS, JUDGES, "r", judge_fn=fn)
        self.assertEqual(out["summary"]["n_scored_both"], 0)
        self.assertIsNone(out["summary"]["agreement_rate"])
        self.assertEqual(calls["n"], 4)

    def test_score_delta(self):
        def fn(cfg, item, rubric):
            return (_always(True, 90)(cfg, item, rubric) if cfg["model"] == "a"
                    else _always(True, 70)(cfg, item, rubric))
        out = dj.score(ITEMS, JUDGES, "r", judge_fn=fn)
        self.assertEqual(out["summary"]["mean_abs_score_delta"], 20.0)


class TestJudgeItemNormalization(unittest.TestCase):
    def test_score_clamped_and_reason_truncated(self):
        def call_fn(cfg, messages, timeout=60):
            return {"pass": 1, "score": "250", "reason": "长" * 500}
        out = dj.judge_item({"model": "m"}, {"question": "q", "answer": "a"}, "r",
                            call_fn=call_fn)
        self.assertIs(out["pass"], True)
        self.assertEqual(out["score"], 100)
        self.assertLessEqual(len(out["reason"]), 200)

    def test_extractor_handles_fence_wrapped_output(self):
        # 评委无视"只输出 JSON"的指令、用围栏+废话包裹时，提取层（call_judge 内）也能解析
        raw = ('评委意见如下：\n```json\n'
               '{"pass": false, "score": 10, "reason": "编造"}\n```')
        out = dj.extract_first_json_object(raw)
        self.assertIs(out["pass"], False)
        self.assertEqual(out["score"], 10)


class TestLoadItems(unittest.TestCase):
    def test_rejects_missing_answer(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            f.write('{"question": "q", "answer": "a"}\n')
            f.write('{"question": "q2"}\n')
            path = f.name
        with self.assertRaises(SystemExit):
            dj.load_items(path)
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
