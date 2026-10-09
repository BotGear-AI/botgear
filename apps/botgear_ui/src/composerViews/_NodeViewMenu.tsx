// The "switch view" kebab. Iterates the registry — adding a new
// view file in this directory automatically extends the menu.
//
// Lives in this directory (not in Composer.tsx) because every shape
// renders it on its own title bar and we want a single source of
// truth for the menu UX.
//
// Pop-out ("Open in window") used to live here as a kebab item
// available from every shape; it's now a dedicated title-bar icon on
// view_full only (next to Maximize) — see view_full.tsx — so this menu
// is purely the shape switcher again.
import { useEffect, useRef, useState } from 'react'
import { MoreVertical } from 'lucide-react'

import type { ServiceProxy } from '../models/ServiceProxy'
import { offeredViewsFor } from './index'


export function NodeViewMenu({
  proxy,
  current,
  onChange,
}: {
  proxy: ServiceProxy
  current: string
  onChange?: (proxyId: string, next: string) => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  // Close only when the mousedown lands OUTSIDE the menu wrapper. The
  // previous version closed on every document mousedown — which fired
  // before the menu item's click event resolved, unmounting the item
  // and swallowing the click.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as globalThis.Node | null
      if (target && wrapRef.current && wrapRef.current.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  const proxyId = proxy.id ?? proxy.name ?? ''
  const options = offeredViewsFor(proxy)
  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        aria-label="Switch node view"
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }}
        className="nodrag nopan rounded p-0.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
      >
        <MoreVertical className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-10 mt-1 min-w-[160px] overflow-hidden rounded border border-slate-700 bg-slate-900 text-xs shadow-xl">
          {options.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => { e.stopPropagation(); onChange?.(proxyId, id); setOpen(false) }}
              className={
                id === current
                  ? 'nodrag nopan block w-full px-3 py-1.5 text-left font-medium text-sky-300 bg-slate-800'
                  : 'nodrag nopan block w-full px-3 py-1.5 text-left text-slate-200 hover:bg-slate-800'
              }
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
