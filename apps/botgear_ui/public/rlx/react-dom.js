// Import-map shim: resolves a service bundle's bare `react-dom` import to
// the host's single ReactDOM (set on window.__RLX__ in main.tsx). Needed
// by views that use createPortal (e.g. video's floating snapshot panel).
// See docs/TODO_SERVICE_UI_BUNDLES.md.
const D = window.__RLX__.reactDOM
export default D.default ?? D
// createPortal/flushSync for portal views; unstable_batchedUpdates is
// pulled by react-three-fiber's reconciler (v8). version for libs that
// feature-detect off it. __DOM_INTERNALS_... is react-dom's counterpart
// to react.js's __CLIENT_INTERNALS_... — see the comment there; without
// it, anything reaching for react-dom's shared internals object off this
// namespace gets `undefined` silently instead of the host's real one.
//
// This list is exactly React DOM 19.2.8's real export surface (verified
// against node_modules/react-dom/cjs/react-dom.production.js), same
// rationale as react.js's shim — cover the whole surface once instead of
// discovering gaps one bundle/property at a time. (react-dom/client's
// createRoot/hydrateRoot are a SEPARATE module specifier, not part of
// plain 'react-dom' — a bundle importing react-dom/client gets its own
// bundled copy, which is fine: it still shares this SAME react instance
// for hooks/dispatcher coordination via the internals object above.)
export const {
  createPortal, flushSync, unstable_batchedUpdates, version,
  preconnect, prefetchDNS, preinit, preinitModule, preload, preloadModule,
  requestFormReset, useFormState, useFormStatus,
  __DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE,
} = D
