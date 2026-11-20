"""Transcript text cleanup.

Two classes of noise make a transcript hard to read:

* spoken fillers (嗯 / 呃 / 诶 ...), and
* stutters, where the first syllable or the first word is repeated
  (老老师 / 周周四 / 这个这个 / 我我觉得).

Both are handled *before* subtitle segmentation, so the editor, the preview and
every exported format share the same text.  Word timings are realigned after
characters are removed, which keeps cues in sync with the audio.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .analysis import CJK_DIGITS, EXCLAMATIONS, LEGIT_REDUP, STUTTER_CHARS
from .textutil import MEASURE_CHARS, PREFIX_VERB_KEEP, is_content, remove_fillers

# Measure words: "个个人" means "each person", not a stutter.
_PREFIX_STOP = set("个位名次种些点条只件张座家间每各")

# Single-syllable verbs / time units whose repetition is lexical rather than a
# stutter: 理理发 (not 理发), 分分钟 (not 分钟).  Single source of truth lives in
# textutil so analysis (auto-correct) protects exactly the same set.
_PREFIX_VERB_KEEP = PREFIX_VERB_KEEP

# Numeral / 几 / 两 / 半 + classifier: 一个一个 / 一条一条 / 一份一份 is the
# "one by one" construction, not a stutter.  Checked structurally so new
# classifier words keep working without a giant hard-coded list.
_COUNT_CHARS = set(CJK_DIGITS) | set("十百千万亿几半") | set("0123456789")
_MEASURE_CHARS = MEASURE_CHARS

# ABAB verb reduplication (研究研究 / 收拾收拾 …) is protected by
# ``analysis.LEGIT_REDUP``.  These four are the ones the 2026-11-18 audit found
# missing; the synced list also has them now.  Keeping a local copy is a cheap
# guard against a future list regression and never deletes anything by itself.
_ABAB_REDUP_KEEP = frozenset({"琢磨", "打扮", "高兴", "热闹"})


def _is_word_reduplication(unit: str) -> bool:
    """True when a repeated unit is lexical (Numeral+classifier / ABAB)."""
    if unit in LEGIT_REDUP or unit in _ABAB_REDUP_KEEP:
        return True
    return (
        len(unit) == 2
        and (unit[0] in _COUNT_CHARS or unit[0].isdigit())
        and unit[1] in _MEASURE_CHARS
    )


def _is_prefix_reduplication(word: str) -> bool:
    """True for a single syllable whose double is a lexical reduplication."""
    return word in _PREFIX_VERB_KEEP or (word * 2) in LEGIT_REDUP


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def _stutter_unit_ok(unit: str) -> bool:
    if not unit or unit.isspace():
        return False
    if all(ch in EXCLAMATIONS for ch in unit):
        return False  # 哈哈哈 / 嗯嗯 is laughter or a filler, not a stutter
    if _is_word_reduplication(unit) or (
        len(unit) == 1 and (unit * 2) in LEGIT_REDUP
    ):
        return False
    if all(ch in CJK_DIGITS or ch.isdigit() for ch in unit):
        return False
    if len(unit) == 1:
        return unit in STUTTER_CHARS
    return True


def _tokens(text: str) -> list[tuple[str, int, int]] | None:
    try:
        import jieba  # type: ignore
        return [(w, s, e) for w, s, e in jieba.tokenize(text)]
    except Exception:  # pragma: no cover - release runtime ships jieba
        return None


def _collapse_regex(text: str) -> str:
    """Fallback when jieba is unavailable (conservative)."""

    def repl(match: "re.Match[str]") -> str:
        unit = match.group(1)
        return unit if _stutter_unit_ok(unit) else match.group(0)

    text = re.sub(
        r"([我你他她它这那就是在的和但而也都很不没会能要想说对好个们上下里外来去有无一])\1",
        r"\1",
        text,
    )
    return re.sub(r"([\u4e00-\u9fff]{2,4})\1", repl, text)


def collapse_stutters(text: str) -> str:
    """Collapse immediate syllable/word repetitions that read as stutters.

    老老师 -> 老师, 我我觉得 -> 我觉得, 这个这个 -> 这个.
    Legitimate reduplication (看看 / 妈妈 / 高高兴兴 / 研究研究) is preserved.
    """
    if not text:
        return ""
    tokens = _tokens(text)
    if not tokens:
        return _collapse_regex(text)

    kept: list[tuple[str, int, int]] = []
    name_of: dict[int, str] = {}
    kept_indexes: list[int] = []
    dropped = False
    for index, (word, start, end) in enumerate(tokens):
        following = tokens[index + 1][0] if index + 1 < len(tokens) else ""
        previous = kept[-1][0] if kept else ""
        # 老老师 -> 老师 (drop the isolated first syllable)
        if (
            len(word) == 1
            and _is_cjk(word)
            and word not in _PREFIX_STOP
            and not _is_prefix_reduplication(word)
            and len(following) > 1
            and following.startswith(word)
            and _is_cjk(following[0])
        ):
            dropped = True
            continue
        # 老师师 -> 老师 (drop the isolated trailing syllable)
        if (
            len(word) == 1
            and _is_cjk(word)
            and len(previous) >= 2
            and previous.endswith(word)
            and not all(ch in EXCLAMATIONS for ch in word)
        ):
            dropped = True
            continue
        # 我我 / 这个这个 / 这个这个这个 -> one copy
        if following == word and _stutter_unit_ok(word):
            dropped = True
            continue
        kept.append((word, start, end))
        name_of[index] = word
        kept_indexes.append(index)

    if not dropped:
        return text
    keep_set = set(kept_indexes)
    out: list[str] = []
    cursor = 0
    for index, (word, start, end) in enumerate(tokens):
        # Gaps between tokens hold punctuation/whitespace; keep them, but
        # never re-insert a token that was dropped as a stutter.
        if index in keep_set:
            if start > cursor:
                out.append(text[cursor:start])
            out.append(name_of[index])
            cursor = end
        else:
            if start > cursor:
                out.append(text[cursor:start])
            cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _flat_chars(sentence: dict) -> tuple[list[str], list[tuple[int, int]] | None, list[int] | None]:
    """Return ``(chars, times, word_index)`` for content characters.

    ``times`` / ``word_index`` are ``None`` when the stored words no longer
    describe the text (manual edit); timings are then rebuilt proportionally.
    """
    text = sentence.get("text") or ""
    chars = [ch for ch in text if is_content(ch)]
    flat_chars: list[str] = []
    times: list[tuple[int, int]] = []
    owners: list[int] = []
    for index, word in enumerate(sentence.get("words") or []):
        word_chars = [ch for ch in (word.get("text") or "") if is_content(ch)]
        if not word_chars:
            continue
        try:
            start = int(word.get("start_ms") or 0)
            end = max(start + 1, int(word.get("end_ms") or start + 1))
        except (TypeError, ValueError):
            start, end = 0, 1
        count = len(word_chars)
        for offset, ch in enumerate(word_chars):
            flat_chars.append(ch)
            times.append((start + (end - start) * offset // count,
                          start + (end - start) * (offset + 1) // count))
            owners.append(index)
    if flat_chars != chars:
        return chars, None, None
    return chars, times, owners


def _clean_text(text: str, settings: dict[str, Any]) -> str:
    out = text or ""
    if settings.get("subtitle_clean_stutter", True):
        out = collapse_stutters(out)
    if settings.get("subtitle_clean_fillers", True):
        out = remove_fillers(out)
    return out.strip()


def clean_sentence(sentence: dict, settings: dict[str, Any] | None = None) -> dict | None:
    """Clean one sentence; return ``None`` when nothing readable remains."""
    settings = settings or {}
    text = sentence.get("text") or ""
    cleaned = _clean_text(text, settings)
    if not cleaned:
        return None
    if cleaned == text:
        return sentence

    old_chars, times, owners = _flat_chars(sentence)
    new_chars = [ch for ch in cleaned if is_content(ch)]
    if not new_chars:
        return None

    words: list[dict] = []
    if times is not None and owners is not None:
        # Map each kept character back to the word it belonged to.
        matcher = difflib.SequenceMatcher(
            None, "".join(old_chars), "".join(new_chars), autojunk=False
        )
        kept_indexes: list[int] = []
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
            if tag == "equal":
                kept_indexes.extend(range(i1, i2))
        groups: list[list[int]] = []
        for old_index in kept_indexes:
            owner = owners[old_index]
            if groups and owners[groups[-1][0]] == owner:
                groups[-1].append(old_index)
            else:
                groups.append([old_index])
        for group in groups:
            start = times[group[0]][0]
            end = max(times[group[-1]][1], start + 1)
            words.append({
                "text": "".join(old_chars[i] for i in group),
                "start_ms": start,
                "end_ms": end,
            })
        if sum(len(w["text"]) for w in words) != len(new_chars):
            words = []  # alignment failed; fall back to proportional timings

    if not words:
        start = int(sentence.get("start_ms") or 0)
        end = max(start + 1, int(sentence.get("end_ms") or start + 1))
        count = len(new_chars)
        # 每字至少 1ms：interval 小于字数时旧的比例排布会产生 end<=start 的零时长词。
        span = max(end - start, count)
        words = [
            {"text": ch,
             "start_ms": start + span * i // count,
             "end_ms": start + span * (i + 1) // count}
            for i, ch in enumerate(new_chars)
        ]

    out = dict(sentence)
    out["text"] = cleaned
    out["words"] = words
    if words:
        out["end_ms"] = max(int(sentence.get("end_ms") or 0), words[-1]["end_ms"])
    return out


def clean_sentences(sentences: list[dict], settings: dict[str, Any] | None = None) -> list[dict]:
    """Clean every sentence, dropping cues that consisted only of fillers."""
    out: list[dict] = []
    for sentence in sentences:
        cleaned = clean_sentence(sentence, settings)
        if cleaned is not None:
            out.append(cleaned)
    for index, sentence in enumerate(out):
        sentence["id"] = index
    return out
