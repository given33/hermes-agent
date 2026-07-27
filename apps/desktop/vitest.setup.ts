import { configure } from '@testing-library/react'

// Node 25+ exposes an experimental process-wide localStorage. When Vitest
// builds a jsdom project that property can shadow jsdom's per-worker storage,
// making stateful tests leak across workers or see an unavailable store unless
// --localstorage-file is configured. Pin UI tests to jsdom's isolated storage
// so the suite behaves the same on the Node 22 CI floor and newer runtimes.
const createMemoryStorage = (): Storage => {
  const values = new Map<string, string>()

  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: key => values.get(String(key)) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => values.delete(String(key)),
    setItem: (key, value) => values.set(String(key), String(value))
  }
}

const jsdomStorage = window.localStorage ?? createMemoryStorage()
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: jsdomStorage
})
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: jsdomStorage
})
jsdomStorage.clear()

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

// findBy*/waitFor default to a 1000ms deadline — too tight for async-heavy
// panels (radix menus, refetch chains) when the full suite runs under xdist
// CPU contention in CI. Success still resolves the instant the node appears;
// the wider deadline only absorbs a starved runner, killing timing flakes.
configure({ asyncUtilTimeout: 10_000 })
