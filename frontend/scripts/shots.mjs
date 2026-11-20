#!/usr/bin/env node
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const root = fileURLToPath(new URL("..", import.meta.url));
const outDir = path.join(root, ".shots");
mkdirSync(outDir, { recursive: true });

const BASE = process.env.PREVIEW_URL || "http://127.0.0.1:4173";
const EDGE =
  process.env.EDGE_PATH ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const errors = [];
const browser = await chromium.launch({
  executablePath: EDGE,
  headless: true,
  args: ["--force-color-profile=srgb", "--hide-scrollbars"],
});
const page = await browser.newPage({
  viewport: { width: 1600, height: 900 },
  deviceScaleFactor: 1,
  locale: "zh-CN",
});
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console: ${m.text()}`);
});

async function shot(name) {
  await page.waitForTimeout(260);
  const file = path.join(outDir, name);
  await page.screenshot({ path: file });
  console.log(`shot ${name}`);
}

async function main() {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(850);
  await shot("01-boot.png");

  await page.waitForSelector(".empty-page", { timeout: 30000 });
  await page.waitForTimeout(450);
  await shot("02-empty.png");

  await page.locator(".app-nav-settings").click();
  await page.waitForSelector(".drawer");
  await shot("03-settings.png");
  await page.locator('.drawer-head button[aria-label="关闭设置"]').click();
  await page.waitForTimeout(200);

  const recent = page.locator(".recent-card").filter({ hasText: "第01讲" }).first();
  if (await recent.count()) {
    await recent.click();
  } else {
    await page.locator(".app-nav-item").filter({ hasText: "批量转录" }).click();
    await page.waitForSelector(".queue-row");
    await page.locator(".queue-row").first().click();
  }
  await page.waitForSelector(".editor", { timeout: 10000 });
  await page.waitForTimeout(900);
  await shot("04-editor.png");

  const card = page.locator(".issue-card").first();
  if (await card.count()) {
    await card.scrollIntoViewIfNeeded();
    await card.hover();
    await shot("05-editor-issues.png");
  }

  const preview = page.locator(".preview-trigger");
  if (await preview.count()) {
    await preview.click();
    await page.waitForSelector(".preview-modal");
    await page.waitForTimeout(350);
    await shot("06-preview.png");
    await page.locator('button[aria-label="关闭字幕预览"]').click();
    await page.waitForTimeout(200);
  }

  await page.locator(".app-nav-item").filter({ hasText: "批量转录" }).click();
  await page.waitForSelector(".queue-page", { timeout: 10000 });
  await page.waitForTimeout(400);
  await shot("07-queue.png");

  const failed = page.locator(".queue-row.tone-error").first();
  if (await failed.count()) {
    await failed.click();
    await page.waitForSelector(".error-card, .editor", { timeout: 10000 });
    await page.waitForTimeout(400);
    await shot("08-error.png");
  }

  const overflow = await page.evaluate(() => ({
    docW: document.documentElement.scrollWidth,
    winW: window.innerWidth,
    docH: document.documentElement.scrollHeight,
    winH: window.innerHeight,
  }));
  console.log("viewport", JSON.stringify(overflow));
  console.log("errors", errors.length ? JSON.stringify(errors, null, 2) : "none");
}

try {
  await main();
} finally {
  await browser.close();
}
