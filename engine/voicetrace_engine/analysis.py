"""Transcript proof-reading analysis.

Two product features live here:

1. 疑似同音错字 ("suspicious"): if a short character n-gram occurs many times
   while a tonally identical variant occurs only once or twice, the rare one is
   probably a mis-recognition (胜兵 10x vs 圣兵 1x).  We compare pinyin **with
   tones** so legitimate near-homophones (时候 / 事后) do not flood the list.
2. 口吃重复 ("repeat"): immediate repetition such as 好像好像 / 我我觉得.
   Detected on the punctuation-free character stream, so 好像，好像 is caught too.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable

from .textutil import (
    MEASURE_CHARS,
    PREFIX_VERB_KEEP,
    content_positions,
    is_cjk,
    pinyin_of,
)

try:  # optional but strongly recommended: keeps n-grams on word boundaries
    import jieba  # type: ignore
except Exception:  # pragma: no cover - release runtime ships jieba
    jieba = None

# Characters that almost always end/start a *different* word, so an n-gram
# starting or ending on them is a segmentation accident rather than a
# mis-heard word (哪儿不明确 -> 儿不, 时候一定 -> 候一, 话的样子 -> 话的).
HEAD_STOP = set(
    "的了着过是我就都也还又很太更最不没把被给让从向对为以于之其所及等"
    "个位名次种些点条只件张座家间这那他她它我你您们"
    "啊呀呢吧吗嘛哦嗯呃儿子头候时上下里中前后"
    "一二三四五六七八九十百千万零〇两几"
)
TAIL_STOP = set("的了着过啊呀呢吧吗嘛哦嗯呃们么儿")
CJK_DIGITS = set("零〇一二三四五六七八九两")

# Two-character reduplications that are normal Chinese (看看 / 谢谢 / 妈妈 ...).
LEGIT_REDUP = {
    "看看", "试试", "想想", "问问", "说说", "听听", "走走", "坐坐", "等等", "找找",
    "聊聊", "谈谈", "玩玩", "歇歇", "尝尝", "摸摸", "拍拍", "洗洗", "数数", "算算",
    "猜猜", "练练", "读读", "写写", "画画", "唱唱", "跑跑", "笑笑", "抱抱", "亲亲",
    "谢谢", "拜拜", "好好", "慢慢", "快快", "早早", "往往", "统统", "通通", "纷纷",
    "一一", "万万", "稍稍", "恰恰", "偏偏", "微微", "渐渐", "悄悄", "默默", "静静",
    "安安", "多多", "少少", "久久", "远远", "近近", "高高", "大大", "小小", "细细",
    "圆圆", "扁扁", "长长", "短短", "厚厚", "薄薄", "轻轻", "重重", "深深", "浅浅",
    "满满", "空空", "干干", "净净", "甜甜", "苦苦", "酸酸", "辣辣", "香香", "臭臭",
    "暖暖", "凉凉", "热热", "冷冷", "匆匆", "缓缓", "徐徐", "暗暗", "白白", "明明",
    "天天", "年年", "月月", "日日", "夜夜", "次次", "回回", "遍遍", "声声", "字字",
    "句句", "篇篇", "页页", "行行", "条条", "根根", "支支", "张张", "片片", "块块",
    "颗颗", "粒粒", "滴滴", "点点", "星星", "朵朵", "束束", "串串", "群群", "队队",
    "对对", "双双", "套套", "件件", "种种", "样样", "层层", "阵阵", "缕缕", "丝丝",
    "股股", "团团", "簇簇", "丛丛", "排排", "列列", "妈妈", "爸爸", "爷爷", "奶奶",
    "哥哥", "姐姐", "弟弟", "妹妹", "宝宝", "娃娃", "舅舅", "姑姑", "叔叔", "婶婶",
    "婆婆", "公公", "太太", "先生", "一一", "刚刚", "常常", "偷偷", "恋恋", "依依",
    "一个个", "一个个", "一次次", "一天天", "一年年", "一点点", "一步步", "一家家",
    "一件件", "一条条", "一片片", "一句句", "一声声", "一阵阵", "一丝丝", "一层层",
    "一次次", "一回回", "一趟趟", "一遍遍", "一个个", "一只只", "一朵朵", "一颗颗",
    "一粒粒", "一串串", "一群群", "一对对", "一双双", "一套套", "一份份", "一段段",
    "一篇篇", "一页页", "一行行", "一排排", "一队队", "一堆堆", "一摞摞", "一沓沓",
    "说着", "想着", "看着", "走着", "聊着", "听着", "笑着", "哭着", "吃着", "喝着",
    "玩着", "做着", "写着", "读着", "跑着", "唱着", "跳着", "开着", "关着", "等着",
    "拿着", "放着", "坐着", "站着", "躺着", "睡着", "醒着", "忙着", "闲着", "累着",
    "说着说着", "想着想着", "看着看着", "走着走着", "聊着聊着", "听着听着",
    # ABAB verb reduplication (收拾收拾 / 研究研究) is grammatical, not a stutter
    "收拾", "商量", "研究", "打扫", "考虑", "休息", "活动", "锻炼", "检查",
    "整理", "准备", "介绍", "讨论", "交流", "体验", "尝试", "感受", "熟悉",
    "适应", "分析", "调查", "观察", "学习", "练习", "思考", "了解", "认识",
    "改进", "调整", "规划", "设计", "处理", "解决", "安排", "布置", "比划",
    "培育", "培养", "灌溉", "浇灌", "修剪", "扶持", "推广", "宣传", "维护",
    # More everyday ABAB reduplications: 琢磨琢磨 / 打扮打扮 / 高兴高兴.
    # Missing entries here are not harmless: auto-correct (default on) folded
    # the doubled word into one copy and silently edited the transcript.
    "琢磨", "打扮", "高兴", "热闹", "放松", "欣赏", "享受", "打听", "运动",
    "清理", "参观", "表演", "联系", "汇报", "演示", "测试", "核对", "记录",
    "解释", "说明", "确认", "核实", "排查", "梳理", "复盘",
    "洗澡", "散步", "聊天", "唱歌", "跳舞",
}

# Single characters for which a 2x repetition is almost always a stutter.
STUTTER_CHARS = set("我你他她它这那就是在的和但而也都很不没会能要想说对好个们上下里外来去有无一")

# Measure words: "个个人" means "each person", not a stutter.
_PREFIX_STOP = set("个位名次种些点条只件张座家间每各")

# 数词 + 量词的重叠是「逐一」含义的语法重叠（一个一个 / 一条一条 / 一份一份 /
# 一句一句），不是口吃。只认「数词首字 + 量词尾字」，所以「一定一定」
# 「一起一起」这类真正的重复仍然会报。
_QUANTIFIER_NUMERALS = CJK_DIGITS | set("十百千万亿几半")
# 单一真源：与 cleanup 共用 textutil.MEASURE_CHARS，超集不变量由「同一个对象」保证。
_QUANTIFIER_CLASSIFIERS = MEASURE_CHARS

EXCLAMATIONS = set("啊呀哈呵嘿嗯哦噢喔呃唉哎呦喂啦呢吧吗哟咦唔嘻呼咚咔哗砰呼噜嘀嗒叮当铃喵汪叽喳呱")

MAX_NGRAM = 4
MIN_NGRAM = 2


@lru_cache(maxsize=20000)
def _tone_pinyin(text: str) -> tuple[str, ...]:
    try:
        from pypinyin import Style, lazy_pinyin
        return tuple(lazy_pinyin(text, style=Style.TONE3, errors="ignore"))
    except Exception:
        return tuple()


@lru_cache(maxsize=8192)
def _char_tone(ch: str) -> str:
    p = _tone_pinyin(ch)
    return p[0] if p else ""


def _cjk_runs(text: str) -> Iterable[tuple[int, str]]:
    """Yield (content_start_index, run_string) for contiguous CJK runs."""
    start = 0
    n = len(text)
    while start < n:
        if not is_cjk(text[start]):
            start += 1
            continue
        end = start + 1
        while end < n and is_cjk(text[end]):
            end += 1
        if end - start >= MIN_NGRAM:
            yield start, text[start:end]
        start = end


@lru_cache(maxsize=4096)
def _word_bounds(content: str) -> frozenset[int] | None:
    """Character offsets that fall on a word boundary (cached per sentence).

    Returning None means "no filter" (jieba unavailable): callers then behave
    exactly like the old n-gram scan.
    """
    if jieba is None or not content:
        return None
    bounds = {0, len(content)}
    pos = 0
    for token in jieba.cut(content):
        pos += len(token)
        bounds.add(pos)
    return frozenset(bounds)


def _iter_ngrams(text: str) -> Iterable[tuple[int, int, str]]:
    """Yield (text_start, text_end, gram) so issue offsets index the raw text."""
    positions = content_positions(text)
    content = "".join(text[i] for i in positions)
    bounds = _word_bounds(content)
    for run_start, run in _cjk_runs(content):
        for n in range(MIN_NGRAM, MAX_NGRAM + 1):
            for i in range(0, len(run) - n + 1):
                if bounds is not None:
                    # only n-grams that do not cut through a word
                    if (run_start + i) not in bounds or (run_start + i + n) not in bounds:
                        continue
                yield positions[run_start + i], positions[run_start + i + n - 1] + 1, run[i:i + n]


def _char_key(gram: str) -> tuple[str, ...]:
    return tuple(_char_tone(c) for c in gram)


def find_suspicious(sentences: list[dict], min_count: int = 3,
                    fuzzy: bool = False) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    places: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    texts: dict[int, str] = {}

    for s in sentences:
        sid = s.get("id")
        text = s.get("text") or ""
        # 损坏/旧版转录里可能有没有 id 的句子：跳过而不是 KeyError 把整个
        # 分析（连带每次保存/编辑）打成 500。
        if sid is None or not text:
            continue
        texts[sid] = text
        for start, end, gram in _iter_ngrams(text):
            counts[gram] += 1
            places[gram].append((sid, start, end))

    # group by character-level tone pinyin (fast pre-index), then verify with
    # phrase-level pinyin so polyphonic characters do not create false pairs.
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for gram in counts:
        key = _char_key(gram)
        if key and all(key):
            groups[key].append(gram)

    issues: list[dict] = []
    for key, grams in groups.items():
        if len(grams) < 2:
            continue
        refs = [g for g in grams if counts[g] >= min_count]
        if not refs:
            continue
        refs.sort(key=lambda g: counts[g], reverse=True)
        for ref in refs:
            rc = counts[ref]
            ref_py = _tone_pinyin(ref)
            for var in grams:
                if var == ref:
                    continue
                vc = counts[var]
                if vc > max(1, rc // 4):
                    continue
                if fuzzy:
                    if not _fuzzy_equal(_tone_pinyin(ref), _tone_pinyin(var)):
                        continue
                elif _tone_pinyin(var) != ref_py:
                    continue
                if _char_overlap(ref, var) == len(ref):
                    continue
                ratio = 1.0 - (vc / max(rc, 1))
                score = round(min(0.99, 0.60 + 0.34 * ratio + 0.05 * min(rc / 12.0, 1.0)), 2)
                for sid, start, end in places[var]:
                    if not _looks_like_word(texts.get(sid, ""), start, var):
                        continue
                    issues.append({
                        "kind": "suspicious",
                        "sentence_id": sid,
                        "start": start,
                        "end": end,
                        "text": var,
                        "suggestion": ref,
                        "count": rc,
                        "variant_count": vc,
                        "score": score,
                        "reason": _suspicious_reason(ref, var, rc, vc, fuzzy),
                    })
    # One occurrence can match several n-gram lengths and several references
    # (圣冰 -> 胜兵 / 圣兵). Keep the most specific span per overlapping region
    # so the proofreading list shows one row per suspicious word.
    issues.sort(key=lambda i: (i["sentence_id"], i["start"],
                               -(i["end"] - i["start"]), -i["score"]))
    kept: list[dict] = []
    for issue in issues:
        clash = any(
            k["sentence_id"] == issue["sentence_id"]
            and issue["start"] < k["end"] and k["start"] < issue["end"]
            for k in kept
        )
        if not clash:
            kept.append(issue)
    return kept


def _char_overlap(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x == y)


def _looks_like_word(text: str, start: int, gram: str) -> bool:
    """Reject candidates that start/end on a syntactic seam.

    This is what removes 儿不 (part of 哪儿) or 话的, while keeping real
    word-confusions such as 圣兵 / 胜兵.
    """
    if not gram:
        return False
    if gram[0] in HEAD_STOP or gram[-1] in TAIL_STOP:
        return False
    if start > 0 and text[start - 1].isascii() and text[start - 1].isalnum():
        return False
    return True


def _fuzzy_equal(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if len(a) != len(b) or not a:
        return False
    from .textutil import fuzzy_syllable
    return all(fuzzy_syllable(x.rstrip("012345") or x) == fuzzy_syllable(y.rstrip("012345") or y)
               for x, y in zip(a, b))


def _suspicious_reason(ref: str, var: str, rc: int, vc: int, fuzzy: bool) -> str:
    py = " ".join(s for s in _tone_pinyin(ref))
    py = "".join(" " if ch.isdigit() else ch for ch in py)
    py = " ".join(py.split())
    kind = "近音异写" if fuzzy else "同音异写"
    return f"{kind} · {py} · 全文 {rc} 次，{var} 仅 {vc} 次"


# ------------------------------------------------------------------ repeats

def _prefix_stutter_positions(content: str) -> set[int]:
    """Positions where jieba splits ``X | X...`` (老 | 老师 -> 老老师).

    Character-list heuristics cannot tell 老老师 from 小小年纪 reliably;
    the dictionary segmentation can.
    """
    if jieba is None or not content:
        return set()
    out: set[int] = set()
    pos = 0
    words = list(jieba.cut(content))
    for first, second in zip(words, words[1:]):
        if (
            len(first) == 1
            and is_cjk(first)
            and first not in _PREFIX_STOP
            and len(second) > 1
            and second.startswith(first)
            and is_cjk(second[0])
        ):
            out.add(pos)
        pos += len(first)
    return out


def find_repeats(sentences: list[dict], max_block: int = 6) -> list[dict]:
    issues: list[dict] = []
    for s in sentences:
        if s.get("id") is None:
            continue  # 同 find_suspicious：无 id 的损坏句不参与分析
        text = s.get("text") or ""
        if not text:
            continue
        positions = content_positions(text)
        content = "".join(text[i] for i in positions)
        n = len(content)
        prefix_hits = _prefix_stutter_positions(content)
        covered = [False] * n
        # longest block first, then leftmost
        found: list[tuple[int, int, int]] = []  # (start, end, block_len)
        for L in range(max_block, 0, -1):
            i = 0
            while i + 2 * L <= n:
                if covered[i] or content[i:i + L] != content[i + L:i + 2 * L]:
                    i += 1
                    continue
                block = content[i:i + L]
                if not _repeat_candidate(block, i in prefix_hits):
                    i += 1
                    continue
                reps = 1
                while (i + (reps + 1) * L <= n
                       and content[i + reps * L:i + (reps + 1) * L] == block):
                    reps += 1
                start, end = i, i + reps * L
                if any(covered[j] for j in range(start, end)):
                    i += 1
                    continue
                for j in range(start, end):
                    covered[j] = True
                found.append((start, end, L))
                i = end
        found.sort()
        for start, end, L in found:
            if start >= len(positions) or end - 1 >= len(positions):
                continue
            t_start = positions[start]
            t_end = positions[end - 1] + 1
            raw = text[t_start:t_end]
            if any(ch in "，。！？；：、,.!?;:" for ch in raw):
                # repetition across punctuation is usually deliberate emphasis
                # (第一个自然段，第一个自然段), not a stutter
                continue
            span = content[start:end]
            period = _min_period(span)
            reps = max(1, len(span) // period) if period else 1
            # keep the smallest repeating unit ("这这这这" -> "这")
            keep = span[:period]
            issues.append({
                "kind": "repeat",
                "sentence_id": s["id"],
                "start": t_start,
                "end": t_end,
                "text": raw,
                "suggestion": keep,
                "count": reps,
                "variant_count": 1,
                "score": 0.92,
                "reason": f"口吃重复 · 连续 {reps} 次",
            })
    return issues


def _min_period(text: str) -> int:
    """Smallest block length such that text is that block repeated."""
    n = len(text)
    for p in range(1, n // 2 + 1):
        if n % p == 0 and text == text[:p] * (n // p):
            return p
    return n


def _is_quantifier_reduplication(block: str) -> bool:
    """True for 「数词 + 量词」重叠（一个一个 / 一条一条 / 一句一句）。

    Repeating a classifier phrase is distributive grammar ("one by one"), not
    a stutter.  Only two-character blocks whose head is a Chinese numeral and
    whose tail is a classifier qualify, so 一定一定 / 一起一起 still report.
    """
    if len(block) != 2:
        return False
    return block[0] in _QUANTIFIER_NUMERALS and block[1] in _QUANTIFIER_CLASSIFIERS


def _repeat_candidate(block: str, cross_word: bool = False) -> bool:
    if all(ch in EXCLAMATIONS for ch in block):
        return False
    if block in LEGIT_REDUP or block * 2 in LEGIT_REDUP:
        return False
    if _is_quantifier_reduplication(block):
        # 一个一个 / 一条一条 / 一份一份: grammar, not a stutter.  auto-correct
        # used to fold them into one copy and silently rewrite valid Chinese.
        return False
    if all(ch in CJK_DIGITS for ch in block):
        # 二零二零 / 一九九九 are years read digit by digit, not stutters.
        return False
    if len(block) == 1:
        # ``cross_word`` marks 老老师 / 周周四, where the single char starts
        # one word and the repeated pair starts the next.  Single-syllable
        # verb/time-unit reduplication (理理发 / 打打电话 / 分分钟) is lexical,
        # not a stutter — cleanup protects it via the shared PREFIX_VERB_KEEP,
        # so analysis must too, or default-on auto-correct folds it anyway.
        if cross_word:
            return block not in PREFIX_VERB_KEEP
        # Measure words are never stutters: 个个人 means "each person" and
        # 个个 means "every one".  _prefix_stutter_positions() already skips
        # measure-word heads (that is why they are in _PREFIX_STOP), so
        # ``cross_word`` is False for them — the STUTTER_CHARS fallback used to
        # flag 个 anyway, and auto-correct then deleted one copy ("个个人" ->
        # "个人", "个个都是" -> "个都是") against the documented intent.
        return block in STUTTER_CHARS and block not in _PREFIX_STOP
    return True


# ------------------------------------------------------------------ public

def analyze(transcript: dict[str, Any], settings: dict[str, Any] | None = None,
            include_lowconf: bool = True) -> list[dict]:
    settings = settings or {}
    sentences = transcript.get("sentences") or []
    min_count = int(settings.get("min_ngram_count", 3) or 3)
    fuzzy = bool(settings.get("fuzzy_pinyin", False))

    issues: list[dict] = []
    issues += find_suspicious(sentences, min_count=min_count, fuzzy=fuzzy)
    repeats = find_repeats(sentences)
    issues += repeats

    # suppress suspicious flags that fall inside a repeat span
    rep_spans = [(i["sentence_id"], i["start"], i["end"]) for i in repeats]
    if rep_spans:
        issues = [
            i for i in issues
            if i["kind"] != "suspicious"
            or not any(i["sentence_id"] == sid and i["start"] < e and i["end"] > s
                       for sid, s, e in rep_spans)
        ]

    if include_lowconf:
        for s in sentences:
            if s.get("id") is None:
                continue
            conf = s.get("conf")
            raw = s.get("text") or ""
            # SPEC §3.3：句级 conf<0.55 整句报 lowconf，无长度门槛；start/end 是
            # 原始 text 的字符下标（旧实现 len>=6 漏报短句、end=len(strip) 错位）。
            if conf is not None and conf < 0.55 and raw.strip():
                issues.append({
                    "kind": "lowconf",
                    "sentence_id": s["id"],
                    "start": 0,
                    "end": len(raw),
                    "text": raw,
                    "suggestion": "",
                    "count": 1,
                    "variant_count": 0,
                    "score": round(0.5 - float(conf) / 2.0, 2),
                    "reason": f"识别置信度偏低 · {conf:.2f}",
                })

    issues.sort(key=lambda i: (i["sentence_id"], i["start"], -len(i.get("text") or "")))
    dismissed = set(transcript.get("dismissed") or [])
    for i in issues:
        i["id"] = f"{i['kind']}:{i['sentence_id']}:{i['start']}:{i['end']}"
        i["resolved"] = i["id"] in dismissed
    return [i for i in issues if not i["resolved"]]


def summarize(issues: list[dict]) -> dict[str, int]:
    out = {"suspicious": 0, "repeat": 0, "lowconf": 0, "total": 0}
    for i in issues:
        k = i.get("kind", "")
        if k in out:
            out[k] += 1
        out["total"] += 1
    return out
