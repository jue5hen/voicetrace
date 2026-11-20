import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Sentence, Transcript } from "../types";
import {
  buildPreviewCues,
  buildSubtitleCues,
  cleanSubtitleText,
  contentLen,
  cueAt,
  wrapSubtitle,
} from "./subtitle";
import type { SubtitleCue } from "./subtitle";

/** 逐字时间轴，模拟引擎输出的词级数据。 */
function makeSentence(id: number, text: string, start = 0, step = 200): Sentence {
  const chars = Array.from(text);
  return {
    id,
    start_ms: start,
    end_ms: start + chars.length * step,
    text,
    conf: 0.9,
    edited: false,
    words: chars.map((ch, i) => ({
      text: ch,
      start_ms: start + i * step,
      end_ms: start + (i + 1) * step,
    })),
  };
}

function makeTranscript(sentences: Sentence[], durationMs: number): Transcript {
  return {
    job_id: "test",
    version: 1,
    media: {
      name: "test.mp4",
      path: "D:\\video\\test.mp4",
      duration_ms: durationMs,
      width: 1920,
      height: 1080,
    },
    sentences,
    stats: {
      chars: 0,
      sentences: sentences.length,
      duration_ms: durationMs,
      rtf: 0,
      elapsed_s: 0,
    },
  } as Transcript;
}

const LONG =
  "我们今天就来讲一讲这个比较复杂的问题顺便把上次留下的那个尾巴也一起处理干净再来一遍";

describe("contentLen", () => {
  it("只数内容字符，不数标点与空白", () => {
    assert.equal(contentLen("你好，世界。"), 4);
    assert.equal(contentLen("  a b  c "), 3);
    assert.equal(contentLen("。，！？"), 0);
  });

  it("引号/书名号/省略号等 Unicode 标点与引擎 textutil.is_punct 同口径", () => {
    // 引擎 content_text 会去掉 P/Z/S 三类字符，前端必须一致（SPEC §3.2）。
    assert.equal(contentLen("“你好”"), 2);
    assert.equal(contentLen("《书》"), 1);
    assert.equal(contentLen("他说……好"), 3);
    assert.equal(contentLen("—"), 0);
    assert.equal(contentLen("😀好"), 1);
  });
});

describe("cleanSubtitleText", () => {
  it("默认清理语气词并把标点变成空格", () => {
    assert.equal(cleanSubtitleText("嗯，这个啊，我们就，。、这样"), "这个 我们就 这样");
  });

  it("两个开关都关掉时原样返回", () => {
    const raw = "嗯，这个啊。";
    assert.equal(
      cleanSubtitleText(raw, { subtitle_clean_fillers: false, subtitle_punct_space: false }),
      raw,
    );
  });
});

describe("buildSubtitleCues 不变量", () => {
  it("相邻 cue 绝不重叠（与引擎 clamp_cue_end 同一策略：40ms 间隔优先）", () => {
    const transcript = makeTranscript([makeSentence(0, LONG, 0, 60)], 60000);
    const cues = buildSubtitleCues(transcript, {
      subtitle_max_chars: 22,
      subtitle_merge_gap_ms: 900,
    });

    assert.ok(cues.length > 1, "样例必须能切出多条 cue");
    for (let i = 0; i + 1 < cues.length; i += 1) {
      assert.ok(
        cues[i].end <= cues[i + 1].start,
        "第 " + i + " 条压住了下一条：" + cues[i].start + "-" + cues[i].end +
          " vs " + cues[i + 1].start,
      );
    }
  });

  it("每条 cue 都是正时长且不超过 6.5 秒", () => {
    const transcript = makeTranscript([makeSentence(0, LONG, 0, 120)], 60000);
    const cues = buildSubtitleCues(transcript, { subtitle_max_chars: 22 });
    for (const cue of cues) {
      assert.ok(cue.end > cue.start);
      assert.ok(cue.end - cue.start <= 6500);
    }
  });

  it("最后一条 cue 不越过媒体时长", () => {
    const transcript = makeTranscript([makeSentence(0, "最后一句收尾", 2000, 300)], 3000);
    const cues = buildSubtitleCues(transcript, { subtitle_max_chars: 22 });
    assert.equal(cues.length, 1);
    assert.ok(cues[0].end <= 3000, "末条越界：" + cues[0].start + "-" + cues[0].end);
  });

  it("首字成句（0.1 秒级）时合并，而不是压住下一条", () => {
    const transcript = makeTranscript(
      [makeSentence(0, "好，我们今天就来讲一讲这个比较复杂的问题顺便把上次留下的尾巴处理干净", 0, 60)],
      60000,
    );
    const cues = buildSubtitleCues(transcript, { subtitle_max_chars: 22, subtitle_merge_gap_ms: 900 });
    assert.ok(cues.length >= 1);
    for (let i = 0; i + 1 < cues.length; i += 1) {
      assert.ok(cues[i].end <= cues[i + 1].start, "合并后仍重叠：" + cues[i].end + " > " + cues[i + 1].start);
    }
  });

  it("空转录返回空数组", () => {
    assert.deepEqual(buildSubtitleCues(makeTranscript([], 0), { subtitle_max_chars: 22 }), []);
  });
});

describe("wrapSubtitle", () => {
  it("行宽不超过 maxChars，且不丢内容", () => {
    const text = "我们今天来讲一讲这个比较复杂的问题顺便把上次留下的尾巴处理干净";
    const wrapped = wrapSubtitle(text, 12, 3);
    const lines = wrapped.split("\n");
    assert.ok(lines.length <= 3);
    for (const line of lines) assert.ok(contentLen(line) <= 12);
    assert.equal(contentLen(wrapped), contentLen(text));
  });

  it("短文本原样返回", () => {
    assert.equal(wrapSubtitle("你好", 22, 2), "你好");
  });
});

describe("cueAt（C6：末条结束后不再常驻）", () => {
  const cues: SubtitleCue[] = [
    { start: 0, end: 1000, text: "第一句", sentenceId: 0 },
    { start: 2000, end: 2600, text: "第二句", sentenceId: 1 },
  ];

  it("区间内返回对应 cue（左闭右开）", () => {
    assert.equal(cueAt(cues, 0)?.text, "第一句");
    assert.equal(cueAt(cues, 999)?.text, "第一句");
    assert.equal(cueAt(cues, 2000)?.text, "第二句");
    assert.equal(cueAt(cues, 2599)?.text, "第二句");
  });

  it("末条结束之后返回 null，不再回退末条", () => {
    assert.equal(cueAt(cues, 2600), null);
    assert.equal(cueAt(cues, 7000), null);
  });

  it("条间空隙与空列表都返回 null", () => {
    assert.equal(cueAt(cues, 1500), null);
    assert.equal(cueAt([], 0), null);
  });

  it("媒体播放到片尾时预览不再显示字幕（与导出 cue 结束一致）", () => {
    const transcript = makeTranscript([makeSentence(0, "最后一句收尾", 0, 200)], 5000);
    const preview = buildPreviewCues(transcript, {
      subtitle_max_chars: 22,
      subtitle_max_lines: 1,
      subtitle_merge_gap_ms: 900,
    });
    assert.equal(preview.length, 1);
    assert.ok(cueAt(preview, 10) !== null, "句内应当显示");
    assert.equal(cueAt(preview, preview[0].end), null);
    assert.equal(cueAt(preview, 5000), null);
  });
});

describe("buildPreviewCues", () => {
  it("给出字数、语速与行数", () => {
    const transcript = makeTranscript([makeSentence(0, "你好世界", 0, 250)], 5000);
    const cues = buildPreviewCues(transcript, {
      subtitle_max_chars: 22,
      subtitle_max_lines: 2,
      subtitle_merge_gap_ms: 900,
    });
    assert.equal(cues.length, 1);
    assert.equal(cues[0].chars, 4);
    assert.equal(cues[0].lines, 1);
    assert.ok(cues[0].cps > 0);
  });
});
