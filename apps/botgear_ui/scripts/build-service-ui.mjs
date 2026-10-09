#!/usr/bin/env node
// Framework-owned builder for modular service UIs (Option B —
// docs/TODO_SERVICE_UI_BUNDLES.md). Compiles a service's ui/View.tsx into
// a single self-contained ui/dist/ui.js (react / react-dom / @rlx/ui
// externalized; the view's own deps + CSS bundled/inlined in).
//
// If the service ALSO has ui/xr/View.tsx, a second self-contained
// ui/dist/xr.js is emitted the same way — used for full-page immersive
// WebXR clients (opened on a headset) that the desktop panel shouldn't
// pay the bundle cost for.
//
//   node scripts/build-service-ui.mjs <abs-or-rel path to a service ui/ dir>
//
// e.g. node scripts/build-service-ui.mjs ../botgear/repo/cli/1.0.0/ui
import { build } from 'vite'
import { resolve, isAbsolute } from 'node:path'
import { existsSync, readdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import react from '@vitejs/plugin-react'
import { esmExternalRequirePlugin } from 'rolldown/plugins'

const arg = process.argv[2]
if (!arg) {
  console.error('usage: node scripts/build-service-ui.mjs <service ui/ dir>')
  process.exit(1)
}
const uiDir = isAbsolute(arg) ? arg : resolve(process.cwd(), arg)
const outDir = resolve(uiDir, 'dist')

// Optional per-bundle module aliases, declared in the bundle's
// package.json under `rlx.aliases` ({ specifier: relativePath }). Lets a
// bundle stub a heavy optional dependency it never executes (e.g. webxr
// aliases the IWER emulator packages to a no-op so ~7MB of synthetic
// environments stay out of xr.js). Paths resolve against the ui/ dir.
let aliases = {}
let xrStandalone = false
try {
  const pkg = JSON.parse(readFileSync(resolve(uiDir, 'package.json'), 'utf8'))
  const raw = pkg?.rlx?.aliases
  if (raw && typeof raw === 'object') {
    for (const [spec, rel] of Object.entries(raw)) aliases[spec] = resolve(uiDir, rel)
  }
  // Opt-in, not a default — most services' xr.js (if they even have one)
  // has no react-reconciler-vendoring dependency and is fine sharing the
  // host's React singleton like ui.js does. See the `standalone` param
  // doc on buildEntry for why webxr needs this.
  xrStandalone = pkg?.rlx?.xrStandalone === true
} catch { /* no package.json / no aliases — fine */ }
const mainEntry = resolve(uiDir, 'View.tsx')
if (!existsSync(mainEntry)) {
  console.error(`no View.tsx at ${mainEntry}`)
  process.exit(1)
}

// Build one entry → <outDir>/<outName>, externalizing the host singletons
// and inlining any emitted CSS into the JS so each artifact is a single
// self-contained module the host loader can import by URL.
//
// `standalone: true` (opt-in per entry, via the bundle's package.json —
// see `rlx.standalone` below) skips ALL of that externalization and
// bundles react/react-dom/@rlx/ui's runtime pieces in directly instead,
// so the entry becomes a fully self-contained React tree with its OWN
// react/react-dom rather than sharing the host's singleton. Use this for
// an entry whose dependency tree vendors its own react-reconciler (e.g.
// @react-three/fiber) — sharing the host's React singleton across that
// boundary means the host's react-dom patch version and the vendored
// reconciler's version must independently agree on React's UNSTABLE
// internals shape (ReactSharedInternals.S/.use, dispatcher.getOwner, …),
// which broke repeatedly in practice (webxr/1.0.0/xr.js, 2026-09) as the
// two drifted apart between otherwise-compatible-looking semver ranges.
// A standalone entry's default export must be a plain mount function
// `(container, props) => unmountFn`, not a component — see
// webxr/1.0.0/ui/xr/View.tsx for the pattern.
async function buildEntry(entry, outName, emptyOutDir, { standalone = false } = {}) {
  await build({
    configFile: false,
    plugins: [react()],
    resolve: { alias: aliases },
    // Bundled deps (e.g. react-three-fiber's react-reconciler) reference
    // process.env.NODE_ENV for dev-only warnings. The browser has no
    // `process`, so without replacement the bundle throws "process is not
    // defined" at load. Pin NODE_ENV to production (dead-codes dev branches)
    // and stub the rest of process.env to {}.
    define: {
      'process.env.NODE_ENV': JSON.stringify('production'),
      'process.env': '{}',
    },
    publicDir: false,
    build: {
      outDir,
      emptyOutDir,
      copyPublicDir: false,
      lib: { entry, formats: ['es'], fileName: () => outName },
      rollupOptions: {
        // react/react-dom/react/jsx-runtime are NOT listed here — they're
        // owned entirely by esmExternalRequirePlugin below (listing a
        // specifier in both places is a silent no-op conflict: the plugin
        // warns "duplicate external" and skips rewriting it, so the
        // require() bug below goes unfixed even though the plugin is
        // wired in). @rlx/ui has no CJS-require call sites so it stays a
        // plain external here. NONE of this applies to a `standalone`
        // entry — nothing is external, everything (including react/
        // react-dom) gets bundled in as this entry's own private copy.
        external: standalone ? [] : ['@rlx/ui'],
        // A view's own deps get bundled in — and some of those (e.g.
        // react-three-fiber's react-reconciler, used by webxr's xr.js)
        // ship CommonJS-only and call require('react') internally. Since
        // react/react-dom need to be external anyway (so the bundle shares
        // the HOST's single React instance instead of a second copy —
        // which would break hooks), that inner require() can't be
        // statically rewritten to the external ESM import by default;
        // it's left as a runtime require() call that throws in a browser
        // ("...environment that doesn't expose the require function").
        // This builtin plugin both externalizes AND rewrites require()
        // calls for exactly these specifiers into the same ESM import.
        // See https://rolldown.rs/builtin-plugins/esm-external-require
        // A standalone entry needs none of this — react/react-dom are
        // bundled in directly, so an inner require('react') resolves to
        // that same local copy via completely normal CJS interop.
        plugins: standalone ? [] : [esmExternalRequirePlugin({ external: ['react', 'react-dom', 'react/jsx-runtime'] })],
        // The host loads a bundle as a SINGLE ESM by URL
        // (/repo/<name>/<version>/<outName>); it can't resolve sibling
        // chunk files. Inline every dynamic import so each artifact stays
        // one self-contained module (matters for deps that code-split,
        // e.g. @react-three/xr's emulator).
        output: { inlineDynamicImports: true },
      },
      minify: false,
      target: 'es2022',
    },
  })

  // Inline emitted CSS INTO <outName> as an injected <style>, then delete
  // the css file — so the artifact is a single self-contained module.
  const cssFiles = readdirSync(outDir).filter((f) => f.endsWith('.css'))
  const jsPath = resolve(outDir, outName)
  if (cssFiles.length) {
    const css = cssFiles.map((f) => readFileSync(resolve(outDir, f), 'utf8')).join('\n')
    const js = readFileSync(jsPath, 'utf8')
    const inject =
      `(()=>{try{if(typeof document!=='undefined'){` +
      `const s=document.createElement('style');` +
      `s.setAttribute('data-rlx-bundle-css','');` +
      `s.textContent=${JSON.stringify(css)};` +
      `document.head.appendChild(s);}}catch(e){}})();\n`
    writeFileSync(jsPath, inject + js)
    for (const f of cssFiles) rmSync(resolve(outDir, f))
    console.log(`  inlined ${cssFiles.join(', ')} (${css.length} bytes) into ${outName}`)
  }

  // Generated-file banner: this is esbuild output (bundled third-party
  // code, vendor license headers, no source map back to View.tsx as
  // authored) — never hand-edit it, and a linter pointed at it directly
  // will always be unhappy. `eslint-disable` + `@ts-nocheck` cover any
  // linter/language-server that respects inline directives; GitHub and
  // most tooling also recognize the "GENERATED FILE" convention.
  const banner =
    `// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.\n` +
    `/* eslint-disable */\n` +
    `// @ts-nocheck\n`
  writeFileSync(jsPath, banner + readFileSync(jsPath, 'utf8'))

  console.log(`built ${entry} → ${outDir}/${outName}`)
}

// Main desktop bundle (empties dist first).
await buildEntry(mainEntry, 'ui.js', true)

// Optional immersive WebXR bundle (kept; do NOT empty dist).
const xrEntry = resolve(uiDir, 'xr', 'View.tsx')
if (existsSync(xrEntry)) {
  await buildEntry(xrEntry, 'xr.js', false, { standalone: xrStandalone })
}
