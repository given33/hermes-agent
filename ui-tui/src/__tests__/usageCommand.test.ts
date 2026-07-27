import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import type { SessionUsageResponse } from '../gatewayTypes.js'

const usageCommand = sessionCommands.find(cmd => cmd.name === 'usage')!

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

const buildCtx = (results: Record<string, unknown>) => {
  const sys = vi.fn()
  const panel = vi.fn()
  const rpc = vi.fn((method: string, _params: unknown) => Promise.resolve(results[method]))
  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), panel, sys }
  }

  const run = async () => {
    usageCommand.run('', ctx as any, 'usage')
    await rpc.mock.results[0]?.value
    await Promise.resolve()
    await Promise.resolve()
  }

  return { panel, run, sys }
}

const baseUsage = (overrides: Partial<SessionUsageResponse> = {}): SessionUsageResponse =>
  ({ calls: 0, input: 0, output: 0, total: 0, ...overrides }) as SessionUsageResponse

describe('/usage slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('reports an empty session without opening a billing panel', async () => {
    const { panel, run, sys } = buildCtx({ 'session.usage': baseUsage() })

    await run()

    expect(sys).toHaveBeenCalledWith('no API calls yet')
    expect(panel).not.toHaveBeenCalled()
  })

  it('renders token, context, compression, and request statistics', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        calls: 3,
        compressions: 2,
        context_max: 128_000,
        context_percent: 25,
        context_used: 32_000,
        input: 1_200,
        model: 'test-model',
        output: 300,
        total: 1_500
      })
    })

    await run()

    expect(panel).toHaveBeenCalledWith(
      'Usage',
      expect.arrayContaining([
        expect.objectContaining({ rows: expect.arrayContaining([['Model', 'test-model'], ['API calls', '3']]) }),
        { text: 'Context: 32,000 / 128,000 (25%)' },
        { text: 'Compressions: 2' }
      ])
    )
  })
})
