/**
 * 让 node 原生测试能直接跑 TS 源码：
 * TypeScript 源码用「无扩展名」的相对导入（`./subtitle`），Node 的 ESM 解析要求
 * 完整文件名，这个钩子补上 .ts/.tsx/index 的候选。
 * 其余解析一律交回 Node 默认实现。
 */
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const CANDIDATES = [".ts", ".tsx", ".mts", "/index.ts", "/index.tsx"];

export async function resolve(specifier, context, next) {
  const relative = specifier.startsWith("./") || specifier.startsWith("../");
  if (relative && !/\.[cm]?[jt]sx?$/i.test(specifier)) {
    try {
      const base = new URL(specifier, context.parentURL);
      for (const ext of CANDIDATES) {
        const url = new URL(base.href + ext);
        if (existsSync(fileURLToPath(url))) return next(url.href, context);
      }
    } catch {
      // 交给默认解析器给出原始错误
    }
  }
  return next(specifier, context);
}
