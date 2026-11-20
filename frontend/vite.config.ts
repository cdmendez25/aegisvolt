import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En desarrollo, /graphql se redirige al gateway local (scripts/run-local.sh).
// En Kubernetes, el Ingress enruta /graphql del MISMO host al gateway del ambiente.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/graphql": { target: process.env.GRAPHQL_PROXY ?? "http://localhost:8100", ws: true } },
  },
});
