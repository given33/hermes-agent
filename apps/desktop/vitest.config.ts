import type { TestProjectConfiguration } from 'vitest/config'
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform,
    // which can exceed vitest's 5000ms default under CI/load. Windows shards
    // can also stall on antivirus/module scanning; 60s absorbs that startup
    // noise without masking genuinely hung tests.
    testTimeout: 60_000
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}'],
    exclude: ['scripts/run-short-session-hang-repro.test.mjs'],
    // Same budget as the ui project below: the first test in each file pays
    // full module transform, and CI/antivirus stalls on Windows aren't real
    // hangs. Without this the electron project kept vitest's 5000ms default
    // while ui got 60s, flaking ~40 cold-transform-sensitive tests.
    testTimeout: 60_000
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
