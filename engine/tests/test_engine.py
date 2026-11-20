"""Unit tests for the deterministic (non-model) engine layers.

Run:  .venv\Scripts\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import unittest

from voicetrace_engine import analysis, edit, export, textutil


def sentence(sid: int, text: str, start: int = 0, conf: float = 0.9) -> dict:
    words = textutil.tokens_to_words([(ch, start / 1000, start / 1000 + 0.2) for ch in text],
                                     offset_ms=0, clip=None)
    return {"id": sid, "start_ms": start, "end_ms": start + 2000, "text": text,
            "conf": conf, "words": words, "edited": False}


def transcript(*texts: str) -> dict:
    sents = [sentence(i, t, i * 2000) for i, t in enumerate(texts)]
    return {"job_id": "t", "version": 1, "media": {"duration_ms": len(texts) * 2000},
            "sentences": sents, "dismissed": [], "stats": {}}


class RepeatTests(unittest.TestCase):
    def test_stutter_pair(self):
        t = transcript("好像好像是这样的，我我觉得我们可以开始了。")
        issues = analysis.find_repeats(t["sentences"])
        spans = {(i["start"], i["end"], i["suggestion"]) for i in issues}
        self.assertIn((0, 4, "好像"), spans)
        self.assertIn((9, 11, "我"), spans)

    def test_legit_reduplication_ignored(self):
        for word in ("看看", "慢慢", "说说", "谢谢", "高高兴兴"):
            self.assertEqual(analysis.find_repeats([sentence(0, word)]), [], word)

    def test_triple_repeat_unit(self):
        t = transcript("这个这个这个真好")
        issues = analysis.find_repeats(t["sentences"])
        self.assertTrue(issues)
        self.assertEqual(issues[0]["suggestion"], "这个")
        self.assertEqual(issues[0]["text"], "这个这个这个")

    def test_prefix_syllable_stutter(self):
        t = transcript("诶老老师你看嘛，周周四再说。")
        spans = {(i["text"], i["suggestion"]) for i in analysis.find_repeats(t["sentences"])}
        self.assertIn(("老老", "老"), spans)
        self.assertIn(("周周", "周"), spans)

    def test_reduplicated_idioms_ignored(self):
        for text in ("高高兴兴", "干干净净", "小小年纪", "天天向上", "一一列举"):
            self.assertEqual(analysis.find_repeats([sentence(0, text)]), [], text)

    def test_apply_repeat_uses_minimal_unit(self):
        t = transcript("好像好像是这样")
        edit.apply_actions(t, [{"type": "repeat", "sentence_id": 0, "start": 0, "end": 4}], {})
        self.assertEqual(t["sentences"][0]["text"], "好像是这样")


class SuspiciousTests(unittest.TestCase):
    def test_dominant_homophone_flagged(self):
        texts = ["胜兵者先胜而后求战。"] + [f"胜兵的意思是先胜后战。"] * 9 + ["我们来讲圣兵这本书。"]
        issues = analysis.find_suspicious(transcript(*texts)["sentences"], 3)
        self.assertEqual(len(issues), 1, issues)
        self.assertEqual(issues[0]["suggestion"], "胜兵")
        self.assertEqual(issues[0]["text"], "圣兵")
        self.assertIn("同音异写", issues[0]["reason"])

    def test_balanced_variants_not_flagged(self):
        texts = ["胜兵先胜。"] * 4 + ["圣兵先胜。"] * 4
        self.assertEqual(analysis.find_suspicious(transcript(*texts)["sentences"], 3), [])

    def test_similar_but_not_homophone_ignored(self):
        texts = ["时候到了。"] * 8 + ["事后再说。"]
        self.assertEqual(analysis.find_suspicious(transcript(*texts)["sentences"], 3), [])


class EditTests(unittest.TestCase):
    def test_multiple_spans_apply_right_to_left(self):
        t = transcript("好像好像是这样的，我我觉得我们可以开始了。")
        edit.apply_actions(t, [
            {"type": "repeat", "sentence_id": 0, "start": 0, "end": 4},
            {"type": "repeat", "sentence_id": 0, "start": 9, "end": 11},
        ], {})
        self.assertEqual(t["sentences"][0]["text"], "好像是这样的，我觉得我们可以开始了。")

    def test_words_stay_in_sync(self):
        t = transcript("胜兵者，先胜而后求战。")
        edit.apply_actions(t, [{"type": "replace", "from": "胜兵", "to": "圣兵", "scope": "selected",
                                "sentence_ids": [0]}], {})
        s = t["sentences"][0]
        plain = "".join(ch for ch in s["text"] if ch in textutil.content_text(s["text"]) and ch not in "，。！？；：、,.!?;: ")
        self.assertEqual("".join(w["text"] for w in s["words"]), plain)
        self.assertTrue(s["edited"])

    def test_set_text_and_replace(self):
        t = transcript("甲说胜兵。", "乙说圣兵。")
        edit.apply_actions(t, [{"type": "set_text", "sentence_id": 0, "text": "甲说完了。"}], {})
        self.assertEqual(t["sentences"][0]["text"], "甲说完了。")
        r = edit.apply_replace(t, "圣兵", "胜兵", "selected", [1])
        self.assertEqual(r["changed"], 1)
        self.assertEqual(t["sentences"][1]["text"], "乙说胜兵。")
        self.assertEqual(edit.apply_replace(t, "圣兵", "胜兵", "selected", [0])["changed"], 0)

    def test_empty_replace_rejected(self):
        with self.assertRaises(ValueError):
            edit.apply_replace(transcript("x"), "", "y")


class ExportTests(unittest.TestCase):
    def test_srt_cues(self):
        t = transcript("第一句话。第二句话比较长，需要切成多条字幕来展示。")
        out = export.export(t, "srt", {"subtitle_max_chars": 12, "max_lines": 1})
        self.assertTrue(out.startswith("1\n00:00:00,000 -->"))
        self.assertNotIn("None", out)

    def test_json_roundtrip(self):
        t = transcript("你好。")
        out = export.export(t, "json", {})
        self.assertEqual(json.loads(out)["sentences"][0]["text"], "你好。")


if __name__ == "__main__":
    unittest.main()
