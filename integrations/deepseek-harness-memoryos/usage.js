import { appendFileSync, readFileSync } from 'node:fs'
import { appendFile } from 'node:fs/promises'
import Schema from '@deepseek-ai/schemastery'
import { assertHarnessUsageCompatibility } from './lib/core.js'
import { registerMemoryOSUsage } from './lib/usage.js'

export const name = 'memoryos-usage'

export const Config = Schema.object({
  condition: Schema.union([
    'legacy_full',
    'msc_full',
    'msc_progressive',
    'msc_delta',
    'msc_delta_core',
    'no_memory',
    'msc_context_only',
  ]).default('no_memory'),
  usageOutputFile: Schema.string(),
  attemptOutputFile: Schema.string(),
  usageGuardFile: Schema.string(),
  runId: Schema.string().default('deepseek-harness'),
  taskId: Schema.string().default('unbound-task'),
  cachePhase: Schema.union(['cold', 'warm']).default('cold'),
  provider: Schema.string(),
  model: Schema.string(),
  cacheNamespaceSha256: Schema.string().default('0'.repeat(64)),
  evaluationHistoryCharLimit: Schema.number(),
  evaluationEvictionOutputFile: Schema.string(),
  evaluationSentinel: Schema.string(),
  pricing: Schema.union([
    Schema.object({
      cacheMissInputUsdPerMillion: Schema.number().required(),
      cacheHitInputUsdPerMillion: Schema.number().required(),
      outputUsdPerMillion: Schema.number().required(),
    }),
    Schema.never(),
  ]),
  pricingJson: Schema.string(),
})

export function apply(ctx, config) {
  assertHarnessUsageCompatibility(ctx)
  return registerMemoryOSUsage(ctx, config, {
    appendFile,
    appendAttempt(path, value) { appendFileSync(path, value, 'utf8') },
    appendEviction(path, value) { appendFileSync(path, value, 'utf8') },
    readUsageGuard(path) {
      let payload
      try {
        payload = JSON.parse(readFileSync(path, 'utf8'))
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error)
        throw new Error(`MEMORYOS_USAGE_GUARD_INVALID: ${detail}`)
      }
      if (payload === null || typeof payload !== 'object' || typeof payload.stop !== 'boolean') {
        throw new Error('MEMORYOS_USAGE_GUARD_INVALID: expected an object with boolean stop')
      }
      return payload
    },
  })
}
