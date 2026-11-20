"""2026-11-19 稳定性审计回归。

P0-1：默认开启的 auto_correct 不得折叠合法「单音节动词重叠 / 时餐量词重叠」。
根因是 cleanup 有保护而 analysis 没有、且量词超集对 时/餐 已破。保护表提升到
textutil 单一真源后，这里钉住「cleanup 与 analysis 判定一致」。
"""

from __future__ import annotations

import shutil
import time
import unittest
import uuid
from pathlib import Path

from voicetrace_engine import analysis, cleanup, edit, segmentation, textutil
from voicetrace_engine import server as engine_server
from voicetrace_engine.config import DEFAULT_SETTINGS

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _sentence(text: str, sid: int = 0) -> dict:
    words = [{"text": ch, "start_ms": 1000 + i * 100, "end_ms": 1000 + (i + 1) * 100}
             for i, ch in enumerate(text) if ch.strip()]
    return {"id": sid, "text": text, "start_ms": 1000,
            "end_ms": 1000 + max(1, len(text)) * 100, "conf": 0.9,
            "words": words, "edited": False}


def _auto_correct_repeats(transcript: dict, settings: dict) -> int:
    """复刻 pipeline._auto_correct 的重复阶段（从后往前 apply_repeat）。"""
    issues = analysis.analyze(transcript, settings, include_lowconf=False)
    repeats = [i for i in issues if i.get("kind") == "repeat"]
    changed = 0
    by_sentence: dict[int, list[dict]] = {}
    for issue in repeats:
        by_sentence.setdefault(int(issue["sentence_id"]), []).append(issue)
    for sid, group in by_sentence.items():
        for issue in sorted(group, key=lambda i: int(i.get("start", 0)), reverse=True):
            try:
                r = edit.apply_repeat(transcript, sid, int(issue["start"]), int(issue["end"]))
                changed += int(r.get("changed", 0))
            except (KeyError, ValueError):
                continue
    return changed


class AutoCorrectKeepTests(unittest.TestCase):
    """cleanup 保住的合法重叠，auto_correct 不能再折叠（P0-1 复现用例）。"""

    KEEP = ["理理发", "打打电话", "用用心", "一餐一餐", "一时一时",
            "分分钟", "唱唱歌", "游游泳", "放放假", "过过年", "握握手",
            "一个一个", "一条一条", "一份一份", "看看", "琢磨琢磨"]

    def test_cleanup_then_auto_correct_preserves_text(self):
        settings = dict(DEFAULT_SETTINGS)
        for text in self.KEEP:
            with self.subTest(text=text):
                cleaned = cleanup.clean_sentences([_sentence(text)], settings)
                self.assertTrue(cleaned, f"{text} 不应被 cleanup 清空")
                self.assertEqual(cleaned[0]["text"], text, f"cleanup 误删 {text}")
                tr = {"sentences": [dict(cleaned[0])], "dismissed": []}
                _auto_correct_repeats(tr, settings)
                self.assertEqual(tr["sentences"][0]["text"], text,
                                 f"auto_correct 误删 {text}")

    def test_find_repeats_does_not_flag_lexical_redup(self):
        for text in ["理理发", "打打电话", "用用心", "一餐一餐", "一时一时"]:
            with self.subTest(text=text):
                reps = [i for i in analysis.find_repeats([_sentence(text)])
                        if i["kind"] == "repeat"]
                self.assertEqual(reps, [], f"{text} 不应被报 repeat")


class SharedTableInvariantTests(unittest.TestCase):
    def test_measure_chars_superset(self):
        self.assertTrue(
            textutil.MEASURE_CHARS <= analysis._QUANTIFIER_CLASSIFIERS,
            "analysis 分类词表必须是 textutil.MEASURE_CHARS 的超集，"
            f"缺：{''.join(sorted(textutil.MEASURE_CHARS - analysis._QUANTIFIER_CLASSIFIERS))}")

    def test_cleanup_shares_textutil_tables(self):
        self.assertIs(cleanup._PREFIX_VERB_KEEP, textutil.PREFIX_VERB_KEEP)
        self.assertIs(cleanup._MEASURE_CHARS, textutil.MEASURE_CHARS)

    def test_verb_keep_covers_audit_cases(self):
        for ch in "理打用分唱游放过握":
            self.assertIn(ch, textutil.PREFIX_VERB_KEEP)
        for ch in "时餐":
            self.assertIn(ch, textutil.MEASURE_CHARS)


class RealStutterStillCleanedTests(unittest.TestCase):
    def test_real_stutters_collapsed_by_cleanup(self):
        settings = dict(DEFAULT_SETTINGS)
        for text, want in [("老老师", "老师"), ("这个这个", "这个"), ("我我觉得", "我觉得")]:
            with self.subTest(text=text):
                cleaned = cleanup.clean_sentences([_sentence(text)], settings)
                self.assertEqual(cleaned[0]["text"], want)

    def test_cross_word_stutter_head_still_flagged(self):
        # 老 不在动词保护集，cross_word 仍应判口吃
        self.assertTrue(analysis._repeat_candidate("老", cross_word=True))
        self.assertFalse(analysis._repeat_candidate("理", cross_word=True))


class EditEnglishWordBoundaryTests(unittest.TestCase):
    """P1-2：编辑重对齐不得把 don't / well-known 从词中间切开（SPEC §3.2）。"""

    def _en_sentence(self, text: str) -> dict:
        words = [{"text": w, "start_ms": i * 200, "end_ms": (i + 1) * 200}
                 for i, w in enumerate(text.split())]
        return {"id": 0, "text": text, "start_ms": 0, "end_ms": len(words) * 200,
                "conf": 0.9, "words": words, "edited": False}

    def test_set_sentence_text_keeps_apostrophe_and_hyphen_words(self):
        s = self._en_sentence("I don't know")
        edit.set_sentence_text(s, "I don't know well-known")
        self.assertEqual([w["text"] for w in s["words"]],
                         ["I", "don't", "know", "well-known"])

    def test_edit_matches_segmentation_grouping(self):
        text = "I don't know well-known"
        s = self._en_sentence(text)
        edit.set_sentence_text(s, text)
        tr = {"sentences": [self._en_sentence(text)],
              "media": {"duration_ms": 100000}}
        segmentation.segment_transcript(tr, max_chars=48)
        self.assertEqual([w["text"] for w in s["words"]],
                         [w["text"] for w in tr["sentences"][0]["words"]])

    def test_cjk_still_one_word_per_char(self):
        s = _sentence("你好世界")
        edit.set_sentence_text(s, "你好世界")
        self.assertEqual([w["text"] for w in s["words"]], ["你", "好", "世", "界"])


def _scratch(tag: str) -> Path:
    p = (PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-1119"
         / f"{tag}-{uuid.uuid4().hex[:8]}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _rm_scratch(path: Path) -> None:
    target = Path(path).resolve()
    if PROJECT_ROOT in target.parents and "engine-1119" in target.parts:
        shutil.rmtree(target, ignore_errors=True)


class RetryKeepsTranscriptTests(unittest.TestCase):
    """P1-1：重试不清 has_transcript；重试再失败旧稿仍可加载。"""

    def setUp(self):
        self.tmp = _scratch("retry")
        data, models = self.tmp / "data", self.tmp / "models"
        bin_dir = self.tmp / "bin"
        for key in ("aed", "vad", "punc"):
            (models / key).mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        for exe in ("ffmpeg", "ffprobe"):
            (bin_dir / (exe + ".exe")).write_bytes(b"stub")
        args = engine_server.parse_args([
            "--token", "t", "--data-dir", str(data),
            "--models-dir", str(models), "--ffmpeg", str(bin_dir)])
        self.state = engine_server.State(args)

    def tearDown(self):
        try:
            self.state.store.close()
        except Exception:
            pass
        _rm_scratch(self.tmp)

    def test_retry_preserves_has_transcript_and_old_text(self):
        job = self.state.store.add_job(str(self.tmp / "clip.mp4"), "clip.mp4", 100)
        jid = job["id"]
        tr = {"job_id": jid, "version": 3, "seg_version": segmentation.SEG_VERSION,
              "media": {"name": "clip.mp4", "path": "clip.mp4", "duration_ms": 2000},
              "sentences": [_sentence("用户编辑过的句子")], "dismissed": [], "stats": {}}
        self.state.save_transcript(jid, tr)
        self.assertTrue(self.state.store.get_job(jid)["has_transcript"])

        row = self.state.retry_job(jid)
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["has_transcript"], "重试不应清掉 has_transcript")

        # 模拟重试再失败：旧稿仍可通过 load_transcript 取回
        self.state.store.update_job(jid, status="error", error="oom: 显存不足")
        self.assertTrue(self.state.store.get_job(jid)["has_transcript"])
        loaded = self.state.load_transcript(jid)
        self.assertEqual(loaded["sentences"][0]["text"], "用户编辑过的句子")

    def test_fresh_retry_clears_batch_override(self):
        job = self.state.store.add_job(str(self.tmp / "c.mp4"), "c.mp4", 100)
        jid = job["id"]
        self.state.pipeline.batch_overrides[jid] = 1
        self.state.retry_job(jid, fresh=True)
        self.assertNotIn(jid, self.state.pipeline.batch_overrides,
                         "fresh 重跑应清掉降级记忆，恢复默认批大小")


class TextUtilControlCharTests(unittest.TestCase):
    def test_control_and_format_chars_not_content(self):
        for ch in ["\x00", "\x07", "\x7f", "\u200d", "\ufeff", "\xad"]:
            self.assertFalse(textutil.is_content(ch), f"{ch!r} 不应算内容字")
        self.assertTrue(textutil.is_content("你"))
        self.assertEqual(textutil.count_chars("a\x00b\u200dc"), 3)


class LowConfTests(unittest.TestCase):
    def test_short_lowconf_reported_with_raw_indices(self):
        tr = {"sentences": [{"id": 0, "text": " 你好 ", "start_ms": 0, "end_ms": 1000,
                             "conf": 0.2, "words": [], "edited": False}]}
        lc = [i for i in analysis.analyze(tr, dict(DEFAULT_SETTINGS)) if i["kind"] == "lowconf"]
        self.assertEqual(len(lc), 1, "短句 conf<0.55 也应报 lowconf（无长度门槛）")
        self.assertEqual(lc[0]["end"], len(" 你好 "), "end 应为原始 text 长度")


class EditRobustnessTests(unittest.TestCase):
    def test_malformed_repeat_start_null_no_crash(self):
        tr = {"sentences": [_sentence("你好世界")], "dismissed": [], "stats": {},
              "media": {"duration_ms": 5000}}
        report = edit.apply_actions(tr, [{"type": "repeat", "sentence_id": 0,
                                          "start": None, "end": 2}], dict(DEFAULT_SETTINGS))
        self.assertTrue(any("error" in a for a in report["actions"]))

    def test_word_time_none_no_crash(self):
        s = _sentence("你好")
        s["words"] = [{"text": "你", "start_ms": None, "end_ms": None},
                      {"text": "好", "start_ms": 100, "end_ms": 200}]
        out = edit.rebuild_words("你好世界", s["words"], 0, 400)
        self.assertTrue(all(w["end_ms"] > w["start_ms"] for w in out))


class ZeroDurationFallbackTests(unittest.TestCase):
    def test_cleanup_proportional_fallback_positive(self):
        # 文本被清理（嗯 被删）且无 words → 走比例回退；9 字挤在 5ms，旧实现零时长
        s = {"id": 0, "text": "嗯甲乙丙丁戊己庚辛壬", "start_ms": 0, "end_ms": 5,
             "conf": 0.9, "words": [], "edited": False}
        cleaned = cleanup.clean_sentence(s, dict(DEFAULT_SETTINGS))
        self.assertIsNotNone(cleaned)
        self.assertTrue(cleaned["words"])
        self.assertTrue(all(w["end_ms"] > w["start_ms"] for w in cleaned["words"]))

    def test_edit_insert_no_zero_duration(self):
        s = _sentence("甲乙")
        edit.set_sentence_text(s, "甲乙丙丁戊己庚辛壬癸")
        self.assertTrue(all(w["end_ms"] > w["start_ms"] for w in s["words"]))


class ExportContentTests(unittest.TestCase):
    def test_export_content_matches_textutil(self):
        from voicetrace_engine import export
        for ch in ["😀", "©", "—", "·", "%", "你"]:
            self.assertEqual(export._content_of(ch), ch if textutil.is_content(ch) else "", ch)

    def test_reserved_device_names_include_conin_conout(self):
        self.assertTrue(engine_server.is_reserved_device_name("CONIN$"))
        self.assertTrue(engine_server.is_reserved_device_name("CONOUT$.srt"))
        self.assertTrue(engine_server.is_reserved_device_name("CON"))
        self.assertFalse(engine_server.is_reserved_device_name("正常.srt"))


if __name__ == "__main__":
    unittest.main()