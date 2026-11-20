from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Sentence-ending punctuation used by FireRedPunc plus common ASCII variants.
TERMINAL_PUNCT = set("。！？!?…")
SOFT_PUNCT = set("，、；：,;:")
ALL_PUNCT = set("。！？!?…，、；：,;:.·“”‘’\"'（）()《》〈〉【】[]{}「」『』—–-～~·・/\\|@#￥$%^&*+=<>_`")

_SPACE_CHARS = set(" \t\r\n\u3000\u00a0")
ASCII_LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
ASCII_DIGITS = set("0123456789")

# 单一真源：cleanup 与 analysis 共用，避免「cleanup 保住、auto_correct 又折叠」。
# 单音节动词/时间单位重叠是词汇性重叠（理理发/分分钟），不是口吃。
PREFIX_VERB_KEEP = frozenset(
    "理唱游放过握谈看听说走跑跳吃喝玩做写洗买卖试学问找想歇坐站睡练读画数算猜尝摸拍抱亲笑哭分"
    "用出打抬吹划抽改动弯转闭甩搓清逛"
)
# 数词+量词「逐一」重叠（一个一个/一餐一餐）的量词尾字。cleanup 与 analysis
# 共用这一张表：cleanup 判合法的，analysis 不会再报成 repeat 被 auto-correct 折叠。
MEASURE_CHARS = set(
    "个位名条份句次回遍趟张只件种样层阵对双群批堆捆包箱排组类级页篇段节章"
    "秒分天年月日岁时场幕套支部辆艘架台桌杯碗盘滴颗粒棵根株朵束串代户步笔"
    "口声行间座家队幢所顶匹头封通则餐顿门面把扇幅枚番届期任克斤米元角吨升"
    "个位名次种些点条只件张座家间份句步遍趟回层段块片颗粒支根束串群队"
    "对双套样行页篇声字天年月日夜周届场期批轮阵圈口排班秒分杯碗盘瓶包"
    "袋箱桶亩斤米吨朵棵辆台部幕集章节本幅顿勺捧把面匹艘架列拨沓摞"
    "堆捆组类级岁桌滴株代户笔则封通枚番届任克元角升幢所顶头门扇"
)


def safe_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def is_punct(ch: str) -> bool:
    if ch in ALL_PUNCT or ch in _SPACE_CHARS:
        return True
    cat = unicodedata.category(ch)
    # P/Z/S 之外，控制与格式字符（Cc/Cf/Cs，如 NUL、ZWJ、BOM、软连字符）也不算
    # 内容字（SPEC §3.2：stats.chars 不含控制字符）。
    return (cat.startswith("P") or cat.startswith("Z") or cat.startswith("S")
            or cat in ("Cc", "Cf", "Cs"))


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x20000 <= o <= 0x2FA1F
        or 0xF900 <= o <= 0xFAFF
    )


def is_content(ch: str) -> bool:
    return not is_punct(ch)


def content_positions(text: str) -> list[int]:
    """Indices in `text` of every non-punctuation, non-space character."""
    return [i for i, ch in enumerate(text) if is_content(ch)]


def content_text(text: str) -> str:
    return "".join(ch for ch in text if is_content(ch))


def strip_punct(text: str) -> str:
    return content_text(text)


def normalize(text: str) -> str:
    return content_text(text).lower()


def count_chars(text: str) -> int:
    return len(content_text(text))


@lru_cache(maxsize=4096)
def _lazy_pinyin_char(ch: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
        out = lazy_pinyin(ch, style=Style.NORMAL, errors="ignore")
        return out[0] if out else ""
    except Exception:
        return ""


def pinyin_of(text: str) -> list[str]:
    """Syllable per CJK character; '' for non-CJK characters (kept for alignment)."""
    out: list[str] = []
    for ch in text:
        if is_cjk(ch):
            out.append(_lazy_pinyin_char(ch))
        else:
            out.append("")
    return out


def pinyin_join(text: str) -> str:
    return " ".join(s for s in pinyin_of(text) if s)


_FUZZY_MAP = [
    ("zh", "z"), ("ch", "c"), ("sh", "s"),
    ("iang", "ian"), ("uang", "uan"), ("ang", "an"), ("eng", "en"), ("ing", "in"),
    ("ong", "on"), ("ian", "ien"),
    ("n", "l"),
]


def fuzzy_syllable(syl: str) -> str:
    s = syl.lower()
    for a, b in _FUZZY_MAP:
        if s.startswith(a):
            s = b + s[len(a):]
            break
    for a, b in _FUZZY_MAP:
        if s.endswith(a) and len(s) > len(a):
            s = s[: -len(a)] + b
            break
    return s


def tokens_to_words(tokens: list[tuple[str, float, float]], offset_ms: float = 0.0,
                    clip: tuple[int, int] | None = None) -> list[dict]:
    """Convert ASR (token, start_s, end_s) to word dicts in absolute ms."""
    words: list[dict] = []
    for tok, start_s, end_s in tokens:
        tok = (tok or "").strip()
        if not tok:
            continue
        # strip sentencepiece word-boundary markers if any survived
        tok = tok.replace("▁", " ").strip()
        if not tok:
            continue
        s = int(round(offset_ms + float(start_s) * 1000))
        e = int(round(offset_ms + float(end_s) * 1000))
        if clip is not None:
            s = max(clip[0], min(clip[1], s))
            e = max(clip[0], min(clip[1], e))
        if e < s:
            e = s
        words.append({"text": tok, "start_ms": s, "end_ms": e})
    return words


def merge_adjacent_tokens(words: list[dict], max_gap_ms: int = 1) -> list[dict]:
    """Merge punctuation-only or duplicate artefacts; keep simple pass-through."""
    out: list[dict] = []
    for w in words:
        t = w.get("text", "")
        if not t or all(is_punct(c) for c in t):
            continue
        if out and out[-1]["text"] == t and abs(out[-1]["start_ms"] - w["start_ms"]) <= max_gap_ms:
            out[-1]["end_ms"] = max(out[-1]["end_ms"], w["end_ms"])
            continue
        out.append({"text": t, "start_ms": int(w["start_ms"]), "end_ms": int(w["end_ms"])})
    return out


# ------------------------------------------------------------------ time formatting

def ms_to_srt(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, millis = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def ms_to_vtt(ms: int) -> str:
    return ms_to_srt(ms).replace(",", ".")


def ms_to_ass(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, millis = divmod(rem, 1000)
    cs = millis // 10
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def ms_to_clock(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, _ = divmod(rem, 1000)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_duration(ms: int) -> str:
    return ms_to_clock(ms)


# ------------------------------------------------------------------ subtitle lines

def _split_long(text: str, limit: int) -> list[str]:
    """Split a long run into chunks <= limit chars, preferring punctuation/space."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[，、；：,;:])", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= limit:
            out.append(p)
            continue
        # hard split, prefer latin word boundary
        buf = ""
        for token in re.split(r"(?<=\s)", p):
            if len(buf) + len(token) <= limit:
                buf += token
            else:
                if buf:
                    out.append(buf.strip())
                while len(token) > limit:
                    out.append(token[:limit])
                    token = token[limit:]
                buf = token
        if buf.strip():
            out.append(buf.strip())
    return out


def _truncate_content(text: str, limit: int) -> str:
    """Cut text after limit content characters, keeping trailing punctuation."""
    out: list[str] = []
    count = 0
    index = 0
    while index < len(text):
        ch = text[index]
        out.append(ch)
        index += 1
        if is_content(ch):
            count += 1
            if count >= limit:
                break
    while index < len(text) and not is_content(text[index]):
        out.append(text[index])
        index += 1
    return "".join(out).strip()


def wrap_subtitle(text: str, max_chars: int = 22, max_lines: int = 2) -> str:
    """Wrap into at most ``max_lines`` balanced lines, each no longer than ``max_chars``."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    max_chars = max(6, max_chars)
    max_lines = max(1, max_lines)
    if len(text) <= max_chars:
        return text
    if max_lines == 1:
        # The budget is in *content* characters (punctuation excluded), just
        # like the sentence splitter.  Slicing the raw string dropped the last
        # character as soon as punctuation had been turned into a space
        # (22 chars + one comma -> only 21 chars exported).
        if count_chars(text) <= max_chars:
            return text
        return _truncate_content(text, max_chars)

    lines: list[str] = []
    rest = text
    while rest and len(lines) < max_lines:
        slots = max_lines - len(lines)
        if len(rest) <= max_chars:
            lines.append(rest)
            break
        if slots == 1:
            lines.append(rest)
            break
        target = max(1, len(rest) // slots)
        cut = _break_near(rest, min(target, max_chars), max_chars)
        head = rest[:cut].strip()
        if head:
            lines.append(head)
        rest = rest[cut:].strip()
    return "\n".join(lines)


def _break_near(text: str, target: int, limit: int) -> int:
    """Pick a break position <= limit, preferring punctuation and word boundaries."""
    limit = max(1, min(limit, len(text) - 1))
    lo = max(1, min(target, limit))
    for i in range(limit, lo - 1, -1):
        ch = text[i - 1]
        if ch in SOFT_PUNCT or ch in TERMINAL_PUNCT or ch == " ":
            return i
    ends = _word_ends(text)
    candidates = [e for e in ends if lo <= e <= limit]
    if candidates:
        return min(candidates, key=lambda e: abs(e - target))
    return lo


def word_ends(text: str) -> tuple[int, ...]:
    """Public wrapper around jieba word-end offsets (empty when unavailable)."""
    return _word_ends(text)


@lru_cache(maxsize=4096)
def _word_ends(text: str) -> tuple[int, ...]:
    """Character offsets where jieba ends a word (empty when jieba is absent)."""
    try:
        import jieba
        return tuple(sorted({end for _, _, end in jieba.tokenize(text) if 0 < end < len(text)}))
    except Exception:
        return ()


def _is_word_boundary(text: str, pos: int) -> bool:
    ends = _word_ends(text)
    if not ends:
        return False
    # bisect would be nicer, but these lists are tiny (one sentence at a time).
    return pos in ends


def _best_break(text: str, limit: int) -> int:
    mid = len(text) // 2
    best = mid
    for i in range(max(1, mid - 6), min(len(text) - 1, mid + 6)):
        ch = text[i]
        if ch in SOFT_PUNCT or ch in TERMINAL_PUNCT or ch == " ":
            best = i + 1
            break
    return max(1, min(len(text) - 1, best))


# ------------------------------------------------------------------ subtitle cleanup

# Conservative filler set: only standalone interjections and clause-final
# 啊/呀/哦 are removed.  Lexical uses (啊哈, 天啊) are left untouched.
_FILLER_CHARS = "嗯呃额哦噢喔唉哎诶欸咦唔唷呐啊呀"
_FILLERS = "|".join(_FILLER_CHARS)
# Leading interjections are also dropped when the ASR forgot the comma
# ("诶老老师你看嘛" -> "老师你看嘛").  啊/呀 are deliberately excluded here:
# at the start of a cue they are far more often real words (啊哈).
_FILLER_LOOSE = "嗯呃额哦噢喔唉哎诶欸咦唔唷呐"
_FILLER_STANDALONE = re.compile(
    rf"(^|[\s，。！？；：、,.!?;:])((?:{_FILLERS})+)(?=[\s，。！？；：、,.!?;:]|$)"
)
_FILLER_START = re.compile(rf"^(?:{_FILLERS})+[，,、\s]+")
_FILLER_LOOSE_RE = re.compile(
    rf"(^|[，,、；;：:])\s*([{_FILLER_LOOSE}]+)(?=[\u4e00-\u9fff])"
)
# Interjections that are the first syllable of a real word.
_FILLER_KEEP_WORDS = ("哎呀", "哎哟", "唉呀", "唉哟", "唉声", "诶呀", "咦呀")
_FILLER_TAIL = re.compile(
    r"(?<=[\u4e00-\u9fff])(?:啊|呀|哦)(?=[\s，。！？；：、,.!?;:]|$)"
)


def _dict_word(word: str) -> bool:
    """Whether jieba knows ``word`` (used to protect lexical uses)."""
    try:
        import jieba
        return bool(jieba.get_FREQ(word))
    except Exception:
        return False


def _drop_standalone_filler(match: "re.Match[str]") -> str:
    window = match.string[max(0, match.start(2) - 1):match.end(2) + 2]
    if any(word in window for word in _FILLER_KEEP_WORDS):
        return match.group(0)  # 哎呀 / 哎哟 are words, not fillers
    return match.group(1)


def _drop_tail_filler(match: "re.Match[str]") -> str:
    # Same keep list as the other passes: 哎呀 / 唉呀 / 咦呀 end in 呀 but are
    # words, and 天啊 / 妈呀 are exclamations rather than a dangling particle.
    start = match.start()
    window = match.string[max(0, start - 1):match.end() + 1]
    if any(word in window for word in _FILLER_KEEP_WORDS):
        return match.group(0)
    if match.string[start - 1] in "天妈":
        return match.group(0)
    return ""


def _drop_leading_filler(match: "re.Match[str]") -> str:
    # The leading-interjection pass must honour the same keep list as the
    # standalone pass: 哎呀 / 哎哟 / 唉呀 are words, not fillers (they used to
    # be deleted here right after _drop_standalone_filler had protected them).
    if any(word in match.group(0) for word in _FILLER_KEEP_WORDS):
        return match.group(0)
    return ""


def _drop_loose_filler(match: "re.Match[str]") -> str:
    run = match.group(2)
    start, end = match.start(2), match.end(2)
    window = match.string[max(0, start - 1):end + 3]
    # 哎呀 / 哎哟 / 唉声叹气 look like filler + word but are lexical.
    if any(word in window for word in _FILLER_KEEP_WORDS):
        return match.group(0)
    following = match.string[end:end + 1]
    if run and following and _dict_word(run[-1] + following):
        return match.group(0)
    return match.group(1)


def remove_fillers(text: str) -> str:
    """Drop common spoken fillers without touching meaningful words."""
    if not text:
        return ""
    out = _FILLER_STANDALONE.sub(_drop_standalone_filler, text)
    out = _FILLER_START.sub(_drop_leading_filler, out)
    out = out.lstrip("，,、 ")
    out = _FILLER_LOOSE_RE.sub(_drop_loose_filler, out)
    out = _FILLER_TAIL.sub(_drop_tail_filler, out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", out)
    out = out.lstrip("，,、；;：: ")
    return out.strip()


def punctuation_to_space(text: str) -> str:
    """Replace Chinese clause/sentence punctuation with a readable space."""
    if not text:
        return ""
    out = re.sub(r"\s*[，。、]\s*", " ", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([！？!?])", r"\1", out)
    return out.strip()


def clean_subtitle_text(text: str, settings: dict | None = None) -> str:
    """Apply optional subtitle cleanup controlled by user settings."""
    settings = settings or {}
    out = text or ""
    if settings.get("subtitle_clean_fillers", True):
        out = remove_fillers(out)
    if settings.get("subtitle_punct_space", True):
        out = punctuation_to_space(out)
    return out.strip()


# ---------------------------------------------------------------- cue text

_LEADING_NOISE = "，、；：,;:。.！!？?…—-"
_TRAILING_SOFT = "，、；：,;:"


def tidy_cue_text(text: str) -> str:
    """Normalise one subtitle cue for display and export.

    ASR units keep the punctuation that terminated them, so cues regularly
    ended in "，".  A subtitle line should not dangle a comma: leading noise is
    dropped, duplicated marks collapsed and *soft* trailing marks removed.
    Sentence-final 。！？… are kept because they carry meaning.
    """
    out = (text or "").strip()
    if not out:
        return ""
    out = re.sub(rf"^[\s{_LEADING_NOISE}]+", "", out)
    out = re.sub(r"([，,、；;：:])(?:\s*\1)+", r"\1", out)
    out = re.sub(r"([。！？!?…])[，,、；;：:]+", r"\1", out)
    out = re.sub(r"([，,、；;：:])\s*([。！？!?…])", r"\2", out)
    out = re.sub(rf"[\s{_TRAILING_SOFT}]+$", "", out)
    out = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out)
    # ASR often leaves a space at a Chinese word boundary; cues read better
    # without it.
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", out)
    out = re.sub(r"([。！？!?…])\s+(?=[\u4e00-\u9fff])", r"\1", out)
    return out.strip()
