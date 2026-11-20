#!/usr/bin/env node
/**
 * 构建脚本：把源码复制到不含 “#” 的临时目录后再运行 Vite。
 *
 * 项目交付目录固定为 `#source/frontend`，而 Vite 会把根路径里的 “#” 当成
 * URL 片段，导致模块 id 被截断（Could not resolve "./App"）。tsc 不受影响，
 * 因此生产构建在临时镜像目录中完成，再把 dist 拷回项目。
 */
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const project = fileURLToPath(new URL("..", import.meta.url));
// 工作区边界：临时镜像必须落在 <项目>/_scratch/<日期>/，不再写到系统 Temp。
const workspace = path.resolve(project, "..", "..");
const scratch = path.join(
  workspace,
  "_scratch",
  new Date().toISOString().slice(0, 10),
);
mkdirSync(scratch, { recursive: true });
const stage = path.join(
  scratch,
  `voicetrace-build-${process.pid}-${Date.now()}`,
);
const distOut = path.join(project, "dist");

function prepare() {
  rmSync(stage, { recursive: true, force: true });
  mkdirSync(stage, { recursive: true });

  copyFileSync(path.join(project, "index.html"), path.join(stage, "index.html"));
  copyFileSync(
    path.join(project, "vite.config.ts"),
    path.join(stage, "vite.config.ts"),
  );
  cpSync(path.join(project, "src"), path.join(stage, "src"), {
    recursive: true,
  });
  for (const name of [".env", ".env.production", ".env.local"]) {
    if (existsSync(path.join(project, name))) {
      copyFileSync(path.join(project, name), path.join(stage, name));
    }
  }
  if (existsSync(path.join(project, "public"))) {
    cpSync(path.join(project, "public"), path.join(stage, "public"), {
      recursive: true,
    });
  }
  symlinkSync(
    path.join(project, "node_modules"),
    path.join(stage, "node_modules"),
    "junction",
  );
}

async function main() {
  prepare();
  const { build } = await import("vite");
  await build({
    root: stage,
    configFile: path.join(stage, "vite.config.ts"),
    logLevel: "info",
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  });

  if (!existsSync(path.join(stage, "dist", "index.html"))) {
    throw new Error("构建结束但未找到 dist/index.html");
  }
  try {
    rmSync(distOut, { recursive: true, force: true });
  } catch {
    // Windows 上预览进程可能短暂占用目录；保留旧文件，直接覆盖新产物。
    mkdirSync(distOut, { recursive: true });
  }
  cpSync(path.join(stage, "dist"), distOut, { recursive: true, force: true });
  console.log(`\n✓ dist 已生成：${distOut}`);
}

try {
  await main();
} finally {
  rmSync(stage, { recursive: true, force: true });
}
