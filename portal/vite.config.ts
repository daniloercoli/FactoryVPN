import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Only the public label is exposed to the client; deployment secrets stay private.
  const rawName = loadEnv(mode, process.cwd(), "APP_NAME").APP_NAME ?? "FactoryVPN";
  const appName = rawName.trim();
  if (
    /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(rawName) ||
    !appName ||
    Array.from(appName).length > 80
  ) {
    throw new Error(
      "APP_NAME must contain 1–80 characters on a single line, without control characters",
    );
  }
  const htmlName = appName.replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#x27;",
      })[char]!,
  );
  return {
    plugins: [
      react(),
      {
        name: "application-name",
        transformIndexHtml: {
          order: "post" as const,
          handler: (html: string) =>
            html.replace("__APP_NAME_HTML__", () => htmlName),
        },
      },
    ],
    define: { __APP_NAME__: JSON.stringify(appName) },
    base: "/app/",
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: process.env.FACTORYVPN_API_URL || "http://127.0.0.1:8000",
        },
      },
    },
    build: { sourcemap: false },
  };
});
