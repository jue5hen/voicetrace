import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isStaleResponse } from "./race";

describe("过期 transcript 回包判定（C3）", () => {
  it("序号落后就丢弃", () => {
    assert.equal(isStaleResponse(4, 5, "job-a", "job-a"), true);
    assert.equal(isStaleResponse(5, 5, "job-a", "job-a"), false);
  });

  it("序号最新但任务已切走也要丢弃", () => {
    assert.equal(isStaleResponse(5, 5, "job-a", "job-b"), true);
    assert.equal(isStaleResponse(5, 5, "job-a", null), true);
  });

  it("写操作作废在途加载后，回包必然过期", () => {
    // 发起加载时序号 3，写操作成功后 loadSeq 变成 4，加载回包必须丢弃。
    assert.equal(isStaleResponse(3, 4, "job-a", "job-a"), true);
  });
});
