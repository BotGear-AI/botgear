import fs from 'node:fs'
import path from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// Modular service UIs (repo/<name>/<version>/ui) are built into a standalone
// ui.js and loaded at runtime via dynamic import() — they are NOT part of this
// host app's module graph. When Vite sees one of their source files change
// (it watches them because Tailwind scans repo/**/ui for classes) it can't HMR
// them, so it falls back to a FULL PAGE RELOAD. That reload drops the live
// WebSocket and blanks any open scope/trace for the seconds it takes to
// reconnect + re-subscribe. This plugin suppresses the reload for those files;
// Tailwind still regenerates their classes on its own content-watch.
function noReloadOnServiceUi(): Plugin {
  return {
    name: 'rlx-no-reload-on-service-ui',
    handleHotUpdate(ctx) {
      if (/[/\\]repo[/\\][^/\\]+[/\\][^/\\]+[/\\]ui[/\\]/.test(ctx.file)) return []
    },
  }
}

function readApiTargetFromEnvFiles(mode: string): string {
  const root = process.cwd()
  const candidates = [`.env.${mode}.local`, `.env.${mode}`, '.env.local', '.env']
  for (const filename of candidates) {
    const fullPath = path.join(root, filename)
    if (!fs.existsSync(fullPath)) continue
    const content = fs.readFileSync(fullPath, 'utf8')
    const match = content.match(/^VITE_API_URL\s*=\s*(.+)\s*$/m)
    if (!match) continue
    return match[1].trim().replace(/^['"]|['"]$/g, '')
  }
  return ''
}

export default defineConfig(({ mode }) => {
  // Default to https://localhost:8998 — the backend serves TLS (self-signed)
  // when ssl is enabled. The .env file in this directory (VITE_API_URL=...)
  // overrides this fallback (set it to http://… if you run the backend
  // without ssl).
  const apiTarget = readApiTargetFromEnvFiles(mode) || 'https://localhost:8998'

  // Serve the dev UI over HTTPS (self-signed) so WebXR works from a headset —
  // WebXR requires a secure context, which a plain-HTTP LAN origin is not.
  // Reuses the backend's self-signed cert (apps/botgear/ssl). Falls back to
  // HTTP if the cert isn't there (certless checkout / ssl disabled).
  const sslDir = path.resolve(process.cwd(), '..', 'botgear', 'ssl')
  const certPath = path.join(sslDir, 'cert.pem')
  const keyPath = path.join(sslDir, 'key.pem')
  const https = fs.existsSync(certPath) && fs.existsSync(keyPath)
    ? { cert: fs.readFileSync(certPath), key: fs.readFileSync(keyPath) }
    : undefined

  return {
    plugins: [react(), noReloadOnServiceUi()],
    // Pre-bundle the singletons that modular service UIs (Option B) share
    // via window.__RLX__ + the import map. Listing them here keeps Vite
    // from re-optimizing mid-session (which 504s in-flight modules) when
    // main.tsx adds an explicit import of one. See
    // docs/TODO_SERVICE_UI_BUNDLES.md.
    optimizeDeps: {
      include: ['react', 'react-dom', 'react/jsx-runtime'],
    },
    server: {
      // Listen on all interfaces (not just localhost) so the dev UI is
      // reachable from other devices on the LAN — e.g. a Quest headset
      // hitting /r/<runtime>/xr/<proxy> for WebXR.
      host: '0.0.0.0',
      allowedHosts: true,
      // Self-signed TLS so the LAN origin is a secure context (WebXR needs it).
      https,
      port: 5051,
      // Fail fast instead of auto-picking 5052 when 5051 is busy. We
      // bookmark http://localhost:5051 elsewhere and silently shifting
      // the port breaks those.
      strictPort: true,
      proxy: {
        // ws: true is required for the WebSocket upgrade at /v1/ws to
        // actually negotiate — without it the proxy treats the upgrade
        // as a regular HTTP request, the handshake never completes,
        // and the indicator stays stuck on "connecting" forever.
        // secure: false tells the proxy to accept the backend's self-signed
        // TLS cert (harmless when the target is plain http).
        '/v1': { target: apiTarget, changeOrigin: true, ws: true, secure: false },
        '/openapi.json': { target: apiTarget, changeOrigin: true, secure: false },
        // Repo-served service icons (GET /repo/<name>/<version>/icon).
        // Without this rule the SPA's catch-all would serve index.html
        // and the <img> would silently fall back to the Package default.
        '/repo': { target: apiTarget, changeOrigin: true, secure: false },
      },
    },
  }
})
