"""Transcript editing: replacements, stutter removal, manual rewrites.

Every edit rebuilds the per-character timeline so word-level timestamps stay
consistent after text changes.  The alignment is a simple difflib opcode walk
over the punctuation-free character stream.
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable

from .segmentation import _inside_word
from .textutil import content_positions, count_chars, is_cjk, safe_int


def _char_times(words: list[dict], total_start: int, total_end: int) -> tuple[str, list[tuple[int, int]]]:
    chars: list[str] = []
    times: list[tuple[int, int]] = []
    for w in words or []:
        text = w.get("text") or ""
        if not text:
            continue
        s = safe_int(w.get("start_ms"), total_start)
        e = safe_int(w.get("end_ms"), s)
        if e < s:
            e = s
        n = len(text)
        span = max(e - s, 1)
        for i, ch in enumerate(text):
            chars.append(ch)
            cs = s + round(span * i / n)
            ce = s + round(span * (i + 1) / n)
            if ce <= cs:
                ce = cs + 1
            times.append((cs, ce))
    if not chars:
        # fall back to an even spread over the sentence
        return "", []
    return "".join(chars), times


_INSERT_CHAR_MS = 120  # 新增字符的默认时长（约 8 字/秒）


def _fill_gaps(times: list[tuple[int, int] | None], start: int, end: int) -> list[tuple[int, int]]:
    n = len(times)
    if n == 0:
        return []
    # forward fill
    prev_end = start
    for i in range(n):
        if times[i] is None:
            times[i] = (prev_end, prev_end)
        prev_end = max(prev_end, times[i][1])  # type: ignore[index]
    # backward pass to give zero-width entries a little room
    for i in range(n - 1, -1, -1):
        s, e = times[i]  # type: ignore[misc]
        if e <= s:
            nxt = times[i + 1][0] if i + 1 < n else end  # type: ignore[index]
            e = max(s, min(nxt, e + 60))
            times[i] = (s, e)
    out: list[tuple[int, int]] = []
    last = start
    for s, e in times:  # type: ignore[misc]
        s = max(start, s)
        e = max(e, s)
        if s < last:
            # A later insertion pushed this retained character past its old
            # slot.  Shift it (and its end) forward instead of clamping it
            # flat, which used to produce a zero-length word (乙 680-680 ms).
            shift = last - s
            s += shift
            e += shift
        if e <= s:
            e = s + 1
        out.append((s, e))
        last = e
    return out


def rebuild_words(text: str, old_words: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    new_positions = content_positions(text)
    new_chars = "".join(text[i] for i in new_positions)
    if not new_chars:
        return []

    old_chars, old_times = _char_times(old_words, start_ms, end_ms)
    if not old_chars:
        # spread evenly by character count
        span = max(end_ms - start_ms, len(new_chars) * 120)
        step = span / len(new_chars)
        new_times: list[tuple[int, int] | None] = [
            (int(start_ms + i * step), int(start_ms + (i + 1) * step)) for i in range(len(new_chars))
        ]
    else:
        new_times = [None] * len(new_chars)
        sm = difflib.SequenceMatcher(None, old_chars, new_chars, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(j2 - j1):
                    new_times[j1 + k] = old_times[i1 + k]
            elif tag == "replace":
                os_ = old_times[i1][0] if i1 < len(old_times) else start_ms
                oe = old_times[i2 - 1][1] if i2 - 1 < len(old_times) and i2 > i1 else os_
                if oe <= os_:
                    oe = os_ + 60
                cnt = j2 - j1
                step = (oe - os_) / max(cnt, 1)
                for k in range(cnt):
                    new_times[j1 + k] = (int(os_ + k * step), int(os_ + (k + 1) * step))
            elif tag == "insert":
                anchor = old_times[i1 - 1][1] if i1 > 0 else start_ms
                cnt = j2 - j1
                # 每个新字给 120 ms（约 8 字/秒），比原来的 30 ms 更接近真实语速
                for k in range(cnt):
                    new_times[j1 + k] = (
                        anchor + k * _INSERT_CHAR_MS,
                        anchor + (k + 1) * _INSERT_CHAR_MS,
                    )
            # delete: nothing to do
        # 插入的文字可能比原窗口更长：先按自然排布算出需要的结尾，再把窗口放宽。
        # 否则 _fill_gaps 会把新字全部压回旧 end_ms，产生一堆零时长词
        #（回归：2 字句改成 17 字后，15/17 个词都落在同一个时间点）。
        natural_end = max([start_ms] + [t[1] for t in new_times if t is not None])
        new_times = _fill_gaps(new_times, start_ms, max(end_ms, natural_end))

    # Group content characters into latin words / CJK chars, using the SAME
    # word-boundary rule as segmentation (_inside_word): a gap made only of
    # word joiners ('’-) glues two word chars, so don't / well-known stay one
    # word instead of being split mid-word (SPEC §3.2 英文按单词).  Word text
    # is taken from the raw span so internal joiners are preserved.
    words: list[dict] = []
    i = 0
    total = len(new_chars)
    while i < total:
        start = new_times[i][0]
        j = i + 1
        while j < total and _inside_word(text, new_positions, j):
            j += 1
        word_text = text[new_positions[i]:new_positions[j - 1] + 1]
        words.append({"text": word_text, "start_ms": start,
                      "end_ms": new_times[j - 1][1]})
        i = j
    return [w for w in words if (w["text"] or "").strip()]


def set_sentence_text(sentence: dict[str, Any], new_text: str) -> dict[str, Any]:
    old_words = sentence.get("words") or []
    start = int(sentence.get("start_ms", 0))
    end = int(sentence.get("end_ms", start + 1000))
    sentence["text"] = new_text
    sentence["words"] = rebuild_words(new_text, old_words, start, end)
    if sentence["words"]:
        sentence["start_ms"] = min(start, sentence["words"][0]["start_ms"])
        sentence["end_ms"] = max(end, sentence["words"][-1]["end_ms"])
    sentence["edited"] = True
    return sentence


def _find_sentence(transcript: dict, sid: int) -> dict | None:
    for s in transcript.get("sentences") or []:
        if int(s.get("id", -1)) == int(sid):
            return s
    return None


def _replace_in_sentence(sentence: dict, old: str, new: str) -> int:
    text = sentence.get("text") or ""
    if not old or old not in text:
        return 0
    count = text.count(old)
    new_text = text.replace(old, new)
    set_sentence_text(sentence, new_text)
    return count


def apply_replace(transcript: dict, old: str, new: str, scope: str = "all",
                  sentence_ids: Iterable[int] | None = None) -> dict:
    if not old:
        raise ValueError("from 不能为空")
    if not new:
        raise ValueError("替换词不能为空")
    if scope not in ("all", "selected"):
        # 未知作用域绝不能退化成「全篇替换」：一个拼错的 scope（或 null）
        # 会把用户只想改一句的替换扩散到整篇字幕。
        raise ValueError(f"未知作用域 {scope}")
    ids = {int(x) for x in (sentence_ids or [])}
    changed = 0
    sentences = transcript.get("sentences") or []
    for s in sentences:
        if scope == "selected" and int(s.get("id", -1)) not in ids:
            continue
        changed += _replace_in_sentence(s, old, new)
    if changed == 0 and scope == "all":
        # fall back to a global text pass in case the span crossed punctuation
        whole = "".join((s.get("text") or "") for s in sentences)
        if old in whole:
            for s in sentences:
                changed += _replace_in_sentence(s, old, new)
    return {"changed": changed}


def _repeat_unit(span: str) -> str:
    """Smallest block that tiles the span ("好像好像" -> "好像")."""
    n = len(span)
    for size in range(1, n // 2 + 1):
        if n % size == 0 and span == span[:size] * (n // size):
            return span[:size]
    return span[:max(1, n // 2)]


def apply_repeat(transcript: dict, sentence_id: int, start: int, end: int,
                 keep: str | None = None) -> dict:
    s = _find_sentence(transcript, sentence_id)
    if not s:
        raise KeyError("句段不存在")
    text = s.get("text") or ""
    start = max(0, min(len(text), int(start)))
    end = max(start, min(len(text), int(end)))
    if end <= start:
        raise ValueError("无效的重复范围")
    span = text[start:end]
    if keep is None:
        keep = _repeat_unit(span)
    new_text = text[:start] + keep + text[end:]
    set_sentence_text(s, new_text)
    return {"changed": 1}


def apply_dismiss(transcript: dict, issue_id: str) -> None:
    dismissed = list(transcript.get("dismissed") or [])
    if issue_id and issue_id not in dismissed:
        dismissed.append(issue_id)
    transcript["dismissed"] = dismissed[-500:]


def apply_actions(transcript: dict, actions: list[dict], settings: dict | None = None) -> dict:
    report = {"changed": 0, "actions": []}
    # tolerate older clients that send {"kind": ...} instead of {"type": ...}
    for act in actions or []:
        if isinstance(act, dict) and not act.get("type") and act.get("kind"):
            act["type"] = act["kind"]
    # Span edits carry character offsets computed against the pre-edit text, so
    # apply them per sentence from the right: earlier offsets stay valid.
    # Text-matching replacements do not depend on offsets and run last.
    ordered: list[dict] = []
    queued_span: dict[int, list[dict]] = {}
    for act in actions or []:
        kind = (act.get("type") or "").lower()
        if kind in ("repeat", "set_text"):
            try:
                sid = int(act["sentence_id"])
            except (KeyError, TypeError, ValueError):
                ordered.append(act)
                continue
            queued_span.setdefault(sid, []).append(act)
        else:
            ordered.append(act)
    for sid in sorted(queued_span):
        group = queued_span[sid]
        whole = [a for a in group if (a.get("type") or "").lower() == "set_text"]
        spans = [a for a in group if (a.get("type") or "").lower() == "repeat"]
        if whole:
            # a whole-sentence rewrite supersedes span edits of that sentence
            ordered.extend(whole[-1:])
        else:
            spans.sort(key=lambda a: safe_int(a.get("start"), 0), reverse=True)
            ordered.extend(spans)
    for act in ordered:
        kind = (act.get("type") or "").lower()
        try:
            if kind == "replace":
                r = apply_replace(transcript, act.get("from", ""), act.get("to", ""),
                                  act.get("scope", "all"), act.get("sentence_ids"))
                report["changed"] += r["changed"]
                report["actions"].append({"type": kind, "changed": r["changed"]})
            elif kind == "repeat":
                r = apply_repeat(transcript, int(act["sentence_id"]), int(act["start"]),
                                 int(act["end"]), act.get("keep"))
                report["changed"] += r["changed"]
                report["actions"].append({"type": kind, "changed": r["changed"]})
            elif kind == "dismiss":
                apply_dismiss(transcript, str(act.get("issue_id") or ""))
                report["actions"].append({"type": kind, "changed": 0})
            elif kind == "set_text":
                s = _find_sentence(transcript, int(act["sentence_id"]))
                if not s:
                    raise KeyError("句段不存在")
                set_sentence_text(s, str(act.get("text") or ""))
                report["changed"] += 1
                report["actions"].append({"type": kind, "changed": 1})
            else:
                raise ValueError(f"未知操作 {kind}")
        except (KeyError, ValueError, TypeError) as e:
            # TypeError: a malformed action ({"sentence_id": null}) used to
            # escape this loop and turn the whole /apply call into a 500 while
            # the pre-edit history snapshot had already been written.
            report["actions"].append({"type": kind, "error": str(e)})
    # drop empty sentences, renumber
    transcript["sentences"] = [s for s in transcript.get("sentences") or [] if (s.get("text") or "").strip()]
    for i, s in enumerate(transcript["sentences"]):
        s["id"] = i
    transcript["version"] = int(transcript.get("version", 0)) + 1
    recompute_stats(transcript)
    return report


def recompute_stats(transcript: dict) -> dict:
    sentences = transcript.get("sentences") or []
    # SPEC §3.2: content characters only (no punctuation, no whitespace).
    chars = sum(count_chars(s.get("text") or "") for s in sentences)
    duration = int((transcript.get("media") or {}).get("duration_ms", 0) or 0)
    stats = dict(transcript.get("stats") or {})
    stats.update({
        "chars": chars,
        "sentences": len(sentences),
        "duration_ms": duration,
    })
    transcript["stats"] = stats
    return stats
