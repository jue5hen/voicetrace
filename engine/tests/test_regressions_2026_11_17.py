"""Regression tests for the 2026-11-17 audit findings (segmentation / cues / edit).

These pin the three defects that the old suite could not see:

* k >= 3 splitting duplicated text (the old fixtures were all comma sentences,
  so they only ever exercised k <= 2);
* the 900 ms minimum cue duration used to re-expand a cue after the 40 ms gap
  clamp, producing overlapping SRT entries;
* inserting characters into a short sentence produced zero-duration words and
  reversed cue order on export.
"""
from __future__ import annotations

import unittest

from voicetrace_engine import edit, export, segmentation, textutil


def make_sentence(sid: int, text: str, start: int = 0, step_ms: int = 200) -> dict:
    words = textutil.tokens_to_words(
        [(ch, (start + i * step_ms) / 1000, (start + (i + 1) * step_ms) / 1000)
         for i, ch in enumerate(text)],
        offset_ms=0,
        clip=None,
    )
    end = start + max(1, len(text)) * step_ms
    return {
        "id": sid, "start_ms": start, "end_ms": end, "text": text,
        "conf": 0.9, "words": words, "edited": False,
    }


def content_len(text: str) -> int:
    return len([ch for ch in text if textutil.is_content(ch)])


class SplitTextConservationTests(unittest.TestCase):
    """P0-2: a k >= 3 split must not re-slice from offset 0."""

    def test_long_unpunctuated_sentence_keeps_every_character_once(self):
        text = "我们今天来讲一讲如何在复杂的环境下做出正确的判断并且把这个判断落实到行动上然后再复盘两遍吧"
        self.assertGreaterEqual(len(text), 45, "must need k>=3 at max_chars=22")
        source = make_sentence(0, text, step_ms=180)
        pieces = segmentation.segment_sentence(source, max_chars=22, max_ms=6500)
        self.assertGreaterEqual(len(pieces), 3, "fixture must reach the k>=3 path")
        joined = "".join(p["text"] for p in pieces)
        self.assertEqual(content_len(joined), content_len(text),
                         "content must be conserved exactly once per character")
        for piece in pieces:
            self.assertLessEqual(content_len(piece["text"]), 22, piece["text"])

    def test_tight_max_chars_also_conserves_text(self):
        text = "这是一段没有任何标点的很长很长的口述内容需要被切成很多段来看它的字数是否守恒"
        source = make_sentence(0, text, step_ms=200)
        pieces = segmentation.segment_sentence(source, max_chars=10, max_ms=6500)
        self.assertGreaterEqual(len(pieces), 4)
        joined = "".join(p["text"] for p in pieces)
        self.assertEqual(content_len(joined), content_len(text))


class CueClampPolicyTests(unittest.TestCase):
    """P1-C: the 40 ms gap must win over the 900 ms minimum duration."""

    def test_gap_wins_when_cues_are_dense(self):
        end = segmentation.clamp_cue_end(start=0, end=900, chars=2, next_start=300)
        self.assertEqual(end, 300 - segmentation.GAP_MS)

    def test_gap_wins_even_below_old_hard_floor(self):
        # 句间距 <240ms：旧 200ms 硬底与 40ms 间隔数学无解，必然重叠。
        # 现在间隔绝对优先，只保证正时长（回归：实测重叠 194ms）。
        end = segmentation.clamp_cue_end(start=1000, end=1009, chars=9, next_start=1006)
        self.assertLess(end, 1006, "不得压住下一条")
        self.assertGreater(end, 1000, "必须正时长")

    def test_minimum_duration_applies_when_there_is_room(self):
        self.assertEqual(
            segmentation.clamp_cue_end(start=0, end=300, chars=1, next_start=940),
            segmentation.MIN_CUE_MS,
        )

    def test_reading_speed_floor_still_applies_without_next_cue(self):
        self.assertGreaterEqual(
            segmentation.clamp_cue_end(start=0, end=100, chars=20),
            int(20 * 1000 / segmentation.MAX_CPS),
        )


class CueExportTests(unittest.TestCase):
    """P1-C: exported cues never overlap and never exceed the media duration."""

    def test_dense_chunks_do_not_overlap(self):
        text = "我们今天就来讲一讲这个比较复杂的问题顺便把上次留下的那个尾巴也一起处理干净"
        transcript = {
            "sentences": [make_sentence(0, text, start=0, step_ms=60)],
            "media": {"duration_ms": 60000},
        }
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertGreaterEqual(len(cues), 2)
        for prev, nxt in zip(cues, cues[1:]):
            self.assertLessEqual(
                prev["end"], nxt["start"],
                f"cue overlap: {prev} -> {nxt}",
            )
        for cue in cues:
            self.assertGreater(cue["end"], cue["start"])

    def test_last_cue_is_clamped_to_media_duration(self):
        transcript = {
            "sentences": [make_sentence(0, "最后一句收尾", start=2000, step_ms=300)],
            "media": {"duration_ms": 3000},
        }
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertTrue(cues)
        self.assertLessEqual(cues[-1]["end"], 3000)


class EditInsertTimingTests(unittest.TestCase):
    """P1-C: typing a longer sentence must not collapse the timeline."""

    def setUp(self):
        self.sentence = make_sentence(0, "你好", start=400, step_ms=200)

    def test_inserted_characters_get_non_zero_duration(self):
        edit.set_sentence_text(self.sentence, "你好我们今天把这个长句子补完整再看看时间轴")
        words = self.sentence["words"]
        self.assertGreater(len(words), 10)
        zero = [w for w in words if w["end_ms"] <= w["start_ms"]]
        self.assertEqual(zero, [], "no word may have zero duration after an insert")

    def test_word_order_is_monotonic_and_end_extends(self):
        before = self.sentence["end_ms"]
        edit.set_sentence_text(self.sentence, "你好我们今天把时间轴补完整")
        words = self.sentence["words"]
        starts = [w["start_ms"] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertGreater(self.sentence["end_ms"], before)

    def test_srt_of_edited_sentence_is_ordered(self):
        edit.set_sentence_text(self.sentence, "你好我们今天把时间轴补完整")
        transcript = {"sentences": [self.sentence], "media": {"duration_ms": 60000}}
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        for prev, nxt in zip(cues, cues[1:]):
            self.assertLessEqual(prev["end"], nxt["start"])


if __name__ == "__main__":
    unittest.main()