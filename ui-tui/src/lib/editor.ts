import { spawnSync } from 'node:child_process'
import { accessSync, constants, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'

import { withInkSuspended } from '@hermes/ink'

/**
 * Editor fallback chain when neither $VISUAL nor $EDITOR is set. Mirrors
 * prompt_toolkit's `Buffer.open_in_editor()` picker so the classic CLI and
 * the TUI launch the same editor on a given box.
 */
const FALLBACKS = ['editor', 'nano', 'pico', 'vi', 'emacs']

/**
 * Tokenize an editor command without invoking a shell. Quotes keep paths and
 * arguments containing spaces together; POSIX backslash escapes are honored
 * outside single quotes. Windows backslashes remain literal so drive paths
 * such as `C:\\Program Files\\Code\\Code.cmd` survive intact.
 */
export function tokenizeEditorCommand(value: string, platform: NodeJS.Platform = process.platform): null | string[] {
  const tokens: string[] = []
  let token = ''
  let tokenStarted = false
  let quote: '"' | "'" | null = null

  for (let i = 0; i < value.length; i++) {
    const character = value[i]!

    if (quote === "'") {
      if (character === "'") {
        quote = null
      } else {
        token += character
      }

      tokenStarted = true

      continue
    }

    if (quote === '"') {
      if (character === '"') {
        quote = null
      } else if (platform !== 'win32' && character === '\\' && i + 1 < value.length) {
        token += value[++i]!
      } else {
        token += character
      }

      tokenStarted = true

      continue
    }

    if (/\s/.test(character)) {
      if (tokenStarted) {
        tokens.push(token)
        token = ''
        tokenStarted = false
      }

      continue
    }

    if (character === '"' || character === "'") {
      quote = character
      tokenStarted = true

      continue
    }

    if (platform !== 'win32' && character === '\\' && i + 1 < value.length) {
      token += value[++i]!
    } else {
      token += character
    }

    tokenStarted = true
  }

  if (quote) {
    return null
  }

  if (tokenStarted) {
    tokens.push(token)
  }

  return tokens
}

function repairWindowsExecutablePath(tokens: string[]): string[] {
  if (tokens.length < 2 || (!tokens[0]!.includes('\\') && !tokens[0]!.includes('/'))) {
    return tokens
  }

  const executableIndex = tokens.findIndex((token, index) => index > 0 && /\.(?:exe|cmd|bat|com)$/i.test(token))

  if (executableIndex <= 0) {
    return tokens
  }

  return [`${tokens.slice(0, executableIndex + 1).join(' ')}`, ...tokens.slice(executableIndex + 1)]
}

const isExecutable = (path: string): boolean => {
  try {
    accessSync(path, constants.X_OK)

    return true
  } catch {
    return false
  }
}

/**
 * Resolve the editor invocation argv (without the file argument).
 *
 *   1. $VISUAL / $EDITOR, quote-tokenized so paths with spaces and arguments
 *      remain separate argv entries (without shell expansion)
 *   2. on POSIX: first FALLBACKS entry resolvable on $PATH
 *   3. on Windows: `notepad.exe`
 *   4. literal `['vi']` as the last-resort POSIX floor
 */
export const resolveEditor = (
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform
): string[] => {
  const explicit = env.VISUAL ?? env.EDITOR

  if (explicit?.trim()) {
    const parsed = tokenizeEditorCommand(explicit.trim(), platform)

    if (parsed?.[0]) {
      return platform === 'win32' ? repairWindowsExecutablePath(parsed) : parsed
    }
  }

  if (platform === 'win32') {
    return ['notepad.exe']
  }

  const dirs = (env.PATH ?? '').split(delimiter).filter(Boolean)
  const found = FALLBACKS.flatMap(name => dirs.map(d => join(d, name))).find(isExecutable)

  return [found ?? 'vi']
}

/** Suspend Ink, open ``initial`` in $EDITOR, return the edited text (null if aborted). */
export async function openInEditor(initial: string, suffix = '.txt'): Promise<null | string> {
  const dir = mkdtempSync(join(tmpdir(), 'hermes-edit-'))
  const file = join(dir, `edit${suffix}`)
  writeFileSync(file, initial)
  const [cmd, ...args] = resolveEditor()
  let status: null | number = null

  await withInkSuspended(async () => {
    status = spawnSync(cmd!, [...args, file], { stdio: 'inherit' }).status
  })

  try {
    return status === 0 ? readFileSync(file, 'utf8') : null
  } finally {
    rmSync(dir, { force: true, recursive: true })
  }
}
