#!/usr/bin/env node
/**
 * 预览 dist 的静态服务器（默认 127.0.0.1:4173）。
 *
 * 1. 不使用 vite preview：项目目录名含 “#”，Vite 的 URL 解析会截断路径。
 * 2. 真实引擎的 /health 没有 CORS 头，且端口每次启动可能变化。这里把 /api
 *    与 /api/ws 反向代理到 _runtime/port.json 指向的本机端口，前端因此可用
 *    相对路径（VITE_API_BASE=/）访问，避免跨源与硬编码端口。
 */
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL("..", import.meta.url));
const root = path.join(here, "dist");
const port = Number(process.env.PORT || 4173);
const portFile = path.join(here, "..", "..", "_runtime", "port.json");
const engineToken = process.env.ENGINE_TOKEN || "devtoken";
const engineAuth = `Bearer ${engineToken}`;

function rewriteToken(rawUrl) {
  try {
    const url = new URL(rawUrl || "/", "http://127.0.0.1");
    if (url.searchParams.has("token")) url.searchParams.set("token", engineToken);
    return `${url.pathname}${url.search}`;
  } catch {
    return rawUrl;
  }
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".woff2": "font/woff2",
  ".mp4": "video/mp4",
  ".m4a": "audio/mp4",
};

function enginePort() {
  try {
    const data = JSON.parse(readFileSync(portFile, "utf8"));
    const value = Number(data.port);
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function proxyHttp(req, res) {
  const target = enginePort();
  if (!target) {
    res.writeHead(503, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: { code: "no_engine", message: "尚未找到 _runtime/port.json" } }));
    return;
  }
  const upstream = http.request(
    {
      host: "127.0.0.1",
      port: target,
      path: rewriteToken(req.url),
      method: req.method,
      headers: {
        ...req.headers,
        host: `127.0.0.1:${target}`,
        authorization: engineAuth,
      },
      timeout: 300_000,
    },
    (up) => {
      res.writeHead(up.statusCode || 502, up.headers);
      up.pipe(res);
    },
  );
  upstream.on("timeout", () => upstream.destroy(new Error("engine timeout")));
  upstream.on("error", (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    }
    res.end(JSON.stringify({ error: { code: "proxy_failed", message: String(err.message || err) } }));
  });
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  try {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname.startsWith("/api/")) {
      proxyHttp(req, res);
      return;
    }
    let rel = decodeURIComponent(url.pathname);
    if (rel === "/") rel = "/index.html";
    let file = path.join(root, rel);
    if (!file.startsWith(root)) {
      res.writeHead(403).end("forbidden");
      return;
    }
    if (!existsSync(file) || !statSync(file).isFile()) {
      file = path.join(root, "index.html");
    }
    res.writeHead(200, {
      "Content-Type": MIME[path.extname(file)] || "application/octet-stream",
      "Cache-Control": "no-store",
    });
    createReadStream(file).pipe(res);
  } catch (err) {
    res.writeHead(500).end(String(err));
  }
});

server.on("upgrade", (req, socket, head) => {
  const target = enginePort();
  if (!target) {
    socket.destroy();
    return;
  }
  const upstream = net.connect(target, "127.0.0.1", () => {
    const lines = [`GET ${rewriteToken(req.url)} HTTP/1.1`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      const name = req.rawHeaders[i];
      const value = req.rawHeaders[i + 1];
      if (name.toLowerCase() === "authorization") continue;
      lines.push(`${name}: ${name.toLowerCase() === "host" ? `127.0.0.1:${target}` : value}`);
    }
    lines.push(`Authorization: ${engineAuth}`);
    upstream.write(`${lines.join("\r\n")}\r\n\r\n`);
    if (head?.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
});

server.listen(port, "127.0.0.1", () => {
  console.log(`preview: http://127.0.0.1:${port}  (proxy -> ${portFile})`);
});
