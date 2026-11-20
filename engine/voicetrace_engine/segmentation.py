"""Semantic subtitle segmentation.

The ASR punctuation pass merges neighbouring clauses into long sentences (up to
64 characters).  A subtitle cue must stay readable, so this module cuts those
sentences on punctuation, pauses and jieba word boundaries, then assigns each
piece the word/character timestamps it actually covers.

The output is intentionally conservative:

* <= ``max_chars`` content characters per cue;
* <= ``max_ms`` milliseconds of speech per cue;
* a short cue is never stretched past the next cue (40 ms guard);
* reading speed is kept at or below ``MAX_CPS`` characters/second.

It is used for new transcripts (pipeline), one-off migration of old transcripts
and by the export layer.  Keeping a single implementation avoids the editor
and the exported SRT drifting apart.
"""

from __future__ import annotations

import math
from typing import Iterable

from .textutil import (
    ASCII_DIGITS,
    ASCII_LETTERS,
    content_text,
    count_chars,
    is_content,
    safe_int,
    tidy_cue_text,
    word_ends,
)

SEG_VERSION = 2  # v2: tidy cue punctuation + configurable pause merge
DEFAULT_MAX_CHARS = 22
DEFAULT_MAX_MS = 6500
MAX_CPS = 8.5
MIN_CUE_MS = 900
GAP_MS = 40
PAUSE_MERGE_MS = 900


def clamp_cue_end(
    *,
    start: int,
    end: int,
    chars: int,
    next_start: int | None = None,
    max_ms: int | None = None,
) -> int:
    """把语速下限、最短时长、条间间隔、最长时长四件事收在一处。

    顺序很重要：相对下一条 cue 的 40 ms 间隔**绝对优先**，900 ms 最短时长与
    语速下限都让位于它。旧实现保留 200 ms 硬底，句间距 <240 ms 时硬底与间隔
    数学无解，必然重叠（实测重叠 194 ms，违 SPEC §7）。现在密集语流里允许
    本条极短，只保证「正时长 + 绝不压住下一条」。
    """
    # ceil, not int: truncating the required duration makes the cue a hair too
    # short (20 chars -> 2352 ms = 8.503 cps, above MAX_CPS).
    end = max(end, start + math.ceil(chars * 1000 / MAX_CPS))
    end = max(end, start + MIN_CUE_MS)
    if next_start is not None:
        end = min(end, next_start - GAP_MS)
    if max_ms:
        end = min(end, start + max_ms)
    return max(end, start + 1)

_BREAK_CHARS = set("，。！？；：、,.!?;:…—")
_LEFT_BINDING = set("的了着过吗呢吧啊呀嘛")
_TRAILING_PUNCT = set("，。！？；：、,.!?;:…")


def _char_times(sentence: dict) -> list[tuple[str, int, int]]:
    """Return ``(char, start_ms, end_ms)`` for every content character.

    Words are normally character-level, but old/edited transcripts may contain
    multi-character words.  In that case the word interval is distributed over
    its characters.  A final proportional fallback keeps timings usable even
    when words are missing or no longer match the edited text.
    """
    text = sentence.get("text") or ""
    chars = [ch for ch in text if is_content(ch)]
    if not chars:
        return []

    start = safe_int(sentence.get("start_ms"))
    end = max(start + 1, safe_int(sentence.get("end_ms"), start + 1))
    words = sentence.get("words") or []
    flat: list[tuple[str, int, int]] = []
    for word in words:
        word_text = word.get("text") or ""
        word_chars = [ch for ch in word_text if is_content(ch)]
        if not word_chars:
            continue
        ws = safe_int(word.get("start_ms"), start)
        we = max(ws + 1, safe_int(word.get("end_ms"), ws + 1))
        n = len(word_chars)
        for i, ch in enumerate(word_chars):
            flat.append((ch, ws + (we - ws) * i // n, ws + (we - ws) * (i + 1) // n))

    if len(flat) == len(chars) and "".join(c[0] for c in flat) == "".join(chars):
        return flat

    # Text and word timings no longer line up (manual edit): spread the
    # sentence interval proportionally. 每字至少 1ms，禁止零时长 cue。
    n = len(chars)
    span = max(end - start, n)
    return [
        (ch, start + span * i // n, start + span * (i + 1) // n)
        for i, ch in enumerate(chars)
    ]


def _unit_spans(text: str) -> list[tuple[int, int, int, int]]:
    """Split raw text into punctuation units.

    Returns ``(text_start, text_end, content_start, content_end)`` offsets.
    Punctuation stays attached to the unit it terminates.
    """
    spans: list[tuple[int, int, int, int]] = []
    text_start = 0
    content_start = 0
    content_count = 0
    for i, ch in enumerate(text):
        if is_content(ch):
            content_count += 1
        elif ch in _BREAK_CHARS and content_count > content_start:
            spans.append((text_start, i + 1, content_start, content_count))
            text_start = i + 1
            content_start = content_count
    if content_count > content_start:
        spans.append((text_start, len(text), content_start, content_count))
    return spans


def _piece(text: str, c0: int, c1: int, ctimes: list[tuple[str, int, int]]) -> dict:
    # Keep the raw slice: the whitespace between two English words is not a
    # content character, so stripping every piece and concatenating later fused
    # words together ("Hello," + " world" -> "Hello,world").  Cue edges are
    # cleaned by tidy_cue_text() at the very end.
    return {
        "text": text,
        "c0": c0,
        "c1": c1,
        "chars": c1 - c0,
        "start": ctimes[c0][1],
        "end": ctimes[c1 - 1][2],
    }


def _content_offsets(text: str) -> list[int]:
    return [i for i, ch in enumerate(text) if is_content(ch)]


# Characters that may sit *inside* one latin word (don't -> don't).
_WORD_JOINERS = set("'’-")


def _is_word_char(ch: str) -> bool:
    return ch in ASCII_LETTERS or ch in ASCII_DIGITS


def _inside_word(text: str, offsets: list[int], pos: int) -> bool:
    """True when the gap before content index ``pos`` glues two word chars.

    Used for two things: keeping English words in one piece when splitting,
    and emitting one ``words`` entry per English word (SPEC §3.2: 英文按单词,
    not one letter per entry).  CJK characters are never word chars, so they
    keep the historical one-character-per-word shape.
    """
    if pos <= 0 or pos >= len(offsets):
        return False
    if not (_is_word_char(text[offsets[pos - 1]]) and _is_word_char(text[offsets[pos]])):
        return False
    gap = text[offsets[pos - 1] + 1:offsets[pos]]
    return all(ch in _WORD_JOINERS for ch in gap)


def _safe_boundary(
    text: str, offsets: list[int], target: int, lo: int, hi: int
) -> int:
    """Move a split point off a latin word when a legal one is nearby."""
    if not _inside_word(text, offsets, target):
        return target
    span = max(target - lo, hi - target)
    for delta in range(1, span + 1):
        for pos in (target - delta, target + delta):
            if lo <= pos <= hi and not _inside_word(text, offsets, pos):
                return pos
    return target


def _group_words(
    chars: list[tuple[str, int, int]], raw_text: str
) -> list[dict]:
    """Group consecutive content characters into latin words / CJK chars."""
    offsets = _content_offsets(raw_text)
    if len(offsets) != len(chars):
        offsets = []  # alignment lost; fall back to one entry per character
    words: list[dict] = []
    start = 0
    for pos in range(1, len(chars) + 1):
        if pos == len(chars) or not offsets or not _inside_word(raw_text, offsets, pos):
            chunk = chars[start:pos]
            words.append({
                "text": "".join(item[0] for item in chunk),
                "start_ms": chunk[0][1],
                "end_ms": chunk[-1][2],
            })
            start = pos
    return words


def _word_boundaries(text: str) -> set[int]:
    """Local content-character boundaries that coincide with a jieba word."""
    ends = word_ends(text)
    if not ends:
        return set()
    boundaries: set[int] = set()
    count = 0
    for i, ch in enumerate(text):
        if is_content(ch):
            count += 1
        if i + 1 in ends and 0 < count < len(content_text(text)):
            boundaries.add(count)
    return boundaries


def _choose_boundary(
    piece: dict,
    target: int,
    lo: int,
    hi: int,
    ctimes: list[tuple[str, int, int]],
) -> int:
    """Pick the most natural boundary near ``target`` (local content index)."""
    text = piece["text"]
    offsets = _content_offsets(text)
    if target <= 1 or target >= piece["chars"]:
        return max(1, min(piece["chars"] - 1, target))
    lo = max(1, min(lo, target))
    hi = min(piece["chars"] - 1, max(hi, target))
    word_bounds = _word_boundaries(text)
    best = target
    best_score = -1e9
    for pos in range(lo, hi + 1):
        gap = ctimes[piece["c0"] + pos][1] - ctimes[piece["c0"] + pos - 1][2]
        score = -abs(pos - target) * 0.6
        if pos in word_bounds:
            score += 2.0
        if gap >= 180:
            score += 1.5
        if gap >= 450:
            score += 1.0
        next_ch = text[offsets[pos]] if pos < len(offsets) else ""
        prev_ch = text[offsets[pos - 1]] if pos > 0 else ""
        if next_ch in _LEFT_BINDING:
            score -= 1.2
        if prev_ch in _LEFT_BINDING:
            score -= 1.6
        if next_ch in _TRAILING_PUNCT:
            score -= 2.0
        if score > best_score:
            best_score = score
            best = pos
    return best


def _split_piece(
    piece: dict,
    max_chars: int,
    max_ms: int,
    ctimes: list[tuple[str, int, int]],
    depth: int = 0,
) -> list[dict]:
    """Recursively split one unit until it satisfies both limits."""
    if piece["chars"] <= max_chars and piece["end"] - piece["start"] <= max_ms:
        return [piece]
    n = piece["chars"]
    duration = piece["end"] - piece["start"]
    k = max(2, math.ceil(n / max_chars), math.ceil(duration / max_ms))
    # A short sentence spread over a long pause can ask for more pieces than
    # it has characters.  k > n used to make offsets[boundary] run past the
    # end (IndexError on "甲乙" over 20 s); each piece needs at least one char.
    k = min(k, n)
    if depth >= 8 or n < 2:
        return [piece]

    offsets = _content_offsets(piece["text"])
    pieces: list[dict] = []
    prev = 0
    for idx in range(1, k):
        target = round(n * idx / k)
        lo = max(prev + 2, target - 5)
        hi = min(n - 1, target + 5, n - (k - idx))
        boundary = _choose_boundary(
            {"text": piece["text"], "c0": piece["c0"], "chars": n},
            target,
            lo,
            hi,
            ctimes,
        )
        boundary = max(prev + 1, min(boundary, n - (k - idx)))
        # Never cut through a latin/number word when another legal point is
        # close: "hello" over a long pause used to become "he" + "llo".
        boundary = _safe_boundary(
            piece["text"], offsets, boundary, prev + 1, n - (k - idx)
        )
        # 左片必须从上一次切点继续：从 offsets[prev] 起、offsets[boundary] 止。
        # 旧实现每轮都从 0 起（text[:split_at]），k >= 3 时会把前面的文本再包一遍
        # ——回归用例：50 字无标点长句切成 3 片，拼接出来是 67 字。
        # 第一片保留片首的标点/空白，否则与前一片合并时英文词间空格丢失。
        left_start = offsets[prev] if prev else 0
        left = _piece(
            piece["text"][left_start:offsets[boundary]],
            piece["c0"] + prev,
            piece["c0"] + boundary,
            ctimes,
        )
        pieces.append(left)
        prev = boundary
    tail_start = offsets[prev] if prev else 0
    tail = _piece(
        piece["text"][tail_start:], piece["c0"] + prev, piece["c1"], ctimes
    )
    pieces.append(tail)

    out: list[dict] = []
    for child in pieces:
        if child["chars"] <= 0:
            continue
        out.extend(_split_piece(child, max_chars, max_ms, ctimes, depth + 1))
    return out


def _pack(
    pieces: Iterable[dict], max_chars: int, max_ms: int,
    merge_gap_ms: int = PAUSE_MERGE_MS,
) -> list[dict]:
    groups: list[dict] = []
    current: dict | None = None
    for piece in pieces:
        if piece["chars"] <= 0:
            continue
        if current is None:
            current = dict(piece)
            continue
        gap = piece["start"] - current["end"]
        combined = current["chars"] + piece["chars"]
        if (
            gap <= max(0, int(merge_gap_ms))
            and combined <= max_chars
            and piece["end"] - current["start"] <= max_ms
        ):
            current["text"] = f"{current['text']}{piece['text']}"
            current["c1"] = piece["c1"]
            current["end"] = piece["end"]
            current["chars"] = combined
        else:
            groups.append(current)
            current = dict(piece)
    if current is not None:
        groups.append(current)
    return groups


def _finalize(
    groups: list[dict], ctimes: list[tuple[str, int, int]], max_ms: int
) -> list[dict]:
    """Enforce reading speed, minimum duration and 40 ms cue gaps."""
    for index, group in enumerate(groups):
        start = max(0, group["start"])
        next_start = groups[index + 1]["start"] if index + 1 < len(groups) else None
        group["start"] = start
        group["end"] = clamp_cue_end(
            start=start,
            end=group["end"],
            chars=group["chars"],
            next_start=next_start,
            max_ms=max_ms,
        )
    return groups


def segment_sentence(
    sentence: dict,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_ms: int = DEFAULT_MAX_MS,
    merge_gap_ms: int = PAUSE_MERGE_MS,
) -> list[dict]:
    """Return one or more short, timestamped sentences for ``sentence``."""
    max_chars = max(6, int(max_chars))
    max_ms = max(1200, int(max_ms))
    text = sentence.get("text") or ""
    chars = content_text(text)
    duration = safe_int(sentence.get("end_ms")) - safe_int(sentence.get("start_ms"))
    if not chars:
        return [dict(sentence)]
    if len(chars) <= max_chars and duration <= max_ms:
        tidied = tidy_cue_text(text)
        if tidied != text:
            out = dict(sentence)
            out["text"] = tidied
            return [out]
        return [dict(sentence)]

    ctimes = _char_times(sentence)
    if not ctimes:
        return [dict(sentence)]

    units: list[dict] = []
    for text_start, text_end, c0, c1 in _unit_spans(text):
        if c1 <= c0:
            continue
        units.append(_piece(text[text_start:text_end], c0, c1, ctimes))
    if not units:
        return [dict(sentence)]

    split_units: list[dict] = []
    for unit in units:
        split_units.extend(_split_piece(unit, max_chars, max_ms, ctimes))

    groups = _finalize(
        _pack(split_units, max_chars, max_ms, merge_gap_ms), ctimes, max_ms
    )
    out: list[dict] = []
    for group in groups:
        words = _group_words(
            ctimes[group["c0"]:group["c1"]], group["text"]
        )
        out.append(
            {
                "id": sentence.get("id", 0),
                "start_ms": group["start"],
                "end_ms": group["end"],
                "text": tidy_cue_text(group["text"]),
                "conf": sentence.get("conf", 1.0),
                "words": words,
                "edited": bool(sentence.get("edited")),
            }
        )
    return out or [dict(sentence)]


def segment_transcript(
    transcript: dict,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_ms: int = DEFAULT_MAX_MS,
    force: bool = False,
    merge_gap_ms: int = PAUSE_MERGE_MS,
) -> bool:
    """Split every over-long sentence in place; return whether anything moved.

    Edited sentences are split too when they exceed the limits; their
    ``edited`` flag is preserved on every piece.  ``force`` skips the cheap
    "already short" fast path (used for freshly generated transcripts), but
    ``segment_sentence`` itself still keeps short sentences untouched.
    """
    sentences = transcript.get("sentences") or []
    out: list[dict] = []
    changed = False
    for sentence in sentences:
        text = sentence.get("text") or ""
        tidied = tidy_cue_text(text)
        count = len(content_text(tidied))
        duration = safe_int(sentence.get("end_ms")) - safe_int(sentence.get("start_ms"))
        if not tidied:
            changed = True  # cue held nothing but punctuation
            continue
        if not force and count <= max_chars and duration <= max_ms:
            if tidied != text:
                sentence = dict(sentence)
                sentence["text"] = tidied
                changed = True
            out.append(sentence)
            continue
        pieces = segment_sentence(sentence, max_chars, max_ms, merge_gap_ms)
        if len(pieces) != 1 or pieces[0].get("text") != text:
            changed = True
        out.extend(pieces)

    if changed:
        for i, sentence in enumerate(out):
            sentence["id"] = i
        transcript["sentences"] = out
        stats = transcript.setdefault("stats", {})
        stats["sentences"] = len(out)
        # SPEC §3.2: stats.chars counts content characters, not punctuation.
        stats["chars"] = sum(count_chars(s.get("text") or "") for s in out)
        transcript["seg_version"] = SEG_VERSION
    return changed
