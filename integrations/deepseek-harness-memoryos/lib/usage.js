import {
  canonicalJson,
  harnessRequestEvidence,
  mapHarnessUsage,
  normalizeUsageConfig,
  sha256,
} from './core.js'

export function registerMemoryOSUsage(ctx, rawConfig, dependencies = {}) {
  const config = normalizeUsageConfig(rawConfig)
  if (config.usageGuardFile && typeof dependencies.readUsageGuard !== 'function') {
    throw new Error('usageGuardFile requires a synchronous pre-dispatch guard reader')
  }
  const pendingSteps = new Map()
  const activeSteps = new Map()
  const attemptCounts = new Map()

  ctx.on('session/event', async (session, event) => {
    const step = event?.data?.step
    if (!Number.isSafeInteger(step) || step < 0) return
    const key = `${session.id}:${step}`
    if (event.type === 'step/start') {
      pendingSteps.set(key, { startedAt: event.time, ttftAt: undefined })
      activeSteps.set(String(session.id), step)
      return
    }
    if (event.type === 'assistant/chunk') {
      const timing = pendingSteps.get(key)
      if (timing && timing.ttftAt === undefined && visibleChunk(event.data.chunk)) {
        timing.ttftAt = event.time
      }
      return
    }
    if (event.type !== 'assistant/message' || event.data.usage === undefined) return
    const timing = pendingSteps.get(key)
    if (!timing?.request) {
      throw new Error('DeepSeek Harness did not expose the assembled request through llm/stream')
    }
    const responseJson = canonicalJson(event.data.message)
    const record = mapHarnessUsage(
      event.data.usage,
      {
        runId: config.runId,
        taskId: config.taskId,
        condition: config.condition,
        cachePhase: config.cachePhase,
        sessionId: String(session.id),
        stepIndex: step,
        provider: config.provider ?? timing.request.provider ?? 'unknown-provider',
        model: config.model ?? timing.request.model ?? 'unknown-model',
        cacheNamespaceSha256: config.cacheNamespaceSha256,
        pricing: config.pricing,
        requestSha256: timing.request.sha256,
        responseSha256: sha256(responseJson),
        requestBytes: timing.request.bytes,
      },
      {
        ttftSeconds: finiteDuration(timing.startedAt, timing.ttftAt),
        latencySeconds: finiteDuration(timing.startedAt, event.time),
      },
    )
    pendingSteps.delete(key)
    activeSteps.delete(String(session.id))
    attemptCounts.delete(key)
    if (config.usageOutputFile && dependencies.appendFile) {
      await dependencies.appendFile(config.usageOutputFile, `${JSON.stringify(record)}\n`, 'utf8')
    }
    dependencies.onUsage?.(record)
  }, { global: true })

  ctx.on('llm/stream', (options, next) => {
    if (config.usageGuardFile) {
      const guard = dependencies.readUsageGuard(config.usageGuardFile)
      if (guard?.stop === true) {
        const reason = nonEmptyString(guard.reason) ?? 'controller requested a usage stop'
        throw new Error(`MEMORYOS_USAGE_GUARD_STOP: ${reason}`)
      }
    }
    const sessionId = options?.sessionId === undefined ? undefined : String(options.sessionId)
    const step = sessionId === undefined ? undefined : activeSteps.get(sessionId)
    if (sessionId !== undefined && step !== undefined && options.purpose === undefined) {
      const key = `${sessionId}:${step}`
      const timing = pendingSteps.get(key)
      if (timing) {
        const request = harnessRequestEvidence(options)
        const attemptIndex = (attemptCounts.get(key) ?? 0) + 1
        attemptCounts.set(key, attemptIndex)
        if (config.attemptOutputFile && dependencies.appendAttempt) {
          dependencies.appendAttempt(config.attemptOutputFile, `${JSON.stringify({
            event: 'provider_attempt',
            run_id: config.runId,
            task_id: config.taskId,
            condition: config.condition,
            cache_phase: config.cachePhase,
            session_id: sessionId,
            step_index: step,
            attempt_index: attemptIndex,
            provider: nonEmptyString(options.provider) ?? config.provider ?? 'unknown-provider',
            model: nonEmptyString(options.model) ?? config.model ?? 'unknown-model',
            request_sha256: request.sha256,
            request_bytes: request.bytes,
            request_components: request.components,
          })}\n`)
        }
        timing.request = {
          ...request,
          provider: nonEmptyString(options.provider),
          model: nonEmptyString(options.model),
        }
      }
    }
    return next()
  }, { global: true, prepend: true })

  return Object.freeze({ config })
}

function nonEmptyString(value) {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function visibleChunk(chunk) {
  return chunk?.type === 'text-delta'
    || chunk?.type === 'reasoning-delta'
    || chunk?.type === 'tool-call-delta'
}

function finiteDuration(start, end) {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
  return (end - start) / 1000
}
