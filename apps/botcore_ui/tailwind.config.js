/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{ts,tsx}',
    // Modular service UIs (repo/<svc>/<ver>/ui/View.tsx) are dynamically
    // loaded at runtime but share THIS app's Tailwind CSS. Scan them so the
    // utilities they use (e.g. gap-x-3 on the runtime card) aren't purged —
    // otherwise classes the host's own src doesn't also use silently vanish.
    '../botcore/repo/**/ui/**/*.{ts,tsx}',
  ],
  // Class-based dark mode: dark variants apply when an ancestor has the
  // `dark` class. Set on <html> in index.html so the whole app is dark
  // regardless of the user's OS theme.
  darkMode: 'class',
  theme: { extend: {} },
  plugins: [],
}
