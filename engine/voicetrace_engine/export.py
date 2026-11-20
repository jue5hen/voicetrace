"""Subtitle / text exporters (SRT, VTT, ASS, TXT, JSON)."""

from __future__ import annotations

import json
import re
from typing import Any

from .segmentation import (
    DEFAULT_MAX_MS as MAX_CUE_MS, MIN_CUE_MS, clamp_cue_end, segment_sentence,
)
from .textutil import (
    ASCII_LETTERS, clean_subtitle_text, content_positions, is_content, ms_to_ass,
    ms_to_srt, ms_to_vtt, wrap_subtitle,
)


def _content_len(text: str) -> int:
    return len(content_positions(text))


PUNCT_CHARS = "，。！？；：、,.!?;:…—"
TERMINAL_CHARS = "。！？!?…"
MAX_MERGE_GAP_MS = 400


def _merge_units(sentences: list[dict], max_chars: int = 22) -> list[dict]:
    """Join only tiny ASR fragments (e.g. 第|五十一讲), never full sentences.

    The pipeline already emits semantic, short sentences.  Re-merging two
    normal sentences here would undo that work and reintroduce 40+ character
    cues in the exported file.
    """
    units: list[dict] = []
    merge_cap = max(16, int(max_chars * 1.4))
    for s in sentences:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        unit = {
            "start_ms": int(s.get("start_ms", 0)),
            "end_ms": int(s.get("end_ms", 0)),
            "text": text,
            "words": list(s.get("words") or []),
        }
        if units:
            prev = units[-1]
            gap = unit["start_ms"] - prev["end_ms"]
            prev_len = _content_len(prev["text"])
            text_len = _content_len(text)
            if (
                gap <= MAX_MERGE_GAP_MS
                and not prev["text"].endswith(tuple(TERMINAL_CHARS))
                and min(prev_len, text_len) <= 5
                and prev_len + text_len <= merge_cap
            ):
                # Two fragments split mid-English-phrase keep the space that
                # the strip above removed ("Hello" + "world" -> "Hello world").
                joiner = ""
                if prev["text"][-1] in ASCII_LETTERS and text[0] in ASCII_LETTERS:
                    joiner = " "
                prev["text"] += joiner + text
                prev["words"].extend(unit["words"])
                prev["end_ms"] = unit["end_ms"]
                continue
        units.append(unit)
    return units


def _char_times(words: list[dict]) -> list[tuple[int, int]]:
    times: list[tuple[int, int]] = []
    for w in words:
        wt = w.get("text") or ""
        count = sum(1 for ch in wt if is_content(ch))
        if not count:
            continue
        ws = int(w.get("start_ms", 0))
        we = int(w.get("end_ms", ws))
        times.extend([(ws, we)] * count)
    return times


def _split_segments(text: str) -> list[str]:
    segs: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in PUNCT_CHARS:
            segs.append(buf)
            buf = ""
    if buf:
        if segs:
            segs[-1] += buf
        else:
            segs.append(buf)
    return [s for s in segs if s.strip()]


def _make_chunk(segs: list[dict]) -> dict:
    first, last = segs[0], segs[-1]
    return {
        "start": int(first["start"]),
        "end": int(last["end"]),
        "text": "".join(s["text"] for s in segs).strip(),
    }


def _split_long_segment(seg: dict, budget: int) -> list[dict]:
    """Split an oversized edited cue, keeping the timings proportional.

    The raw-ASR path is already segmented before export; this is a safety net
    for a long manually edited sentence.  Every chunk gets a slice of the
    original interval instead of sharing the whole one (which used to make
    several cues overlap exactly).
    """
    text = seg["text"]
    total = _content_len(text)
    if total <= budget:
        return [dict(seg)]
    out: list[dict] = []
    buf = ""
    chars = 0
    consumed = 0
    start = int(seg["start"])
    end = int(seg["end"])
    span = max(1, end - start)
    for ch in text:
        buf += ch
        if is_content(ch):
            chars += 1
        if chars >= budget:
            chunk_start = start + span * consumed // total
            consumed += chars
            chunk_end = start + span * consumed // total
            out.append({
                "start": max(start, chunk_start),
                "end": max(chunk_start + 1, chunk_end),
                "text": buf,
            })
            buf = ""
            chars = 0
    if buf.strip():
        chunk_start = start + span * consumed // total
        out.append({
            "start": max(start, chunk_start),
            "end": max(chunk_start + 1, end),
            "text": buf,
        })
    return out


def _cue_chunks(
    sentence: dict, max_chars: int, max_lines: int, settings: dict | None = None
) -> list[dict]:
    """Return short, cleaned cues for one transcript sentence.

    Segmentation happens before cleanup so the word timings still describe the
    original text.  A cue is at most ``max_chars`` content characters; the
    exporter therefore emits one readable line rather than packing two 22-char
    lines into a 44-character block.
    """
    settings = settings or {}
    pieces = segment_sentence(sentence, max_chars=max_chars, max_ms=MAX_CUE_MS)
    out: list[dict] = []
    for piece in pieces:
        body = clean_subtitle_text(piece.get("text") or "", settings)
        if not body:
            continue
        start = int(piece.get("start_ms", 0))
        end = int(piece.get("end_ms", start + MIN_CUE_MS))
        out.append({
            "start": start,
            "end": max(end, start + MIN_CUE_MS),
            "text": body,
        })
    return out


def _punctuate_from(original: str, chunk: str, words: list[dict]) -> str:
    """Recover punctuation/space that the word stream dropped.

    The words are a subsequence of the original text (minus punctuation), so we
    walk the original and cut it whenever the reconstructed chunk is complete.
    """
    if not chunk:
        return ""
    target = chunk
    out: list[str] = []
    wi = 0
    i = 0
    acc = 0
    while i < len(original) and acc < len(target):
        ch = original[i]
        out.append(ch)
        if is_content(ch):
            acc += 1
        i += 1
    # include trailing punctuation
    while i < len(original) and not is_content(original[i]):
        out.append(original[i])
        i += 1
    res = "".join(out)
    # safety: if the chunk's characters are not a subsequence, fall back
    if _content_of(res) != target[: len(_content_of(res))]:
        return target
    return res


def _content_of(s: str) -> str:
    return "".join(ch for ch in s if is_content(ch))


def build_cues(
    transcript: dict,
    max_chars: int = 22,
    max_lines: int = 2,
    settings: dict | None = None,
) -> list[dict]:
    settings = settings or {}
    cues: list[dict] = []
    for s in _merge_units(transcript.get("sentences") or [], max_chars):
        for c in _cue_chunks(s, max_chars, max_lines, settings):
            if not (c.get("text") or "").strip():
                continue
            if c["end"] <= c["start"]:
                c["end"] = c["start"] + MIN_CUE_MS
            cues.append(c)

    try:
        media_end = int((transcript.get("media") or {}).get("duration_ms") or 0)
    except (TypeError, ValueError):
        media_end = 0

    clamped: list[dict] = []
    for i, cue in enumerate(cues):
        chars = _content_len(cue["text"])
        next_start = cues[i + 1]["start"] if i + 1 < len(cues) else None
        # 单一实现：策略见 segmentation.clamp_cue_end（40 ms 间隔优先于 900 ms 最短时长）
        cue["end"] = clamp_cue_end(
            start=cue["start"],
            end=cue["end"],
            chars=chars,
            next_start=next_start,
            max_ms=MAX_CUE_MS,
        )
        if media_end > 0:
            # The media end is a hard ceiling: the old max(start + 200, ...)
            # floor could push the last cue past it (900-1100 ms on a 1000 ms
            # clip).  A cue starting at/after the end is degenerate: drop it.
            if cue["start"] >= media_end:
                continue
            cue["end"] = min(cue["end"], media_end)
            if cue["end"] <= cue["start"]:
                continue
        clamped.append(cue)
    return clamped


def to_srt(transcript: dict, settings: dict | None = None) -> str:
    settings = settings or {}
    cues = build_cues(transcript, int(settings.get("subtitle_max_chars", 22)),
                      int(settings.get("subtitle_max_lines", 2)), settings)
    out = []
    for i, c in enumerate(cues, 1):
        body = wrap_subtitle(c["text"], int(settings.get("subtitle_max_chars", 22)),
                             int(settings.get("subtitle_max_lines", 2)))
        out.append(f"{i}\n{ms_to_srt(c['start'])} --> {ms_to_srt(c['end'])}\n{body}\n")
    return "\n".join(out)


def to_vtt(transcript: dict, settings: dict | None = None) -> str:
    settings = settings or {}
    cues = build_cues(transcript, int(settings.get("subtitle_max_chars", 22)),
                      int(settings.get("subtitle_max_lines", 2)), settings)
    out = ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        body = wrap_subtitle(c["text"], int(settings.get("subtitle_max_chars", 22)),
                             int(settings.get("subtitle_max_lines", 2)))
        out.append(f"{i}\n{ms_to_vtt(c['start'])} --> {ms_to_vtt(c['end'])}\n{body}\n")
    return "\n".join(out)


_ASS_HEADER = """[Script Info]
; Generated by 声迹 VoiceTrace
Title: {title}
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{fontsize},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2.2,1.2,2,48,48,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def to_ass(transcript: dict, settings: dict | None = None) -> str:
    settings = settings or {}
    cues = build_cues(transcript, int(settings.get("subtitle_max_chars", 22)),
                      int(settings.get("subtitle_max_lines", 2)), settings)
    title = (transcript.get("media") or {}).get("name", "VoiceTrace")
    out = [_ASS_HEADER.format(title=_ass_escape(title), fontsize=46, marginv=40)]
    for c in cues:
        body = wrap_subtitle(c["text"], int(settings.get("subtitle_max_chars", 22)),
                             int(settings.get("subtitle_max_lines", 2)))
        # ASS treats {\...} as override tags and \N as a line break: escape the
        # cue text before turning the real newlines into \N (the karaoke path
        # already did this; the plain dialogue path did not).
        body = _ass_escape(body).replace("\n", "\\N")
        out.append(
            f"Dialogue: 0,{ms_to_ass(c['start'])},{ms_to_ass(c['end'])},Default,,0,0,0,,{body}\n"
        )
    return "".join(out)


def _ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def to_txt(transcript: dict, settings: dict | None = None) -> str:
    settings = settings or {}
    lines = []
    for s in transcript.get("sentences") or []:
        t = clean_subtitle_text((s.get("text") or "").strip(), settings)
        if t:
            lines.append(t)
    return "\n".join(lines) + "\n"


def to_json(transcript: dict, settings: dict | None = None) -> str:
    return json.dumps(transcript, ensure_ascii=False, indent=2)


EXPORTERS = {
    "srt": to_srt,
    "vtt": to_vtt,
    "ass": to_ass,
    "txt": to_txt,
    "json": to_json,
}


def export(transcript: dict, fmt: str, settings: dict | None = None) -> str:
    fmt = (fmt or "srt").lower()
    if fmt not in EXPORTERS:
        raise ValueError(f"不支持的格式 {fmt}")
    return EXPORTERS[fmt](transcript, settings)
