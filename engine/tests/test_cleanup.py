"""Fillers and stutters are removed before subtitles are segmented."""
from __future__ import annotations

import unittest

from voicetrace_engine import cleanup, textutil


def make_sentence(sid: int, text: str, start: int = 0, step_ms: int = 200) -> dict:
    words = textutil.tokens_to_words(
        [(ch, (start + i * step_ms) / 1000, (start + (i + 1) * step_ms) / 1000)
         for i, ch in enumerate(text)],
        offset_ms=0,
        clip=None,
    )
    return {
        "id": sid,
        "start_ms": start,
        "end_ms": start + max(1, len(text)) * step_ms,
        "text": text,
        "conf": 0.9,
        "words": words,
        "edited": False,
    }


class CollapseStuttersTests(unittest.TestCase):
    def test_prefix_syllable(self):
        self.assertEqual(cleanup.collapse_stutters("诶老老师你看嘛"), "诶老师你看嘛")
        self.assertEqual(cleanup.collapse_stutters("周周四和吴周五"), "周四和吴周五")

    def test_duplicate_word(self):
        self.assertEqual(cleanup.collapse_stutters("我我觉得这个这个可以"), "我觉得这个可以")
        self.assertEqual(cleanup.collapse_stutters("这个这个这个"), "这个")

    def test_legitimate_reduplication_kept(self):
        for text in (
            "高高兴兴", "干干净净", "小小年纪", "天天向上", "一一列举",
            "看看这个", "妈妈来了", "研究研究", "呵呵", "个个人都",
        ):
            self.assertEqual(cleanup.collapse_stutters(text), text)

    def test_punctuation_is_preserved(self):
        self.assertEqual(cleanup.collapse_stutters("我我，觉得"), "我，觉得")


class CleanSentenceTests(unittest.TestCase):
    def test_fillers_and_stutters_removed(self):
        sentence = make_sentence(0, "嗯，我我觉得这个啊可以")
        out = cleanup.clean_sentence(sentence, {})
        self.assertEqual(out["text"], "我觉得这个啊可以")
        self.assertTrue(out["words"])
        self.assertLessEqual(out["words"][0]["start_ms"], out["words"][-1]["end_ms"])
        for word in out["words"]:
            self.assertLessEqual(word["start_ms"], word["end_ms"])

    def test_switches_off_keep_text(self):
        sentence = make_sentence(0, "嗯，我我觉得可以")
        out = cleanup.clean_sentence(
            sentence,
            {"subtitle_clean_fillers": False, "subtitle_clean_stutter": False},
        )
        self.assertEqual(out["text"], "嗯，我我觉得可以")

    def test_sentence_of_punctuation_is_dropped(self):
        self.assertIsNone(cleanup.clean_sentence(make_sentence(0, "嗯，啊。"), {}))

    def test_reindex_after_drop(self):
        out = cleanup.clean_sentences(
            [make_sentence(0, "嗯。"), make_sentence(1, "我我觉得可以。")], {}
        )
        self.assertEqual([s["id"] for s in out], [0])
        self.assertEqual(out[0]["text"], "我觉得可以。")


if __name__ == "__main__":
    unittest.main()
