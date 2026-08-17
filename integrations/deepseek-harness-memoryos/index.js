import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { assertHarnessCompatibility } from './lib/core.js'
import { registerMemoryOSPlugin } from './lib/plugin.js'
import { createFileMemoryControlState, defaultControlStateFile } from './lib/state.js'

export const name = 'memoryos-tools'
export const inject = ['tools']

export const Config = Schema.object({
  enabled: Schema.boolean().default(true),
  controlEnabled: Schema.boolean().default(true),
  onboardingNotice: Schema.boolean().default(true),
  baseUrl: Schema.string().default('http://127.0.0.1:8000'),
  condition: Schema.union([
    'legacy_full',
    'msc_full',
    'msc_progressive',
    'msc_delta',
    'msc_delta_core',
    'msc_context_only',
  ]).default('msc_progressive'),
  budgetTokens: Schema.number().default(6000),
  maxContextCalls: Schema.number().default(0),
  responseFormat: Schema.union([
    'json',
    'deepseek-compact',
    'deepseek-progressive-compact',
  ]).default('json'),
  toolProfile: Schema.union([
    'read-only',
    'cross-session-write',
  ]).default('read-only'),
  timeoutMs: Schema.number().default(30000),
  repository: Schema.string(),
  task: Schema.string(),
  authTokenEnv: Schema.string(),
  authTokenFile: Schema.string(),
  stateFile: Schema.string(),
})

export function apply(ctx, config) {
  assertHarnessCompatibility(ctx)
  const stateFile = config.stateFile || defaultControlStateFile()
  return registerMemoryOSPlugin(ctx, { ...config, stateFile }, {
    defineTool,
    stateStore: createFileMemoryControlState(stateFile),
  })
}

export { HARNESS_COMPATIBILITY, mapHarnessUsage } from './lib/core.js'
