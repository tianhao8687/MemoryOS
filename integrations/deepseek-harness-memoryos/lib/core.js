import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'

export const HARNESS_COMPATIBILITY = Object.freeze({
  version: '0.1.0-rc.5',
  commit: '47f943859bef60e4160492346772ded9b24f765a',
})

const CONDITION_POLICY = Object.freeze({
  legacy_full: Object.freeze({ detailLevel: 'fact', initialMode: 'full', laterMode: 'full' }),
  msc_full: Object.freeze({ detailLevel: 'fact', initialMode: 'full', laterMode: 'full' }),
  msc_progressive: Object.freeze({ detailLevel: 'index', initialMode: 'full', laterMode: 'full' }),
  msc_delta: Object.freeze({ detailLevel: 'fact', initialMode: 'full', laterMode: 'delta' }),
  msc_delta_core: Object.freeze({ detailLevel: 'fact', initialMode: 'full', laterMode: 'delta' }),
  msc_context_only: Object.freeze({ detailLevel: 'fact', initialMode: 'full', laterMode: 'full' }),
})

const USAGE_CONDITIONS = new Set([
  'legacy_full',
  'msc_full',
  'msc_progressive',
  'msc_delta',
  'msc_delta_core',
  'no_memory',
  'msc_context_only',
])

const VOLATILE_KEYS = new Set([
  'retrieval_run_id',
  'selection_latency_ms',
  'render_latency_ms',
  'duration_ms',
  'stage_timings_ms',
  'usage',
])

export function assertHarnessCompatibility(ctx) {
  if (!ctx || typeof ctx !== 'object'
      || !ctx.tools || typeof ctx.tools.register !== 'function') {
    throw new Error(
      'DeepSeek Harness API incompatibility: expected ctx.tools.register '
      + `from ${HARNESS_COMPATIBILITY.version} @ ${HARNESS_COMPATIBILITY.commit}`,
    )
  }
}

export function assertHarnessUsageCompatibility(ctx) {
  if (!ctx || typeof ctx !== 'object' || typeof ctx.on !== 'function') {
    throw new Error(
      'DeepSeek Harness API incompatibility: expected ctx.on '
      + `from ${HARNESS_COMPATIBILITY.version} @ ${HARNESS_COMPATIBILITY.commit}`,
    )
  }
}

export function normalizeConfig(config) {
  const condition = config.condition ?? 'msc_progressive'
  if (!Object.hasOwn(CONDITION_POLICY, condition)) {
    throw new Error(`unsupported MemoryOS condition: ${String(condition)}`)
  }
  const baseUrl = String(config.baseUrl ?? 'http://127.0.0.1:8000').replace(/\/+$/u, '')
  const url = new URL(baseUrl)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('baseUrl must be an HTTP(S) URL without embedded credentials')
  }
  const timeoutMs = Number(config.timeoutMs ?? 30_000)
  const budgetTokens = Number(config.budgetTokens ?? 6_000)
  const maxContextCalls = Number(config.maxContextCalls ?? 0)
  const responseFormat = String(config.responseFormat ?? 'json')
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 300_000) {
    throw new Error('timeoutMs must be an integer between 1 and 300000')
  }
  if (!Number.isSafeInteger(budgetTokens) || budgetTokens < 64 || budgetTokens > 50_000) {
    throw new Error('budgetTokens must be an integer between 64 and 50000')
  }
  if (!Number.isSafeInteger(maxContextCalls) || maxContextCalls < 0 || maxContextCalls > 100) {
    throw new Error('maxContextCalls must be an integer between 0 and 100')
  }
  if (!['json', 'deepseek-compact', 'deepseek-progressive-compact'].includes(responseFormat)) {
    throw new Error(
      'responseFormat must be json, deepseek-compact, or deepseek-progressive-compact',
    )
  }
  if (responseFormat === 'deepseek-progressive-compact' && condition !== 'msc_progressive') {
    throw new Error('deepseek-progressive-compact requires msc_progressive')
  }
  return Object.freeze({
    baseUrl,
    condition,
    budgetTokens,
    maxContextCalls,
    responseFormat,
    timeoutMs,
    repository: config.repository ? String(config.repository) : undefined,
    task: config.task ? String(config.task) : undefined,
    authTokenEnv: config.authTokenEnv ? String(config.authTokenEnv) : undefined,
    authTokenFile: config.authTokenFile ? String(config.authTokenFile) : undefined,
  })
}

export function normalizeUsageConfig(config) {
  const condition = String(config.condition ?? 'no_memory')
  if (!USAGE_CONDITIONS.has(condition)) {
    throw new Error(`unsupported MemoryOS usage condition: ${condition}`)
  }
  return Object.freeze({
    condition,
    usageOutputFile: config.usageOutputFile ? String(config.usageOutputFile) : undefined,
    attemptOutputFile: config.attemptOutputFile ? String(config.attemptOutputFile) : undefined,
    usageGuardFile: config.usageGuardFile ? String(config.usageGuardFile) : undefined,
    runId: String(config.runId ?? 'deepseek-harness'),
    taskId: String(config.taskId ?? 'unbound-task'),
    cachePhase: config.cachePhase === 'warm' ? 'warm' : 'cold',
    provider: config.provider ? String(config.provider) : undefined,
    model: config.model ? String(config.model) : undefined,
    cacheNamespaceSha256: String(config.cacheNamespaceSha256 ?? '0'.repeat(64)),
    pricing: normalizePricing(parsePricing(config.pricing, config.pricingJson)),
  })
}

function parsePricing(value, serialized) {
  if (value !== undefined && value !== null) return value
  if (serialized === undefined || serialized === null || serialized === '') return undefined
  try {
    return JSON.parse(String(serialized))
  } catch {
    throw new Error('pricingJson must contain a JSON object')
  }
}

function normalizePricing(value) {
  if (value === undefined || value === null) return undefined
  const pricing = {
    cacheMissInputUsdPerMillion: Number(value.cacheMissInputUsdPerMillion),
    cacheHitInputUsdPerMillion: Number(value.cacheHitInputUsdPerMillion),
    outputUsdPerMillion: Number(value.outputUsdPerMillion),
  }
  if (Object.values(pricing).some(item => !Number.isFinite(item) || item < 0)) {
    throw new Error('pricing values must be finite non-negative numbers')
  }
  return Object.freeze(pricing)
}

export async function resolveAuthToken(config, environment = process.env) {
  if (config.authTokenEnv) {
    const value = environment[config.authTokenEnv]
    if (!value) throw new Error(`MemoryOS auth token environment is missing: ${config.authTokenEnv}`)
    return value.trim()
  }
  if (config.authTokenFile) {
    const value = (await readFile(config.authTokenFile, 'utf8')).trim()
    if (!value) throw new Error('MemoryOS auth token file is empty')
    return value
  }
  return undefined
}

export function createMemoryOSClient(config, dependencies = {}) {
  const fetchImpl = dependencies.fetchImpl ?? globalThis.fetch
  if (typeof fetchImpl !== 'function') throw new Error('a standards-compatible fetch is required')
  const states = new Map()

  async function request(path, options, signal) {
    const token = await resolveAuthToken(config, dependencies.environment)
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(new Error('MemoryOS request timed out')), config.timeoutMs)
    const abort = () => controller.abort(signal?.reason)
    signal?.addEventListener('abort', abort, { once: true })
    try {
      const response = await fetchImpl(`${config.baseUrl}${path}`, {
        ...options,
        headers: {
          Accept: 'application/json',
          ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(token === undefined ? {} : { Authorization: `Bearer ${token}` }),
        },
        signal: controller.signal,
      })
      const text = await response.text()
      let payload
      try {
        payload = JSON.parse(text)
      } catch {
        throw new Error(`MemoryOS returned non-JSON HTTP ${response.status}`)
      }
      if (!response.ok) throw new Error(`MemoryOS returned HTTP ${response.status}`)
      return payload
    } finally {
      clearTimeout(timeout)
      signal?.removeEventListener('abort', abort)
    }
  }

  async function context(args, sessionId, signal) {
    const state = states.get(sessionId) ?? { calls: 0, previous: undefined, stable: new Map() }
    states.set(sessionId, state)
    if (config.maxContextCalls > 0 && state.calls >= config.maxContextCalls) {
      throw new Error(`memory_context is limited to ${config.maxContextCalls} call(s) per session`)
    }
    const policy = CONDITION_POLICY[config.condition]
    const responseMode = state.calls === 0 ? policy.initialMode : policy.laterMode
    const body = {
      task: config.task ?? args.task,
      repository: config.repository ?? args.repository,
      budget_tokens: config.budgetTokens,
      detail_level: policy.detailLevel,
      response_mode: responseMode,
      ...(responseMode === 'delta' && state.previous
        ? { previous_context_id: state.previous }
        : {}),
    }
    if (!body.task || !body.repository) {
      throw new Error('memory_context requires task and repository unless fixed in plugin config')
    }
    const raw = await request('/api/context', { method: 'POST', body: JSON.stringify(body) }, signal)
    state.calls += 1
    if (typeof raw.context_id === 'string') {
      const stable = stableContextId(raw)
      state.stable.set(raw.context_id, stable)
      state.previous = raw.context_id
    }
    return {
      context: sanitizeContext(raw, state.stable),
      experiment: {
        condition: config.condition,
        detail_level: policy.detailLevel,
        response_mode: responseMode,
      },
    }
  }

  async function explain(args, signal) {
    const query = new URLSearchParams()
    if (args.expected_atom_sha256) query.set('expected_atom_sha256', args.expected_atom_sha256)
    for (const section of args.sections ?? []) query.append('sections', section)
    if (args.budget_tokens !== undefined) query.set('budget_tokens', String(args.budget_tokens))
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    const raw = await request(
      `/api/memories/${encodeURIComponent(args.memory_id)}/explain${suffix}`,
      { method: 'GET' },
      signal,
    )
    return sanitizeValue(raw)
  }

  return Object.freeze({ context, explain, states })
}

export function stableContextId(raw) {
  const semantic = sanitizeValue(raw)
  if (semantic && typeof semantic === 'object' && !Array.isArray(semantic)) {
    delete semantic.context_id
    delete semantic.requires_base_context_id
  }
  return sha256(canonicalJson(semantic))
}

export function sanitizeContext(raw, stableIds) {
  const visible = sanitizeValue(raw)
  if (!visible || typeof visible !== 'object' || Array.isArray(visible)) {
    throw new Error('MemoryOS context response must be a JSON object')
  }
  if (typeof raw.context_id === 'string') visible.context_id = stableIds.get(raw.context_id)
  if (typeof raw.requires_base_context_id === 'string') {
    visible.requires_base_context_id = stableIds.get(raw.requires_base_context_id)
      ?? sha256(raw.requires_base_context_id)
  }
  if (!visible.context_id) visible.context_fingerprint = stableContextId(raw)
  return visible
}

export function sanitizeValue(value) {
  if (Array.isArray(value)) return value.map(sanitizeValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !VOLATILE_KEYS.has(key))
        .map(([key, item]) => [key, sanitizeValue(item)]),
    )
  }
  return value
}

export function mapHarnessUsage(usage, metadata, timing = {}) {
  const uncached = nonNegativeInteger(usage.inputTokens, 'inputTokens')
  const output = nonNegativeInteger(usage.outputTokens, 'outputTokens')
  const hasCacheRead = Object.hasOwn(usage, 'cacheReadTokens')
  const cacheRead = hasCacheRead
    ? nonNegativeInteger(usage.cacheReadTokens, 'cacheReadTokens')
    : undefined
  const cacheWrite = Object.hasOwn(usage, 'cacheWriteTokens')
    ? nonNegativeInteger(usage.cacheWriteTokens, 'cacheWriteTokens')
    : 0
  const input = uncached + (cacheRead ?? 0) + cacheWrite
  const miss = hasCacheRead ? uncached + cacheWrite : null
  const hit = hasCacheRead ? cacheRead : null
  const reasoning = Object.hasOwn(usage, 'reasoningTokens')
    ? nonNegativeInteger(usage.reasoningTokens, 'reasoningTokens')
    : null
  if (reasoning !== null && reasoning > output) throw new Error('reasoningTokens exceeds outputTokens')
  return {
    schema_version: '1.0',
    run_id: metadata.runId,
    task_id: metadata.taskId,
    condition: metadata.condition,
    cache_phase: metadata.cachePhase,
    session_id: metadata.sessionId,
    step_index: metadata.stepIndex,
    provider: metadata.provider,
    model: metadata.model,
    input_tokens: input,
    cache_hit_tokens: hit,
    cache_miss_tokens: miss,
    output_tokens: output,
    reasoning_tokens: reasoning,
    cost_usd: calculateCost(metadata.pricing, input, hit, miss, output),
    ttft_seconds: timing.ttftSeconds ?? null,
    latency_seconds: timing.latencySeconds ?? null,
    usage_source: 'provider_exact',
    request_sha256: metadata.requestSha256,
    response_sha256: metadata.responseSha256,
    request_bytes: metadata.requestBytes,
    memory_payload_tokens: null,
    memory_wrapper_tokens: null,
    memory_tool_schema_tokens: null,
    other_tool_schema_tokens: null,
    cache_namespace_sha256: metadata.cacheNamespaceSha256,
  }
}

export function harnessRequestEvidence(options) {
  const projection = deepSeekVisibleRequest(options)
  const encoded = new TextEncoder().encode(canonicalJson(projection))
  const generation = { ...projection }
  delete generation.messages
  delete generation.tools
  const components = Object.fromEntries(
    Object.entries({
      system: options.system ?? null,
      messages: deepSeekVisibleMessages(options.messages ?? []),
      tools: projection.tools ?? [],
      generation,
    }).map(([name, value]) => {
      const component = new TextEncoder().encode(canonicalJson(value))
      return [name, Object.freeze({
        sha256: sha256(component),
        bytes: component.byteLength,
      })]
    }),
  )
  return Object.freeze({
    sha256: sha256(encoded),
    bytes: encoded.byteLength,
    components: Object.freeze(components),
  })
}

/**
 * Project Harness request objects onto the JSON fields visible to DeepSeek.
 *
 * Harness messages carry random durable ids and UI/source metadata.  The
 * DeepSeek adapter deliberately strips those fields before dispatch, so
 * hashing the raw GenerateOptions makes identical cold/warm prompts look
 * different and invalidates prefix-cache evidence.  Keep this projection in
 * lock-step with dsh-llm-deepseek 0.1.0-rc.5's serializeRequest().
 */
export function deepSeekVisibleRequest(options) {
  const messages = []
  if (options.system !== undefined) {
    messages.push({ role: 'system', content: options.system })
  }
  messages.push(...deepSeekVisibleMessages(options.messages ?? []))

  const tools = options.tools?.map(tool => ({
    type: 'function',
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    },
  }))
  const reasoning = deepSeekVisibleReasoning(options.reasoningEffort)
  return {
    model: options.model,
    messages,
    stream: true,
    stream_options: { include_usage: true },
    ...reasoning,
    ...(tools === undefined || tools.length === 0 ? {} : { tools }),
    ...(options.temperature === undefined ? {} : { temperature: options.temperature }),
    ...(options.maxTokens === undefined ? {} : { max_tokens: options.maxTokens }),
    ...(options.stop === undefined ? {} : { stop: options.stop }),
  }
}

function deepSeekVisibleMessages(messages) {
  const visible = []
  for (const message of messages) {
    if (message.role === 'system') {
      visible.push({ role: 'system', content: flattenVisibleText(message.content) })
      continue
    }
    if (message.role === 'assistant') {
      const content = flattenVisibleText(message.content)
      const reasoning = (message.content ?? [])
        .filter(block => block.type === 'reasoning')
        .map(block => block.text)
        .join('')
      const toolCalls = (message.content ?? [])
        .filter(block => block.type === 'tool-call')
        .map(block => ({
          id: block.id,
          type: 'function',
          function: { name: block.name, arguments: block.arguments },
        }))
      visible.push({
        role: 'assistant',
        content,
        ...(toolCalls.length > 0 && reasoning.length > 0
          ? { reasoning_content: reasoning }
          : {}),
        ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
      })
      continue
    }
    const blocks = message.content ?? []
    const toolResults = blocks.filter(block => block.type === 'tool-result')
    const content = flattenVisibleText(blocks)
    if (content.length > 0 || toolResults.length === 0) {
      visible.push({ role: 'user', content })
    }
    for (const result of toolResults) {
      visible.push({
        role: 'tool',
        tool_call_id: result.toolCallId,
        content: flattenVisibleText(result.content) || '(no output)',
      })
    }
  }
  return visible
}

function flattenVisibleText(blocks = []) {
  return blocks
    .filter(block => block.type === 'text')
    .map(block => block.text)
    .join('')
}

function deepSeekVisibleReasoning(effort) {
  if (effort === 'off') return { thinking: { type: 'disabled' } }
  if (effort === 'high' || effort === 'max') {
    return { thinking: { type: 'enabled' }, reasoning_effort: effort }
  }
  return {}
}

function calculateCost(pricing, input, hit, miss, output) {
  if (!pricing) return null
  if (hit === null || miss === null) return null
  const inputCost = (
    miss * pricing.cacheMissInputUsdPerMillion
    + hit * pricing.cacheHitInputUsdPerMillion
  ) / 1_000_000
  return inputCost + output * pricing.outputUsdPerMillion / 1_000_000
}

function nonNegativeInteger(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer`)
  return value
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}
