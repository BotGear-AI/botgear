// Import-map shim: resolves a modular service bundle's bare `react`
// import to the HOST's single React instance (set on window.__RLX__ in
// main.tsx). Prevents a second React copy (which would break hooks).
// See docs/TODO_SERVICE_UI_BUNDLES.md.
const R = window.__RLX__.react
export default R.default ?? R
// Re-export the FULL React named-export surface — heavy bundles (r3f,
// drei, zustand, use-sync-external-store) import hooks beyond the basics
// (e.g. useDebugValue, useInsertionEffect). A missing name here surfaces
// as "module 'react' does not provide an export named X" at bundle load
// for a STATIC `import { X } from 'react'` — but for a bundled dependency
// that reaches for a property at runtime off the namespace object instead
// (react-dom's own code does `React.__CLIENT_INTERNALS_...`, not a static
// import; some libraries call `React.use(...)` the same way), a missing
// name here fails SILENTLY as `undefined` instead, surfacing much later
// and less obviously as e.g. "Cannot read properties of undefined
// (reading 'S')" or "m.use is not a function". __CLIENT_INTERNALS_... is
// React's ReactSharedInternals (dispatcher/transition coordination
// object) — react-dom, react-reconciler and friends all need the SAME
// instance the host's react-dom already got from THIS SAME React
// instance, which only happens if we forward it here too.
//
// This list is exactly React 19.2.8's real export surface (verified
// against node_modules/react/cjs/react.production.js — `grep -n
// "^exports\." react.production.js`) rather than "whatever a given
// bundle happened to need" — so a future bundle pulling in `useOptimistic`
// or `use` doesn't reopen this same bug one property at a time.
// `createFactory` is kept for compat even though React 19 dropped it
// (React <=18 code that still imports it gets `undefined`, not a build
// break; nothing currently in this repo's bundles calls it).
export const {
  createElement, cloneElement, createContext, createRef, createFactory,
  Fragment, Children, Component, PureComponent, Profiler, StrictMode,
  Suspense, Activity, forwardRef, memo, lazy, isValidElement, version,
  useState, useEffect, useLayoutEffect, useInsertionEffect, useRef,
  useMemo, useCallback, useContext, useReducer, useImperativeHandle,
  useId, useTransition, useDeferredValue, useSyncExternalStore,
  useDebugValue, startTransition, use, useActionState, useOptimistic,
  useEffectEvent, cache, cacheSignal, unstable_useCacheRefresh,
  __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE,
  __COMPILER_RUNTIME,
} = R
