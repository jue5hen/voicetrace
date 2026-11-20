/**
 * 过期回包判定（C3）。
 *
 * transcript / history 这类读请求会带一个单调递增的请求序号：回包到达时，
 * 只要序号已不是最新（期间又发起了新请求、或写操作把在途请求作废），
 * 或者发起请求时的任务已经被切走，就必须整份丢弃，不能写回当前面板。
 */
export function isStaleResponse(
  requestSeq: number,
  currentSeq: number,
  requestJobId: string,
  currentJobId: string | null,
): boolean {
  return requestSeq !== currentSeq || currentJobId !== requestJobId;
}
