import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/graphql": { target: process.env.GRAPHQL_PROXY ?? "http://localhost:8100", ws: true } },
  },
});
