"""Regression tests for the 2026-11-18 engine text/export audit.

Each test pins one defect found while auditing
``voicetrace_engine/{segmentation,textutil,export,cleanup}.py``.  No GPU, model,
ffmpeg or network is needed: everything runs on synthetic transcripts.
"""
from __future__ import annotations

import unittest

from voicetrace_engine import cleanup, export, segmentation, textutil


def make_sentence(sid: int, text: str, start: int = 0, step_ms: int = 200) -> dict:
    words = []
    for i, ch in enumerate(text):
        if not textutil.is_content(ch):
            continue
        words.append({
            "text": ch,
            "start_ms": start + i * step_ms,
            "end_ms": start + (i + 1) * step_ms,
        })
    return {
        "id": sid,
        "start_ms": start,
        "end_ms": start + max(1, len(text)) * step_ms,
        "text": text,
        "conf": 0.9,
        "words": words,
        "edited": False,
    }


def content(text: str) -> str:
    return textutil.content_text(text)


class FillerWhitelistTests(unittest.TestCase):
    """Remove-fillers has three passes; all must honour the keep-word list."""

    def test_keep_words_at_clause_start_survive(self):
        for text in (
            "哎呀，这可怎么办。",
            "哎哟，好疼。",
            "唉呀，我忘了。",
            "诶呀，你来了。",
            "咦呀，真的吗？",
        ):
            self.assertEqual(textutil.remove_fillers(text), text)

    def test_keep_words_at_clause_end_survive(self):
        self.assertEqual(textutil.remove_fillers("天啊"), "天啊")
        self.assertEqual(textutil.remove_fillers("妈呀"), "妈呀")
        self.assertEqual(textutil.remove_fillers("咦呀，真的吗？"), "咦呀，真的吗？")

    def test_real_fillers_are_still_removed(self):
        self.assertEqual(textutil.remove_fillers("嗯，好"), "好")
        self.assertEqual(textutil.remove_fillers("唉，你看"), "你看")
        self.assertEqual(textutil.remove_fillers("这个啊，我们就这样"), "这个，我们就这样")


class AssEscapingTests(unittest.TestCase):
    """to_ass must escape override characters in the Dialogue text field."""

    def test_dialogue_text_escapes_braces_and_backslash(self):
        transcript = {
            "sentences": [make_sentence(0, "a{b}c\\d哦")],
            "media": {"name": "x{y}.mp4", "duration_ms": 5000},
        }
        out = export.to_ass(transcript, {})
        dialogue = [line for line in out.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogue), 1)
        self.assertTrue(dialogue[0].endswith(",,a\\{b\\}c\\\\d哦"), dialogue[0])
        self.assertNotIn("{b}", dialogue[0], "raw override braces reached ASS")
        self.assertIn("Title: x\\{y\\}.mp4", out)

    def test_plain_chinese_text_is_unchanged(self):
        transcript = {
            "sentences": [make_sentence(0, "你好世界")],
            "media": {"name": "demo.mp4", "duration_ms": 5000},
        }
        out = export.to_ass(transcript, {})
        dialogue = [line for line in out.splitlines() if line.startswith("Dialogue:")][0]
        self.assertTrue(dialogue.endswith(",,你好世界"), dialogue)


class EnglishWordTests(unittest.TestCase):
    """SPEC §3.2: English is one word per entry and never cut mid-word."""

    def test_words_are_latin_words_not_single_letters(self):
        text = ("This is a long English sentence without punctuation that "
                "should stay readable")
        pieces = segmentation.segment_sentence(
            make_sentence(0, text), max_chars=22, max_ms=6500
        )
        words = [w["text"] for piece in pieces for w in piece["words"]]
        for expected in ("This", "English", "sentence", "readable"):
            self.assertIn(expected, words)
        single_letters = [
            word for word in words
            if len(word) == 1 and word.isascii() and word.isalpha()
        ]
        self.assertEqual(single_letters, ["a"], words)
        for piece in pieces:
            self.assertEqual(
                "".join(w["text"] for w in piece["words"]),
                content(piece["text"]),
                piece["text"],
            )

    def test_split_prefers_a_word_boundary(self):
        pieces = segmentation.segment_sentence(
            make_sentence(0, "aa bbbbbbbb cc"), max_chars=10, max_ms=6500
        )
        self.assertGreaterEqual(len(pieces), 2)
        self.assertTrue(
            any("bbbbbbbb" in piece["text"] for piece in pieces),
            [piece["text"] for piece in pieces],
        )
        self.assertEqual(
            "".join(content(piece["text"]) for piece in pieces),
            content("aa bbbbbbbb cc"),
        )

    def test_merged_punctuation_units_keep_the_space(self):
        text = "Hello, world, how are you doing today, my friend and colleague"
        pieces = segmentation.segment_sentence(
            make_sentence(0, text, step_ms=120), max_chars=22, max_ms=6500
        )
        joined = "|".join(piece["text"] for piece in pieces)
        self.assertNotIn("Hello,world", joined)
        self.assertEqual(pieces[0]["text"], "Hello, world")
        # Content is conserved exactly once even though cue edges lose spaces.
        self.assertEqual(
            "".join(content(piece["text"]) for piece in pieces), content(text)
        )

    def test_merge_units_keeps_space_between_english_fragments(self):
        transcript = {
            "sentences": [make_sentence(0, "Hello", start=0),
                          make_sentence(1, "world", start=600)],
            "media": {"duration_ms": 60000},
        }
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertEqual([cue["text"] for cue in cues], ["Hello world"])

    def test_merge_units_still_joins_chinese_fragments_without_space(self):
        transcript = {
            "sentences": [make_sentence(0, "第", start=0),
                          make_sentence(1, "五十一讲", start=500)],
            "media": {"duration_ms": 60000},
        }
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertEqual([cue["text"] for cue in cues], ["第五十一讲"])


class CueClampTests(unittest.TestCase):
    def test_reading_speed_floor_rounds_up(self):
        # 20 chars at 8.5 cps need 2353 ms; int() used to return 2352 ms
        # (8.503 cps) and violate the documented MAX_CPS ceiling.
        end = segmentation.clamp_cue_end(start=0, end=1, chars=20)
        self.assertGreaterEqual(end, 2353)
        self.assertLessEqual(20 * 1000 / end, segmentation.MAX_CPS)


class LegitReduplicationTests(unittest.TestCase):
    """auto_correct is on by default: grammatical reduplication must survive."""

    def test_a_ab_verb_reduplication_is_kept(self):
        for text in (
            "理理发",  # 理发
            "唱唱歌",  # 唱歌
            "游游泳",  # 游泳
            "放放假",  # 放假
            "过过年",  # 过年
            "握握手",  # 握手
            "分分钟",  # 分钟
            "打打电话",  # 打电话
            "用用心",  # 用心
        ):
            self.assertEqual(cleanup.collapse_stutters(text), text)

    def test_numeral_classifier_reduplication_is_kept(self):
        for text in ("一个一个", "一条一条", "一份一份", "一天一天", "一遍一遍"):
            self.assertEqual(cleanup.collapse_stutters(text), text)

    def test_abab_verb_reduplication_is_kept(self):
        for text in ("研究研究", "琢磨琢磨", "打扮打扮", "高兴高兴", "热闹热闹"):
            self.assertEqual(cleanup.collapse_stutters(text), text)

    def test_real_stutters_are_still_collapsed(self):
        for text, expected in (
            ("老老师", "老师"),
            ("周周四", "周四"),
            ("我我觉得", "我觉得"),
            ("这个这个", "这个"),
            ("好像好像", "好像"),
            ("这个这个这个", "这个"),
            ("诶老老师你看嘛", "诶老师你看嘛"),
            ("我我觉得这个这个可以", "我觉得这个可以"),
        ):
            self.assertEqual(cleanup.collapse_stutters(text), expected)

    def test_cleaned_words_keep_a_positive_timeline(self):
        for text, expected in (
            ("我我觉得这个这个可以", "我觉得这个可以"),
            ("老老师讲的", "老师讲的"),
            ("这个这个这个", "这个"),
        ):
            out = cleanup.clean_sentence(make_sentence(0, text), {})
            self.assertEqual(out["text"], expected)
            self.assertEqual(
                "".join(word["text"] for word in out["words"]),
                content(expected),
            )
            starts = [word["start_ms"] for word in out["words"]]
            self.assertEqual(starts, sorted(starts))
            for word in out["words"]:
                self.assertGreater(
                    word["end_ms"], word["start_ms"],
                    "removed characters left a zero-duration word: %r" % (word,),
                )


if __name__ == "__main__":
    unittest.main()