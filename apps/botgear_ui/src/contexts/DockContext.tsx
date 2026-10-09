// DockContext — a tiny flag answering "are we rendering inside a
// popped-out dock window?" (DockView). view_full.tsx's "Open in window"
// title-bar icon reads it to hide itself when already in a dock —
// popping a dock view out of a dock window is a no-op worth avoiding.
// Defaults to false everywhere else (the canvas), so views behave
// exactly as before outside the dock.
import { createContext, useContext, type ReactNode } from 'react'

const Context = createContext<boolean>(false)

export function DockProvider({ children }: { children: ReactNode }) {
  return <Context.Provider value={true}>{children}</Context.Provider>
}

/** True only inside a DockView popup. */
export function useInDock(): boolean {
  return useContext(Context)
}
