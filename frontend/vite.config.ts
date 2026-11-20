import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端只与本地引擎通信，产物直接交给 Tauri 壳加载。
// 使用相对 base，兼容 tauri:// 与本地文件协议加载 dist。
export default defineConfig({
  base: "./",
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 浏览器开发时走同源 /api，由 Vite 转发到本机引擎。
      "/api": {
        target: process.env.VT_ENGINE_URL || "http://127.0.0.1:8765",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    target: "chrome120",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
