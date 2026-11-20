import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Issue } from "../types";
import {
  basename,
  buildSubtitle,
  confTone,
  extname,
  fmtAssTime,
  fmtBytes,
  fmtClock,
  fmtDuration,
  fmtSrtTime,
  fmtVttTime,
  formatLabel,
  stageLabel,
  statusLabel,
  statusTone,
  summarize,
} from "./format";
import { buildSubtitleCues } from "./subtitle";
import type { Sentence, Transcript } from "../types";

describe("时间码", () => {
  it("fmtClock 紧凑时钟", () => {
    assert.equal(fmtClock(0), "00:00.0");
    assert.equal(fmtClock(1234), "00:01.2");
    assert.equal(fmtClock(3661500), "1:01:01.5");
    assert.equal(fmtClock(1234, false), "00:01");
  });

  it("fmtDuration 元信息时长", () => {
    assert.equal(fmtDuration(0), "--:--");
    assert.equal(fmtDuration(-1), "--:--");
    assert.equal(fmtDuration(2523000), "42:03");
    assert.equal(fmtDuration(3723000), "1:02:03");
  });

  it("SRT / VTT / ASS 时间码格式", () => {
    assert.equal(fmtSrtTime(3723456), "01:02:03,456");
    assert.equal(fmtVttTime(3723456), "01:02:03.456");
    assert.equal(fmtAssTime(3723456), "1:02:03.46");
    assert.equal(fmtSrtTime(-5), "00:00:00,000");
  });
});

describe("其它格式化", () => {
  it("fmtBytes 单位与精度", () => {
    assert.equal(fmtBytes(0), "—");
    assert.equal(fmtBytes(1023), "1023 B");
    assert.equal(fmtBytes(1024), "1.0 KB");
    assert.equal(fmtBytes(1536), "1.5 KB");
    assert.equal(fmtBytes(2 * 1024 * 1024 * 1024), "2.0 GB");
  });

  it("basename / extname 处理 Windows 与 POSIX 路径", () => {
    assert.equal(basename("D:\\video\\第01讲.mp4"), "第01讲.mp4");
    assert.equal(basename("/home/u/a.mkv"), "a.mkv");
    assert.equal(extname("D:\\video\\第01讲.MP4"), "mp4");
    assert.equal(extname("noext"), "");
  });
});

describe("状态与阶段文案（契约）", () => {
  it("stage=load 必须显示为「加载模型」", () => {
    assert.equal(stageLabel("load"), "加载模型");
    assert.equal(stageLabel("probe"), "读取媒体");
    assert.equal(stageLabel("asr"), "语音识别");
  });

  it("状态与色调映射", () => {
    assert.equal(statusLabel("canceled"), "已取消");
    assert.equal(statusLabel("done"), "已完成");
    assert.equal(statusTone("done"), "ok");
    assert.equal(statusTone("processing"), "accent");
    assert.equal(statusTone("error"), "err");
    assert.equal(statusTone("canceled"), "muted");
  });

  it("置信度分档", () => {
    assert.equal(confTone(0.9), "ok");
    assert.equal(confTone(0.6), "warn");
    assert.equal(confTone(0.4), "err");
  });

  it("导出格式标签", () => {
    assert.equal(formatLabel("srt"), "SRT");
    assert.equal(formatLabel("json"), "JSON");
  });
});

describe("summarize", () => {
  it("只统计未解决的提示", () => {
    const issues: Issue[] = [
      { id: "a", kind: "suspicious", sentence_id: 0, start: 0, end: 1, text: "x", suggestion: "", count: 1, variant_count: 0, score: 1, reason: "", resolved: false },
      { id: "b", kind: "repeat", sentence_id: 0, start: 0, end: 1, text: "y", suggestion: "", count: 1, variant_count: 0, score: 1, reason: "", resolved: true },
      { id: "c", kind: "lowconf", sentence_id: 0, start: 0, end: 1, text: "z", suggestion: "", count: 1, variant_count: 0, score: 1, reason: "", resolved: false },
    ];
    assert.deepEqual(summarize(issues), { suspicious: 1, repeat: 0, lowconf: 1, total: 2 });
  });
});

describe("buildSubtitle（浏览器回退导出）", () => {
  const sentence: Sentence = {
    id: 0,
    start_ms: 0,
    end_ms: 1200,
    text: "你好世界",
    conf: 0.9,
    edited: false,
    words: Array.from("你好世界").map((ch, i) => ({
      text: ch,
      start_ms: i * 250,
      end_ms: (i + 1) * 250,
    })),
  };
  const transcript = {
    job_id: "j1",
    version: 1,
    media: { name: "m.mp4", path: "D:\\m.mp4", duration_ms: 5000, width: 0, height: 0 },
    sentences: [sentence],
    stats: { chars: 4, sentences: 1, duration_ms: 5000, rtf: 0, elapsed_s: 0 },
  } as Transcript;

  it("SRT 有递增序号、箭头时间码与空行分隔", () => {
    const srt = buildSubtitle("srt", transcript, null);
    const cues = buildSubtitleCues(transcript, null);
    assert.equal(cues.length, 1);
    assert.ok(srt.startsWith("1\n"));
    assert.ok(srt.includes(" --> "));
    assert.ok(srt.includes("你好世界"));
  });

  it("VTT 以 WEBVTT 头开始", () => {
    assert.ok(buildSubtitle("vtt", transcript, null).startsWith("WEBVTT"));
  });

  it("ASS 含样式段与 Dialogue 行", () => {
    const ass = buildSubtitle("ass", transcript, null);
    assert.ok(ass.includes("[V4+ Styles]"));
    assert.ok(ass.includes("Dialogue: 0,"));
  });

  it("JSON 导出可解析且带 stats", () => {
    const json = JSON.parse(buildSubtitle("json", transcript, null)) as {
      job_id: string;
      stats: { chars: number };
      sentences: { words: { text: string; start_ms: number; end_ms: number }[] }[];
    };
    assert.equal(json.job_id, "j1");
    assert.equal(json.stats.chars, 4);
    assert.equal(json.sentences.length, 1);
    // 与引擎 JSON 一致：浏览器回退也必须带词级时间轴。
    assert.deepEqual(json.sentences[0].words, sentence.words);
  });

  it("未知格式返回空串", () => {
    assert.equal(buildSubtitle("bogus" as never, transcript, null), "");
  });
});
