import { randomUUID } from 'node:crypto'
import {
  canonicalJson,
  harnessRequestEvidence,
  mapHarnessUsage,
  memoryWriteTokenAccounting,
  normalizeUsageConfig,
  sha256,
} from './core.js'

export function registerMemoryOSUsage(ctx, rawConfig, dependencies = {}) {
  const config = normalizeUsageConfig(rawConfig)
  if (config.usageGuardFile && typeof dependencies.readUsageGuard !== 'function') {
    throw new Error('usageGuardFile requires a synchronous pre-dispatch guard reader')
  }
  if (config.evaluationEvictionOutputFile && typeof dependencies.appendEviction !== 'function') {
    throw new Error('evaluationEvictionOutputFile requires an eviction ledger writer')
  }
  registerControlledContextEviction(ctx, config, dependencies)
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
          const writeTokenAccounting = memoryWriteTokenAccounting(options)
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
            memory_write_token_accounting: writeTokenAccounting,
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

function registerControlledContextEviction(ctx, config, dependencies) {
  const limit = config.evaluationHistoryCharLimit
  if (limit === undefined) return
  ctx.on('agent/pre-step', async ({ agent, step }, next) => {
    if (step !== 1) return next()
    const session = agent?.session
    const nodes = Array.isArray(session?.surface?.nodes)
      ? [...session.surface.nodes]
      : session?.surface?.nodes === undefined
        ? []
        : [...session.surface.nodes]
    const messages = typeof session?.deriveMessages === 'function'
      ? session.deriveMessages()
      : []
    if (nodes.length !== messages.length || messages.length < 2) return next()
    const encoded = messages.map(message => canonicalJson(message))
    const totalChars = encoded.reduce((total, value) => total + value.length, 0)
    if (totalChars <= limit) return next()

    let retainedChars = 0
    let retainStart = messages.length
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const nextChars = retainedChars + encoded[index].length
      if (nextChars > limit && retainStart < messages.length) break
      retainStart = index
      retainedChars = nextChars
    }
    retainStart = completeTurnBoundary(session, nodes, retainStart)
    if (retainStart <= 0 || retainStart >= messages.length) return next()

    const shadowedSeqs = nodes.slice(0, retainStart)
    const shadowedText = encoded.slice(0, retainStart).join('\n')
    const retainedText = encoded.slice(retainStart).join('\n')
    const markerText = [
      'Controlled context-window eviction removed older conversation turns.',
      'Those turns are not present in the active model context. Do not infer their contents.',
    ].join(' ')
    const replacement = session.append('user/message', {
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: markerText }],
      source: {
        kind: 'plugin',
        plugin: 'dsh-memoryos-evaluation',
        form: 'notice',
        summary: 'controlled context eviction',
      },
    }, {
      surfaceOp: {
        op: 'replace',
        start: shadowedSeqs[0],
        end: shadowedSeqs.at(-1),
      },
      sourceEventSeqs: shadowedSeqs,
    })
    if (config.evaluationEvictionOutputFile) {
      const sentinel = config.evaluationSentinel
      await dependencies.appendEviction(config.evaluationEvictionOutputFile, `${JSON.stringify({
        schema_version: '1.0',
        event: 'controlled_context_eviction',
        session_id: String(session.id),
        history_char_limit: limit,
        surface_chars_before: totalChars,
        shadowed_chars: shadowedText.length,
        retained_chars_before_marker: retainedText.length,
        shadowed_message_count: retainStart,
        retained_message_count: messages.length - retainStart,
        shadowed_seqs: shadowedSeqs,
        replacement_seq: replacement.seq,
        sentinel_sha256: sentinel === undefined ? null : sha256(sentinel),
        shadowed_contains_sentinel: sentinel === undefined ? null : shadowedText.includes(sentinel),
        retained_contains_sentinel: sentinel === undefined ? null : retainedText.includes(sentinel),
      })}\n`)
    }
    return next()
  }, { global: true, prepend: true })
}

function completeTurnBoundary(session, nodes, proposed) {
  const events = session.events ?? []
  const isUser = index => events[nodes[index]]?.type === 'user/message'
  if (isUser(proposed)) return proposed
  for (let index = proposed + 1; index < nodes.length; index += 1) {
    if (isUser(index)) return index
  }
  for (let index = proposed - 1; index >= 0; index -= 1) {
    if (isUser(index)) return index
  }
  return proposed
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
