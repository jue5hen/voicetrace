#!/usr/bin/env node
/**
 * 前端测试入口：Node 原生 test runner + 一个 TS 解析钩子。
 *
 * 不用 vitest 的原因：vitest 会加载 node_modules 里的依赖，而交付路径
 * `09-VoiceTrace/#source/frontend` 含 "#"，Vite/vite-node 会把路径当 URL 片段
 * 截断（EISDIR: ... 09-VoiceTrace/）。node:test 只按文件系统路径解析，
 * 配合 scripts/ts-resolve.mjs 补 `./subtitle` 这类无扩展名相对导入即可，
 * 零依赖、不受 Vite 版本影响。
 *
 * 用法：
 *   node scripts/test.mjs                     跑 src 下所有 *.test.ts
 *   node scripts/test.mjs src/lib/x.test.ts   只跑指定文件
 */
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const project = fileURLToPath(new URL("..", import.meta.url));

function collect(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) collect(full, out);
    else if (entry.name.endsWith(".test.ts")) out.push(full);
  }
  return out;
}

const explicit = process.argv.slice(2).filter((a) => !a.startsWith("-"));
const files = (
  explicit.length ? explicit.map((f) => path.resolve(project, f)) : collect(path.join(project, "src"))
).sort();
if (!files.length) {
  console.error("[test] 没有找到测试文件");
  process.exit(1);
}

const register = pathToFileURL(path.join(project, "scripts", "register-ts.mjs")).href;
const result = spawnSync(
  process.execPath,
  ["--import", register, "--test", "--test-reporter=spec", ...files],
  { cwd: project, stdio: "inherit", env: process.env },
);
process.exit(result.status ?? 1);
