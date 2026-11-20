"""Subtitle segmentation and non-destructive cleanup tests."""
from __future__ import annotations

import re
import unittest

from voicetrace_engine import segmentation, textutil


def make_sentence(sid: int, text: str, start: int = 0, step_ms: int = 200) -> dict:
    words = textutil.tokens_to_words(
        [(ch, (start + i * step_ms) / 1000, (start + (i + 1) * step_ms) / 1000)
         for i, ch in enumerate(text)],
        offset_ms=0,
        clip=None,
    )
    end = start + max(1, len(text)) * step_ms
    return {
        "id": sid,
        "start_ms": start,
        "end_ms": end,
        "text": text,
        "conf": 0.9,
        "words": words,
        "edited": False,
    }


def content_len(text: str) -> int:
    return len([ch for ch in text if textutil.is_content(ch)])


class CleanupTests(unittest.TestCase):
    def test_standalone_fillers_removed_but_words_kept(self):
        self.assertEqual(
            textutil.remove_fillers("嗯，这个啊，我们就，。、这样"),
            "这个，我们就，。、这样",
        )
        self.assertEqual(
            textutil.remove_fillers("天啊，啊哈，这个嗯嗯很好"),
            "天啊，啊哈，这个嗯嗯很好",
        )

    def test_punctuation_to_space(self):
        self.assertEqual(
            textutil.punctuation_to_space("你好，世界。测试、一下"),
            "你好 世界 测试 一下",
        )

    def test_clean_subtitle_text_combines_both_switches(self):
        self.assertEqual(
            textutil.clean_subtitle_text("嗯，这个啊，我们就，。、这样。天啊，啊哈。"),
            "这个 我们就 这样 天啊 啊哈",
        )
        self.assertEqual(
            textutil.clean_subtitle_text(
                "嗯，这个啊。",
                {"subtitle_clean_fillers": False, "subtitle_punct_space": False},
            ),
            "嗯，这个啊。",
        )


class SegmentationTests(unittest.TestCase):
    def setUp(self):
        self.text = (
            "这个啊，我们今天来讲一讲，如何在复杂的环境下，做出正确的判断，"
            "并且把这个判断落实到行动上。"
        )
        self.source = make_sentence(0, self.text)

    def test_pieces_respect_limits_and_keep_text(self):
        pieces = segmentation.segment_sentence(self.source, max_chars=22, max_ms=6500)
        self.assertGreater(len(pieces), 1)
        previous_end = -1
        for piece in pieces:
            self.assertLessEqual(content_len(piece["text"]), 22, piece["text"])
            self.assertLessEqual(piece["end_ms"] - piece["start_ms"], 6500)
            self.assertGreaterEqual(piece["start_ms"], previous_end)
            previous_end = piece["end_ms"]
        joined = "".join(piece["text"] for piece in pieces)
        self.assertEqual(content_len(joined), content_len(self.text))

    def test_transcript_updates_ids_stats_and_version(self):
        transcript = {
            "sentences": [
                make_sentence(0, self.text),
                make_sentence(1, "这是第二句，也应该保留。", start=12000),
            ],
            "stats": {},
        }
        changed = segmentation.segment_transcript(transcript, max_chars=22, force=True)
        self.assertTrue(changed)
        self.assertEqual(transcript["seg_version"], segmentation.SEG_VERSION)
        self.assertEqual(transcript["stats"]["sentences"], len(transcript["sentences"]))
        joined = "".join(s["text"] for s in transcript["sentences"])
        # Trailing soft punctuation is trimmed per cue, content never is.
        self.assertEqual(
            content_len(joined),
            content_len(self.text) + content_len("这是第二句，也应该保留。"),
        )
        # SPEC §3.2: content characters only (the old len(joined) counted the
        # punctuation this assertion is surrounded by).
        self.assertEqual(transcript["stats"]["chars"], textutil.count_chars(joined))
        self.assertFalse(joined.endswith("，"))
        self.assertEqual(
            [sentence["id"] for sentence in transcript["sentences"]],
            list(range(len(transcript["sentences"]))),
        )


class CueTextTests(unittest.TestCase):
    def test_tidy_cue_text(self):
        self.assertEqual(textutil.tidy_cue_text("，这个，。 测试 ，"), "这个。测试")
        self.assertEqual(textutil.tidy_cue_text("好，。"), "好。")
        self.assertEqual(textutil.tidy_cue_text("，。 "), "")
        self.assertEqual(textutil.tidy_cue_text("这个， ，我们"), "这个，我们")

    def test_segments_never_end_with_soft_punctuation(self):
        source = make_sentence(0, "，嗯，这个啊，我们今天来讲一讲，如何做好判断。")
        pieces = segmentation.segment_sentence(source, max_chars=14)
        self.assertTrue(pieces)
        for piece in pieces:
            self.assertFalse(piece["text"].endswith(("，", ",", "、")), piece["text"])
            self.assertFalse(piece["text"].startswith(("，", ",", "、")), piece["text"])

    def test_punctuation_only_sentence_is_dropped(self):
        transcript = {"sentences": [make_sentence(0, "，。")], "stats": {}}
        self.assertTrue(segmentation.segment_transcript(transcript, force=True))
        self.assertEqual(transcript["sentences"], [])


class CueExportTests(unittest.TestCase):
    def test_build_cues_cleans_and_limits_speed(self):
        text = "嗯，这个啊，我们今天来讲一讲，如何在复杂的环境下，做出正确的判断。"
        source = make_sentence(0, text)
        transcript = {
            "seg_version": segmentation.SEG_VERSION,
            "sentences": [source],
            "stats": {},
        }
        cues = segmentation.segment_transcript(transcript, max_chars=22, force=True)
        self.assertTrue(cues)
        from voicetrace_engine import export

        built = export.build_cues(
            transcript,
            max_chars=22,
            max_lines=2,
            settings={"subtitle_clean_fillers": True, "subtitle_punct_space": True},
        )
        self.assertTrue(built)
        for cue in built:
            visible = cue["text"].replace("\n", "")
            self.assertNotIn("，", visible)
            self.assertNotIn("。", visible)
            duration = max(1, cue["end"] - cue["start"]) / 1000
            self.assertLessEqual(len(visible) / duration, 8.6, visible)

        srt = export.to_srt(
            transcript,
            settings={"subtitle_clean_fillers": True, "subtitle_punct_space": True},
        )
        self.assertNotIn("嗯", srt)
        self.assertNotIn("，", srt)
        self.assertRegex(srt, r"\d{2}:\d{2}:\d{2},\d{3} --> ")


if __name__ == "__main__":
    unittest.main()
