import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is proxied rather than called cross-origin. Two reasons: the browser
// then sends same-origin requests so CORS never enters the picture during dev,
// and the production build can be served by the same host without changing a
// single fetch URL. api/main.py's CORS list is for the browser extension,
// which genuinely is cross-origin; the dashboard should not need it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // 127.0.0.1, NOT localhost. Node 18+ resolves localhost to IPv6 ::1
        // first, and uvicorn binds IPv4 only by default, so the proxy gets
        // ECONNREFUSED ::1:8000 and returns a 500 while the API sits there
        // answering direct requests perfectly. Being explicit costs nothing
        // and removes a confusing failure mode.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
