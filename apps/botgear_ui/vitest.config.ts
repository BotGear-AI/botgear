import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // Speed: coverage instrumentation only runs when `--coverage` is passed
    // (`npm run test:coverage`, or CI's run_app_tests.py once
    // @vitest/coverage-v8 is present) — plain `npm test` stays uninstrumented
    // and fast. json-summary is what CI's post_coverage_comment.py reads
    // (write_node_coverage_summary in run_app_tests.py); text/html are for a
    // human reading a local run.
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary', 'html'],
    },
  },
});
