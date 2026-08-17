import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { chmod, mkdir, rename, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'

const STATE_SCHEMA_VERSION = '1.0'

export function defaultControlStateFile(environment = process.env, options = {}) {
  const platform = options.platform ?? process.platform
  const home = options.home ?? homedir()
  const root = platform === 'win32'
    ? environment.LOCALAPPDATA || join(home, 'AppData', 'Local')
    : environment.XDG_CONFIG_HOME || join(home, '.config')
  return resolve(root, 'dsh-memoryos', 'state.json')
}

export function createMemoryControlState(initialState) {
  let state = normalizeState(initialState)
  return Object.freeze({
    read() {
      return { state: { ...state }, warning: undefined }
    },
    async write(value) {
      state = normalizeState(value)
    },
  })
}

export function createFileMemoryControlState(path) {
  const statePath = resolve(String(path))
  let writes = Promise.resolve()
  return Object.freeze({
    path: statePath,
    read(fallback) {
      const initial = normalizeState(fallback)
      try {
        const parsed = JSON.parse(readFileSync(statePath, 'utf8'))
        return { state: normalizeState(parsed), warning: undefined }
      } catch (error) {
        if (error?.code === 'ENOENT') return { state: initial, warning: undefined }
        return {
          state: { ...initial, enabled: false },
          warning: `MemoryOS control state was invalid and memory was disabled: ${errorMessage(error)}`,
        }
      }
    },
    write(value) {
      const next = normalizeState(value)
      const operation = writes.then(() => atomicWriteState(statePath, next))
      writes = operation.catch(() => {})
      return operation
    },
  })
}

export function memoryControlState(enabled, onboardingNoticeShown = false) {
  return Object.freeze({
    schema_version: STATE_SCHEMA_VERSION,
    enabled: Boolean(enabled),
    onboarding_notice_shown: Boolean(onboardingNoticeShown),
  })
}

async function atomicWriteState(path, value) {
  const directory = dirname(path)
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const temporary = join(directory, `.${basename(path)}.${randomUUID()}.tmp`)
  try {
    await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    })
    try {
      await chmod(temporary, 0o600)
    } catch (error) {
      if (process.platform !== 'win32') throw error
    }
    await rename(temporary, path)
  } finally {
    await rm(temporary, { force: true })
  }
}

function normalizeState(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('MemoryOS control state must be an object')
  }
  if (value.schema_version !== STATE_SCHEMA_VERSION) {
    throw new Error(`unsupported MemoryOS control state schema: ${String(value.schema_version)}`)
  }
  if (typeof value.enabled !== 'boolean') {
    throw new Error('MemoryOS control state enabled must be boolean')
  }
  if (typeof value.onboarding_notice_shown !== 'boolean') {
    throw new Error('MemoryOS control state onboarding_notice_shown must be boolean')
  }
  return memoryControlState(value.enabled, value.onboarding_notice_shown)
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}
