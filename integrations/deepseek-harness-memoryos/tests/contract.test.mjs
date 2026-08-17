import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import {
  assertHarnessCompatibility,
  assertHarnessUsageCompatibility,
  deepSeekVisibleRequest,
  harnessRequestEvidence,
  memoryWriteTokenAccounting,
} from '../lib/core.js'
import {
  registerMemoryOSPlugin,
  renderDeepSeekContext,
  renderDeepSeekExplanation,
} from '../lib/plugin.js'
import { registerMemoryOSUsage } from '../lib/usage.js'
import {
  createFileMemoryControlState,
  createMemoryControlState,
  memoryControlState,
} from '../lib/state.js'

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return JSON.stringify(payload) },
  }
}

function fixtureHarness(fetchImpl) {
  const tools = new Map()
  const listeners = new Map()
  return {
    tools: {
      register(tool) {
        tools.set(tool.name, tool)
        return () => tools.delete(tool.name)
      },
    },
    on(name, handler) {
      listeners.set(name, handler)
      return () => listeners.delete(name)
    },
    async emit(name, ...args) {
      return listeners.get(name)?.(...args)
    },
    registered: tools,
    fetchImpl,
  }
}

test('request evidence hashes only the provider-visible DeepSeek payload', () => {
  const request = {
    provider: 'deepseek-official',
    model: 'deepseek-v4-flash',
    reasoningEffort: 'high',
    maxTokens: 1_200,
    sessionId: 'cold-random-session',
    system: 'stable system',
    messages: [{
      id: 'cold-random-message',
      role: 'user',
      source: { kind: 'user', uiOnly: 'cold metadata' },
      content: [{ type: 'text', text: 'stable task' }],
    }],
    tools: [{ name: 'read', description: 'Read a file', parameters: { type: 'object' } }],
  }
  const warm = structuredClone(request)
  warm.sessionId = 'warm-random-session'
  warm.messages[0].id = 'warm-random-message'
  warm.messages[0].source.uiOnly = 'warm metadata'

  assert.deepEqual(harnessRequestEvidence(request), harnessRequestEvidence(warm))
  assert.deepEqual(Object.keys(harnessRequestEvidence(request).components), [
    'system',
    'messages',
    'tools',
    'generation',
  ])
  assert.deepEqual(deepSeekVisibleRequest(request), {
    model: 'deepseek-v4-flash',
    messages: [
      { role: 'system', content: 'stable system' },
      { role: 'user', content: 'stable task' },
    ],
    stream: true,
    stream_options: { include_usage: true },
    thinking: { type: 'enabled' },
    reasoning_effort: 'high',
    tools: [{
      type: 'function',
      function: {
        name: 'read',
        description: 'Read a file',
        parameters: { type: 'object' },
      },
    }],
    max_tokens: 1_200,
  })

  warm.messages[0].content[0].text = 'changed task'
  assert.notEqual(harnessRequestEvidence(request).sha256, harnessRequestEvidence(warm).sha256)
})

test('write-token accounting prices only model-visible write schemas and replayed results', () => {
  const request = {
    model: 'deepseek-v4-flash',
    tools: [
      {
        name: 'memory_propose',
        description: 'Propose one fact',
        parameters: { type: 'object', properties: { key: { type: 'string' } } },
      },
      {
        name: 'memory_confirm',
        description: 'Confirm one fact',
        parameters: { type: 'object', properties: { memory_id: { type: 'string' } } },
      },
      { name: 'bash', description: 'Run a command', parameters: { type: 'object' } },
    ],
    messages: [
      {
        role: 'assistant',
        content: [
          { type: 'tool-call', id: 'write-call', name: 'memory_propose', arguments: '{}' },
          { type: 'tool-call', id: 'bash-call', name: 'bash', arguments: '{}' },
        ],
      },
      {
        role: 'user',
        content: [
          {
            type: 'tool-result',
            toolCallId: 'write-call',
            content: [{ type: 'text', text: 'candidate_memory_id=memory-1' }],
          },
          {
            type: 'tool-result',
            toolCallId: 'bash-call',
            content: [{ type: 'text', text: 'workspace output is excluded' }],
          },
        ],
      },
    ],
  }
  const accounting = memoryWriteTokenAccounting(request)
  assert.equal(accounting.tokenizer_id, 'unicode-heuristic-v1')
  assert.equal(accounting.tokenizer_kind, 'estimated')
  assert.ok(accounting.write_tool_schema_tokens > 0)
  assert.ok(accounting.memory_write_result_tokens > 0)
  assert.equal(
    accounting.memory_write_visible_tokens,
    accounting.write_tool_schema_tokens + accounting.memory_write_result_tokens,
  )

  const unrelated = structuredClone(request)
  unrelated.tools[2].description = 'x'.repeat(10_000)
  unrelated.messages[1].content[1].content[0].text = 'x'.repeat(10_000)
  assert.deepEqual(memoryWriteTokenAccounting(unrelated), accounting)

  const largerMemoryResult = structuredClone(request)
  largerMemoryResult.messages[1].content[0].content[0].text += 'x'.repeat(100)
  assert.ok(
    memoryWriteTokenAccounting(largerMemoryResult).memory_write_result_tokens
      > accounting.memory_write_result_tokens,
  )
})

test('fixture Harness calls MSC tools, carries delta state, and writes exact usage', async () => {
  const requests = []
  let contextCall = 0
  const fetchImpl = async (url, options) => {
    requests.push({ url, options, body: options.body ? JSON.parse(options.body) : undefined })
    if (url.includes('/api/context')) {
      contextCall += 1
      if (contextCall === 1) {
        return response({
          schema_version: '2.3',
          mode: 'full',
          context_id: 'internal-random-context-1',
          retrieval_run_id: 'internal-retrieval-1',
          selection_latency_ms: 4.2,
          text: 'Project Memory Context v2.3\n- [memory-1 @ atom] add returns a sum',
          usage: { delivered_payload_tokens: 123 },
        })
      }
      return response({
        schema_version: '2.3',
        mode: 'delta',
        context_id: 'internal-random-context-2',
        requires_base_context_id: 'internal-random-context-1',
        retrieval_run_id: 'internal-retrieval-2',
        text: 'Memory Context Delta v2.3\nNo memory-context changes.',
        usage: { delivered_payload_tokens: 42 },
      })
    }
    if (url.includes('/api/memories/memory-1/explain')) {
      return response({ memory_id: 'memory-1', sections: { evidence: ['fixture evidence'] } })
    }
    return response({ error: 'not found' }, 404)
  }
  const harness = fixtureHarness(fetchImpl)
  const usage = []
  const written = []
  const attempts = []
  const config = {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_delta',
    task: 'Fix add',
    repository: 'calculator-fixture',
    runId: 'contract-run',
    taskId: 'calculator-task',
    cachePhase: 'warm',
    provider: 'deepseek',
    model: 'deepseek-v4-flash',
    cacheNamespaceSha256: 'a'.repeat(64),
    usageOutputFile: '/virtual/provider-usage.jsonl',
    attemptOutputFile: '/virtual/provider-attempts.jsonl',
    pricing: {
      cacheMissInputUsdPerMillion: 0.14,
      cacheHitInputUsdPerMillion: 0.0028,
      outputUsdPerMillion: 0.28,
    },
  }
  registerMemoryOSPlugin(harness, config, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  registerMemoryOSUsage(harness, config, {
    onUsage: value => usage.push(value),
    appendFile: async (...args) => { written.push(args) },
    appendAttempt: (...args) => { attempts.push(args) },
  })

  assert.deepEqual(
    [...harness.registered.keys()],
    ['memoryos_control', 'memory_context', 'memory_explain'],
  )
  const exec = {
    agent: { id: 'session-1', session: { id: 'session-1' } },
    signal: new AbortController().signal,
  }
  const first = JSON.parse(await harness.registered.get('memory_context').execute({}, exec))
  const second = JSON.parse(await harness.registered.get('memory_context').execute({}, exec))
  assert.match(first.result.context.text, /Project Memory Context v2\.3/u)
  assert.equal(first.result.context.retrieval_run_id, undefined)
  assert.equal(first.result.context.selection_latency_ms, undefined)
  assert.equal(first.result.context.usage, undefined)
  assert.match(first.result.context.context_id, /^[0-9a-f]{64}$/u)
  assert.equal(second.result.context.mode, 'delta')
  assert.equal(second.result.context.requires_base_context_id, first.result.context.context_id)
  assert.equal(requests[1].body.previous_context_id, 'internal-random-context-1')

  const explanation = JSON.parse(await harness.registered.get('memory_explain').execute(
    { memory_id: 'memory-1', sections: ['evidence'] },
    exec,
  ))
  assert.deepEqual(explanation.result.sections.evidence, ['fixture evidence'])

  const session = { id: 'session-1' }
  await harness.emit('session/event', session, {
    type: 'step/start', time: 1_000, data: { turn: 1, step: 0 },
  })
  await harness.emit('llm/stream', {
    provider: 'deepseek-official',
    model: 'deepseek-v4-flash',
    sessionId: 'session-1',
    system: 'stable system',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'stable task' }] }],
    tools: [],
  }, () => 'delegated')
  await harness.emit('session/event', session, {
    type: 'assistant/chunk',
    time: 1_100,
    data: { turn: 1, step: 0, chunk: { type: 'text-delta', text: 'x' } },
  })
  await harness.emit('session/event', session, {
    type: 'assistant/message',
    time: 1_400,
    data: {
      turn: 1,
      step: 0,
      message: { role: 'assistant', content: [] },
      usage: { inputTokens: 25, cacheReadTokens: 75, outputTokens: 10, reasoningTokens: 4 },
    },
  })
  assert.equal(usage.length, 1)
  assert.equal(usage[0].input_tokens, 100)
  assert.equal(usage[0].cache_hit_tokens, 75)
  assert.equal(usage[0].cache_miss_tokens, 25)
  assert.equal(usage[0].output_tokens, 10)
  assert.equal(usage[0].reasoning_tokens, 4)
  assert.equal(usage[0].cost_usd, 0.00000651)
  assert.equal(usage[0].ttft_seconds, 0.1)
  assert.equal(usage[0].latency_seconds, 0.4)
  assert.match(usage[0].request_sha256, /^[0-9a-f]{64}$/u)
  assert.match(usage[0].response_sha256, /^[0-9a-f]{64}$/u)
  assert.ok(usage[0].request_bytes > 0)
  assert.equal(written.length, 1)
  assert.match(written[0][1], /"usage_source":"provider_exact"/u)
  assert.equal(attempts.length, 1)
  const attempt = JSON.parse(attempts[0][1])
  assert.equal(attempt.event, 'provider_attempt')
  assert.equal(attempt.step_index, 0)
  assert.equal(attempt.attempt_index, 1)
  assert.equal(attempt.request_sha256, usage[0].request_sha256)
  assert.deepEqual(attempt.memory_write_token_accounting, {
    tokenizer_id: 'unicode-heuristic-v1',
    tokenizer_kind: 'estimated',
    counter_version: '1.0.0',
    write_tool_schema_tokens: 0,
    memory_write_result_tokens: 0,
    memory_write_visible_tokens: 0,
  })
})

test('provider attempt ledger counts retries before a successful usage record', async () => {
  const harness = fixtureHarness()
  const attempts = []
  registerMemoryOSUsage(harness, {
    condition: 'no_memory',
    runId: 'retry-run',
    taskId: 'retry-task',
    cachePhase: 'cold',
    cacheNamespaceSha256: 'd'.repeat(64),
    attemptOutputFile: '/virtual/provider-attempts.jsonl',
  }, { appendAttempt: (_path, value) => { attempts.push(JSON.parse(value)) } })

  const session = { id: 'retry-session' }
  await harness.emit('session/event', session, {
    type: 'step/start', time: 3_000, data: { turn: 1, step: 4 },
  })
  const options = {
    provider: 'deepseek-official',
    model: 'deepseek-v4-flash',
    sessionId: 'retry-session',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'retry fixture' }] }],
    tools: [],
  }
  await harness.emit('llm/stream', options, () => 'failed-attempt')
  await harness.emit('llm/stream', options, () => 'successful-retry')

  assert.deepEqual(attempts.map(item => item.attempt_index), [1, 2])
  assert.deepEqual(attempts.map(item => item.step_index), [4, 4])
})

test('controller usage guard stops before provider dispatch and attempt accounting', async () => {
  const harness = fixtureHarness()
  const attempts = []
  let delegated = 0
  registerMemoryOSUsage(harness, {
    condition: 'msc_full',
    runId: 'guard-run',
    taskId: 'guard-task',
    cachePhase: 'cold',
    cacheNamespaceSha256: 'e'.repeat(64),
    attemptOutputFile: '/virtual/provider-attempts.jsonl',
    usageGuardFile: '/virtual/usage-guard.json',
  }, {
    appendAttempt: (_path, value) => { attempts.push(JSON.parse(value)) },
    readUsageGuard: () => ({
      stop: true,
      reason: 'relative input-token ceiling crossed',
    }),
  })

  const session = { id: 'guard-session' }
  await harness.emit('session/event', session, {
    type: 'step/start', time: 4_000, data: { turn: 1, step: 2 },
  })
  await assert.rejects(
    harness.emit('llm/stream', {
      provider: 'deepseek-official',
      model: 'deepseek-v4-flash',
      sessionId: 'guard-session',
      messages: [{ role: 'user', content: [{ type: 'text', text: 'guard fixture' }] }],
      tools: [],
    }, () => { delegated += 1 }),
    /MEMORYOS_USAGE_GUARD_STOP: relative input-token ceiling crossed/u,
  )

  assert.equal(delegated, 0)
  assert.equal(attempts.length, 0)
})

test('controlled evaluation window evicts whole old turns and records sentinel visibility', async () => {
  const harness = fixtureHarness()
  const evictions = []
  const sentinel = 'Glacier-47'
  registerMemoryOSUsage(harness, {
    condition: 'no_memory',
    runId: 'eviction-run',
    taskId: 'eviction-task',
    cachePhase: 'cold',
    cacheNamespaceSha256: 'f'.repeat(64),
    evaluationHistoryCharLimit: 1_024,
    evaluationEvictionOutputFile: '/virtual/context-evictions.jsonl',
    evaluationSentinel: sentinel,
  }, {
    appendEviction: (_path, value) => { evictions.push(JSON.parse(value)) },
  })

  const messages = [
    { role: 'user', content: [{ type: 'text', text: `${sentinel} ${'a'.repeat(650)}` }] },
    { role: 'assistant', content: [{ type: 'text', text: 'b'.repeat(650) }] },
    { role: 'user', content: [{ type: 'text', text: 'c'.repeat(320) }] },
    { role: 'assistant', content: [{ type: 'text', text: 'd'.repeat(320) }] },
  ]
  const events = messages.map((message, seq) => ({
    seq,
    type: message.role === 'user' ? 'user/message' : 'assistant/message',
  }))
  const appended = []
  const session = {
    id: 'eviction-session',
    events,
    surface: { nodes: [0, 1, 2, 3] },
    deriveMessages: () => messages,
    append(type, data, intent) {
      appended.push({ type, data, intent })
      return { seq: 4, type, data, ...intent }
    },
  }
  const delegated = await harness.emit(
    'agent/pre-step',
    { agent: { session }, step: 1 },
    async () => 'delegated',
  )

  assert.equal(delegated, 'delegated')
  assert.equal(appended.length, 1)
  assert.equal(appended[0].type, 'user/message')
  assert.deepEqual(appended[0].intent.surfaceOp, { op: 'replace', start: 0, end: 1 })
  assert.deepEqual(appended[0].intent.sourceEventSeqs, [0, 1])
  assert.match(appended[0].data.content[0].text, /not present in the active model context/u)
  assert.equal(evictions.length, 1)
  assert.equal(evictions[0].shadowed_contains_sentinel, true)
  assert.equal(evictions[0].retained_contains_sentinel, false)
  assert.equal(evictions[0].shadowed_message_count, 2)
  assert.equal(evictions[0].retained_message_count, 2)
})

test('DeepSeek compact mode exposes one argument-free context call and text only', async () => {
  const requests = []
  const fetchImpl = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) })
    return response({
      schema_version: '2.3',
      mode: 'full',
      context_id: 'internal-context-id',
      retrieval_run_id: 'hidden-retrieval-id',
      text: 'Use the nominal scale contract during figure finalization.',
      usage: { delivered_payload_tokens: 80 },
    })
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'Fix nominal axes',
    repository: 'mwaskom-seaborn',
    budgetTokens: 512,
    maxContextCalls: 1,
    responseFormat: 'deepseek-compact',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })

  assert.deepEqual([...harness.registered.keys()], ['memoryos_control', 'memory_context'])
  const tool = harness.registered.get('memory_context')
  assert.deepEqual(tool.parameters, {})
  assert.match(tool.description, /Call once/u)
  const exec = {
    agent: { id: 'session-compact', session: { id: 'session-compact' } },
    signal: new AbortController().signal,
  }
  const value = await tool.execute({}, exec)
  assert.match(value, /nominal scale contract/u)
  assert.match(value, /verify against repository code/u)
  assert.doesNotMatch(value, /context_id|experiment|retrieval_run_id|"ok"/u)
  assert.equal(requests[0].body.budget_tokens, 512)
  await assert.rejects(
    tool.execute({}, exec),
    /limited to 1 call/u,
  )
  assert.equal(requests.length, 1)
})

test('first successful context announces activation once and failed context does not announce', async () => {
  const store = createMemoryControlState(memoryControlState(true, false))
  const fetchImpl = async () => response({
    schema_version: '2.3',
    mode: 'full',
    context_id: 'activation-context',
    text: 'The durable project decision is available.',
  })
  const harness = fixtureHarness(fetchImpl)
  const plugin = registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'Continue the project',
    repository: 'fixture://activation',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
    stateStore: store,
  })
  const exec = {
    agent: { id: 'activation-session', session: { id: 'activation-session' } },
    signal: new AbortController().signal,
  }

  const first = JSON.parse(await harness.registered.get('memory_context').execute({}, exec))
  const second = JSON.parse(await harness.registered.get('memory_context').execute({}, exec))
  assert.equal(first.first_activation, true)
  assert.match(first.user_message, /MemoryOS 已开始工作/u)
  assert.match(first.user_message, /关闭 OS/u)
  assert.match(first.user_message, /开启 OS/u)
  assert.equal(second.first_activation, undefined)
  assert.equal(plugin.controller.onboardingNoticeShown, true)
  assert.equal(store.read().state.onboarding_notice_shown, true)

  const failedStore = createMemoryControlState(memoryControlState(true, false))
  const failedHarness = fixtureHarness(async () => { throw new Error('connection refused') })
  registerMemoryOSPlugin(failedHarness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'Continue the project',
    repository: 'fixture://activation-failure',
  }, {
    defineTool: value => value,
    fetchImpl: failedHarness.fetchImpl,
    environment: {},
    stateStore: failedStore,
  })
  await assert.rejects(
    failedHarness.registered.get('memory_context').execute({}, exec),
    /connection refused/u,
  )
  assert.equal(failedStore.read().state.onboarding_notice_shown, false)
})

test('memoryos_control disables and re-enables model-visible memory tools', async () => {
  let healthCalls = 0
  const fetchImpl = async url => {
    if (url.endsWith('/api/health')) {
      healthCalls += 1
      return response({ ok: true, version: '2.3.0', database: 'ok' })
    }
    return response({ schema_version: '2.3', mode: 'full', text: 'context' })
  }
  const store = createMemoryControlState(memoryControlState(true, false))
  const harness = fixtureHarness(fetchImpl)
  const plugin = registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'Continue the project',
    repository: 'fixture://control',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
    stateStore: store,
  })
  const control = harness.registered.get('memoryos_control')
  const exec = {
    agent: { id: 'control-session', session: { id: 'control-session' } },
    signal: new AbortController().signal,
  }

  assert.match(control.description, /开启 OS/u)
  assert.match(control.description, /关闭 OS/u)
  assert.deepEqual(control.parameters.action.enum, ['enable', 'disable', 'status'])
  assert.deepEqual([...harness.registered.keys()], ['memoryos_control', 'memory_context'])

  const disabled = await control.execute({ action: 'disable' }, exec)
  assert.match(disabled, /MemoryOS 已关闭/u)
  assert.match(disabled, /已经进入当前聊天的内容无法撤回/u)
  assert.deepEqual([...harness.registered.keys()], ['memoryos_control'])
  assert.equal(plugin.controller.enabled, false)
  assert.equal(store.read().state.enabled, false)
  assert.match(await control.execute({ action: 'status' }, exec), /当前已关闭/u)

  const enabled = await control.execute({ action: 'enable' }, exec)
  assert.match(enabled, /MemoryOS 已重新开启/u)
  assert.deepEqual([...harness.registered.keys()], ['memoryos_control', 'memory_context'])
  assert.equal(plugin.controller.enabled, true)
  assert.equal(store.read().state.enabled, true)
  assert.equal(healthCalls, 1)
})

test('memoryos_control fails closed when enable health check fails', async () => {
  const store = createMemoryControlState(memoryControlState(false, false))
  const fetchImpl = async () => response({
    ok: false,
    error: { code: 'SERVICE_UNAVAILABLE', message: 'MemoryOS is unavailable' },
  }, 503)
  const harness = fixtureHarness(fetchImpl)
  const plugin = registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    enabled: false,
    condition: 'msc_context_only',
    task: 'Continue the project',
    repository: 'fixture://control-failure',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
    stateStore: store,
  })
  const control = harness.registered.get('memoryos_control')
  await assert.rejects(
    control.execute({ action: 'enable' }, { signal: new AbortController().signal }),
    /SERVICE_UNAVAILABLE/u,
  )
  assert.deepEqual([...harness.registered.keys()], ['memoryos_control'])
  assert.equal(plugin.controller.enabled, false)
  assert.equal(store.read().state.enabled, false)
})

test('file control state survives restart and corrupt state disables memory', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-memoryos-control-'))
  const statePath = join(root, 'profile', 'state.json')
  const fetchImpl = async () => response({ ok: true, version: '2.3.0', database: 'ok' })
  const config = {
    baseUrl: 'http://memoryos.invalid',
    enabled: true,
    condition: 'msc_context_only',
    task: 'Continue the project',
    repository: 'fixture://persistent-control',
    stateFile: statePath,
  }
  const exec = { signal: new AbortController().signal }
  try {
    const firstHarness = fixtureHarness(fetchImpl)
    registerMemoryOSPlugin(firstHarness, config, {
      defineTool: value => value,
      fetchImpl,
      environment: {},
      stateStore: createFileMemoryControlState(statePath),
    })
    await firstHarness.registered.get('memoryos_control').execute({ action: 'disable' }, exec)
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).enabled, false)

    const restartedHarness = fixtureHarness(fetchImpl)
    const restarted = registerMemoryOSPlugin(restartedHarness, config, {
      defineTool: value => value,
      fetchImpl,
      environment: {},
      stateStore: createFileMemoryControlState(statePath),
    })
    assert.deepEqual([...restartedHarness.registered.keys()], ['memoryos_control'])
    assert.equal(restarted.controller.enabled, false)
    await restartedHarness.registered.get('memoryos_control').execute({ action: 'enable' }, exec)
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).enabled, true)

    await writeFile(statePath, '{not valid json', 'utf8')
    const corruptHarness = fixtureHarness(fetchImpl)
    const corrupt = registerMemoryOSPlugin(corruptHarness, config, {
      defineTool: value => value,
      fetchImpl,
      environment: {},
      stateStore: createFileMemoryControlState(statePath),
    })
    assert.deepEqual([...corruptHarness.registered.keys()], ['memoryos_control'])
    assert.equal(corrupt.controller.enabled, false)
    assert.match(corrupt.controller.stateWarning, /state was invalid/u)
    assert.match(
      await corruptHarness.registered.get('memoryos_control').execute({ action: 'status' }, exec),
      /warning=/u,
    )
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('progressive compact auto-expands one resolved record into an action-ready contract', async () => {
  const memoryId = 'ee9019a4-9ba7-40f4-883d-188ba0c6b72f'
  const atom = '6fde4c87a36855f2690f6b98605a043cd35b713512bc22baa036d2980ce970de'
  const requests = []
  const fetchImpl = async (url, options) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : undefined })
    if (url.includes('/api/context')) {
      return response({
        schema_version: '2.3',
        mode: 'full',
        context_id: 'volatile-context-id',
        retrieval_run_id: 'volatile-retrieval-id',
        text: [
          'Project Memory Context v2.3',
          `- [${memoryId} @ ${atom}] Relevant record`,
          '  record; state=resolved/unknown; policy=ephemeral; evidence=1; details=memory_explain',
        ].join('\n'),
        usage: { delivered_payload_tokens: 288 },
      })
    }
    return response({
      schema_version: '2.3',
      memory_id: memoryId,
      atom_sha256: atom,
      sections: {
        fact: [{ fact: 'Read inherited marks without copying them onto the child.' }],
        evidence: [{
          claim_id: 'volatile-claim-id',
          source_ref: 'git:volatile-source',
          excerpt: 'Read each class own dictionary in closest-first MRO order.',
          path: 'src/_pytest/mark/structures.py',
          line_start: 358,
          line_end: 391,
        }],
        freshness: [{
          atom_sha256: atom,
          truth_state: 'resolved',
          freshness: 'current',
        }],
      },
      usage: { delivered_payload_tokens: 552 },
    })
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_progressive',
    task: 'Fix mark lookup',
    repository: 'pytest-dev-pytest',
    budgetTokens: 1_200,
    responseFormat: 'deepseek-progressive-compact',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })

  const contextTool = harness.registered.get('memory_context')
  const explainTool = harness.registered.get('memory_explain')
  assert.deepEqual(contextTool.parameters, {})
  assert.deepEqual(Object.keys(explainTool.parameters), ['memory_id'])
  assert.match(explainTool.parameters.memory_id.description, /UUID @ SHA256/u)
  const exec = {
    agent: { id: 'progressive-compact', session: { id: 'progressive-compact' } },
    signal: new AbortController().signal,
  }
  const context = await contextTool.execute({}, exec)

  assert.match(context, /action-ready contract/u)
  assert.match(context, /status=resolved/u)
  assert.match(context, /readiness=ready_to_implement/u)
  assert.match(context, /external_lookup_required=false/u)
  assert.match(context, /Read inherited marks/u)
  assert.match(context, /closest-first MRO order/u)
  assert.match(context, /src\/_pytest\/mark\/structures\.py:358-391/u)
  assert.match(context, /Missing infrastructure limits validation/u)
  assert.match(context, /No memory_explain call is needed/u)
  assert.doesNotMatch(context, /resolved\/unknown|policy=|evidence=1/u)
  assert.doesNotMatch(context, /context_id|retrieval_run_id|delivered_payload_tokens|experiment/u)
  assert.doesNotMatch(context, /claim_id|source_ref|atom_sha256|schema_version|usage/u)
  assert.ok(context.length < 1_500)
  assert.equal(requests.length, 2)
  assert.match(requests[1].url, /\/api\/memories\/ee9019a4-9ba7-40f4-883d-188ba0c6b72f\/explain\?/u)
  assert.match(requests[1].url, new RegExp(`expected_atom_sha256=${atom}`, 'u'))
  assert.doesNotMatch(requests[1].url, /%40/u)
})

test('progressive compact splits a model-supplied UUID @ SHA256 explain handle', async () => {
  const memoryId = 'ee9019a4-9ba7-40f4-883d-188ba0c6b72f'
  const atom = '6fde4c87a36855f2690f6b98605a043cd35b713512bc22baa036d2980ce970de'
  const requests = []
  const fetchImpl = async url => {
    requests.push(url)
    return response({
      sections: {
        fact: [{ fact: 'Preserve lazy discovery.' }],
        freshness: [{ truth_state: 'resolved', freshness: 'current' }],
      },
    })
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_progressive',
    task: 'Fix lazy collection',
    repository: 'pytest-dev-pytest',
    responseFormat: 'deepseek-progressive-compact',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  const exec = {
    agent: { id: 'manual-progressive', session: { id: 'manual-progressive' } },
    signal: new AbortController().signal,
  }
  const result = await harness.registered.get('memory_explain').execute(
    { memory_id: `${memoryId} @ ${atom}` },
    exec,
  )

  assert.match(result, /Preserve lazy discovery/u)
  assert.match(requests[0], new RegExp(`/api/memories/${memoryId}/explain\\?`, 'u'))
  assert.match(requests[0], new RegExp(`expected_atom_sha256=${atom}`, 'u'))
  assert.doesNotMatch(requests[0], /%40/u)
})

test('progressive compact keeps selective explain for multiple or unresolved records', async () => {
  const requests = []
  const fetchImpl = async (url) => {
    requests.push(url)
    return response({
      schema_version: '2.3',
      mode: 'full',
      text: [
        'Project Memory Context v2.3',
        `- [${'a'.repeat(36)} @ ${'1'.repeat(64)}] First record`,
        '  record; state=resolved/unknown; policy=ephemeral; evidence=1; details=memory_explain',
        `- [${'b'.repeat(36)} @ ${'2'.repeat(64)}] Second record`,
        '  record; state=unknown/unknown; policy=ephemeral; evidence=1; details=memory_explain',
      ].join('\n'),
    })
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_progressive',
    task: 'Resolve a multi-record task',
    repository: 'fixture',
    responseFormat: 'deepseek-progressive-compact',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  const exec = {
    agent: { id: 'progressive-multiple', session: { id: 'progressive-multiple' } },
    signal: new AbortController().signal,
  }
  const context = await harness.registered.get('memory_context').execute({}, exec)
  assert.match(context, /First record/u)
  assert.match(context, /Second record/u)
  assert.match(context, /Call memory_explain once/u)
  assert.doesNotMatch(context, /resolved\/unknown/u)
  assert.equal(requests.length, 1)
})

test('resolved progressive recovery is one-time, inspection-gated, and clean-worktree-gated', async () => {
  const memoryId = 'ee9019a4-9ba7-40f4-883d-188ba0c6b72f'
  const atom = '6fde4c87a36855f2690f6b98605a043cd35b713512bc22baa036d2980ce970de'
  const fetchImpl = async url => url.includes('/api/context')
    ? response({
        schema_version: '2.3',
        mode: 'full',
        text: [
          'Project Memory Context v2.3',
          `- [${memoryId} @ ${atom}] Relevant record`,
          '  record; state=resolved/unknown; policy=ephemeral; evidence=1; details=memory_explain',
        ].join('\n'),
      })
    : response({
        sections: {
          fact: [{ fact: 'Keep the local behavior boundary.' }],
          freshness: [{ truth_state: 'resolved', freshness: 'unknown' }],
        },
      })
  let clean = true
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_progressive',
    task: 'Fix local behavior',
    repository: 'fixture',
    responseFormat: 'deepseek-progressive-compact',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
    workspaceIsClean: async () => clean,
  })
  const agent = { id: 'recovery-clean', session: { id: 'recovery-clean' } }
  const exec = { agent, signal: new AbortController().signal }
  await harness.registered.get('memory_context').execute({}, exec)

  const beforeInspection = await harness.emit(
    'tools/post-execute',
    { name: 'bash', agent },
    { isError: true, content: [{ type: 'text', text: 'ModuleNotFoundError: No module named optional' }] },
    async () => ({ kind: 'accept' }),
  )
  assert.equal(beforeInspection.additionalContexts, undefined)

  await harness.emit(
    'tools/post-execute',
    { name: 'read', agent },
    { isError: false, content: [{ type: 'text', text: 'local source' }] },
    async () => ({ kind: 'accept' }),
  )
  const recovered = await harness.emit(
    'tools/post-execute',
    { name: 'bash', agent },
    { isError: true, content: [{ type: 'text', text: 'network access is disabled' }] },
    async () => ({ kind: 'accept', additionalContexts: [] }),
  )
  assert.equal(recovered.additionalContexts.length, 1)
  assert.match(recovered.additionalContexts[0].content[0].text, /one-time execution recovery/u)
  assert.match(recovered.additionalContexts[0].content[0].text, /worktree is still unchanged/u)

  const repeated = await harness.emit(
    'tools/post-execute',
    { name: 'bash', agent },
    { isError: true, content: [{ type: 'text', text: 'could not resolve host' }] },
    async () => ({ kind: 'accept' }),
  )
  assert.equal(repeated.additionalContexts, undefined)

  clean = false
  const dirtyAgent = { id: 'recovery-dirty', session: { id: 'recovery-dirty' } }
  await harness.registered.get('memory_context').execute({}, {
    agent: dirtyAgent,
    signal: new AbortController().signal,
  })
  await harness.emit(
    'tools/post-execute',
    { name: 'grep', agent: dirtyAgent },
    { isError: false, content: [{ type: 'text', text: 'target symbol' }] },
    async () => ({ kind: 'accept' }),
  )
  const dirty = await harness.emit(
    'tools/post-execute',
    { name: 'bash', agent: dirtyAgent },
    { isError: true, content: [{ type: 'text', text: 'command not found' }] },
    async () => ({ kind: 'accept' }),
  )
  assert.equal(dirty.additionalContexts, undefined)
})

test('full compact context normalizes resolved unknown status', () => {
  const value = renderDeepSeekContext({
    context: {
      text: 'Fact; state=resolved/unknown; policy=ephemeral',
    },
  })
  assert.match(value, /status=resolved/u)
  assert.doesNotMatch(value, /resolved\/unknown/u)
})

test('progressive compact suppresses contradictory unknown freshness and duplicate text', () => {
  const value = renderDeepSeekExplanation({
    sections: {
      fact: [{ fact: 'Keep the explicit environment override.' }],
      evidence: [
        { excerpt: 'Keep the explicit environment override.' },
        {
          excerpt: 'Use the platform cache for the default.',
          source_ref: 'pylint/config/__init__.py',
        },
      ],
      freshness: [{ truth_state: 'resolved', freshness: 'unknown' }],
    },
  })

  assert.equal(value.match(/Keep the explicit environment override\./gu)?.length, 1)
  assert.match(value, /\[pylint\/config\/__init__\.py\]/u)
  assert.match(value, /Status:\n- resolved/u)
  assert.doesNotMatch(value, /resolved; unknown/u)
})

test('missing Harness surfaces fail with a pinned compatibility error', () => {
  assert.throws(
    () => assertHarnessCompatibility({}),
    /0\.1\.0-rc\.5 @ 47f943859bef60e4160492346772ded9b24f765a/u,
  )
  assert.throws(
    () => assertHarnessUsageCompatibility({}),
    /0\.1\.0-rc\.5 @ 47f943859bef60e4160492346772ded9b24f765a/u,
  )
})

test('no_memory keeps exact usage collection without exposing MemoryOS tools', async () => {
  const harness = fixtureHarness()
  const usage = []
  registerMemoryOSUsage(harness, {
    condition: 'no_memory',
    runId: 'baseline-run',
    taskId: 'baseline-task',
    cachePhase: 'cold',
    cacheNamespaceSha256: 'b'.repeat(64),
  }, { onUsage: value => usage.push(value) })

  assert.deepEqual([...harness.registered.keys()], [])
  const session = { id: 'baseline-session' }
  await harness.emit('session/event', session, {
    type: 'step/start', time: 2_000, data: { turn: 1, step: 0 },
  })
  await harness.emit('llm/stream', {
    provider: 'deepseek-official',
    model: 'neutral-model',
    sessionId: 'baseline-session',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'same prompt' }] }],
    tools: [],
  }, () => 'delegated')
  await harness.emit('session/event', session, {
    type: 'assistant/message',
    time: 2_250,
    data: {
      turn: 1,
      step: 0,
      message: { role: 'assistant', content: [] },
      usage: { inputTokens: 30, cacheReadTokens: 0, outputTokens: 4 },
    },
  })

  assert.equal(usage.length, 1)
  assert.equal(usage[0].condition, 'no_memory')
  assert.equal(usage[0].provider, 'deepseek-official')
  assert.equal(usage[0].model, 'neutral-model')
  assert.equal(usage[0].input_tokens, 30)
})

test('full-context conditions do not expose the progressive explain tool', () => {
  const harness = fixtureHarness()
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_full',
    task: 'Fix add',
    repository: 'calculator-fixture',
  }, {
    defineTool: value => value,
    fetchImpl: async () => response({}),
    environment: {},
  })

  assert.deepEqual([...harness.registered.keys()], ['memoryos_control', 'memory_context'])
})

test('cross-session-write is explicit, repository-bound, and candidate-confirmed', async () => {
  const memoryId = '2af9cd62-7f77-4f3b-bcaf-fc2cdde3343f'
  const requests = []
  const fetchImpl = async (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) })
    if (url.endsWith('/api/memories')) {
      return response({
        ok: true,
        memory: { id: memoryId, status: 'candidate', title: 'Database decision' },
      })
    }
    if (url.endsWith(`/api/memories/${memoryId}/confirm`)) {
      return response({ ok: true, memory: { id: memoryId, status: 'active' } })
    }
    return response({ error: 'not found' }, 404)
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
    toolProfile: 'cross-session-write',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })

  assert.deepEqual(
    [...harness.registered.keys()],
    ['memoryos_control', 'memory_context', 'memory_propose', 'memory_confirm'],
  )
  const exec = {
    agent: {
      id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
      session: { id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6' },
    },
    signal: new AbortController().signal,
  }
  const proposed = await harness.registered.get('memory_propose').execute({
    title: 'Database decision',
    content: 'The repository uses PostgreSQL.',
    category: 'decision',
    source_excerpt: '这个仓库后续数据库统一使用 PostgreSQL。',
    key: 'database.engine',
  }, exec)
  assert.match(proposed, new RegExp(`candidate_memory_id=${memoryId}`, 'u'))
  assert.equal(requests[0].body.scope_type, 'repository')
  assert.equal(requests[0].body.scope_key, 'fixture://bound-repository')
  assert.equal(requests[0].body.created_by, 'agent')
  assert.equal(requests[0].body.memory_type, 'project')
  assert.equal(requests[0].body.source.source_type, 'conversation')
  assert.equal(
    requests[0].body.source.source_ref,
    'deepseek-harness:session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
  )

  const confirmed = await harness.registered.get('memory_confirm').execute({
    memory_id: memoryId,
    rationale: 'The user stated a lasting repository decision.',
  }, exec)
  assert.equal(confirmed, `memory_id=${memoryId}\nstatus=active`)
  assert.equal(requests[1].body.rationale, 'The user stated a lasting repository decision.')
  assert.match(requests[1].url, new RegExp(`/api/memories/${memoryId}/confirm$`, 'u'))
})

test('cross-session write schemas require atomic keys and expose conflict strategies', () => {
  const harness = fixtureHarness()
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
    toolProfile: 'cross-session-write',
  }, {
    defineTool: value => value,
    fetchImpl: async () => response({}),
    environment: {},
  })

  const propose = harness.registered.get('memory_propose')
  const confirm = harness.registered.get('memory_confirm')
  assert.equal(propose.parameters.key.required, true)
  assert.match(propose.description, /exactly one independently updateable fact/iu)
  assert.match(propose.parameters.content.description, /one atomic fact/iu)
  assert.match(propose.parameters.key.description, /reuse the exact write_key/iu)
  assert.deepEqual(confirm.parameters.strategy.enum, ['supersede', 'keep_both', 'reject'])
  assert.match(confirm.parameters.strategy.description, /same candidate/iu)
  assert.match(confirm.description, /Do not create a replacement candidate/iu)
})

test('confirm conflict is actionable and blocks candidate churn until resolution', async () => {
  const candidateId = '2af9cd62-7f77-4f3b-bcaf-fc2cdde3343f'
  const conflictId = 'bb630c2a-97d4-45bc-81aa-f4638ce5a624'
  const requests = []
  let proposalCount = 0
  const fetchImpl = async (url, options) => {
    const body = JSON.parse(options.body)
    requests.push({ url, body })
    if (url.endsWith('/api/memories')) {
      proposalCount += 1
      return response({
        ok: true,
        memory: {
          id: proposalCount === 1
            ? candidateId
            : 'a6b61374-4f8c-4f5f-9779-875bda9e12bc',
          status: 'candidate',
        },
      })
    }
    if (url.endsWith(`/api/memories/${candidateId}/confirm`) && body.strategy === undefined) {
      return response({
        ok: false,
        error: {
          code: 'CONFLICT_DETECTED',
          message: 'candidate conflicts with active memory; choose a resolution strategy',
          details: { candidate_id: candidateId, conflict_ids: [conflictId] },
        },
      }, 409)
    }
    if (url.endsWith(`/api/memories/${candidateId}/confirm`) && body.strategy === 'keep_both') {
      return response({ ok: true, memory: { id: candidateId, status: 'active' } })
    }
    return response({ error: 'not found' }, 404)
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
    toolProfile: 'cross-session-write',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  const exec = {
    agent: {
      id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
      session: { id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6' },
    },
    signal: new AbortController().signal,
  }
  const proposal = {
    title: 'One durable fact',
    content: 'The compatibility boundary remains supported.',
    category: 'constraint',
    source_excerpt: '兼容边界继续保留。',
    key: 'compatibility.boundary',
  }

  await harness.registered.get('memory_propose').execute(proposal, exec)
  const conflict = await harness.registered.get('memory_confirm').execute({
    memory_id: candidateId,
  }, exec)
  assert.match(conflict, /status=conflict/u)
  assert.match(conflict, new RegExp(`candidate_memory_id=${candidateId}`, 'u'))
  assert.match(conflict, new RegExp(`conflict_memory_ids=${conflictId}`, 'u'))
  assert.match(conflict, /allowed_strategies=supersede\|keep_both\|reject/u)
  assert.match(conflict, /do_not_call=memory_propose/u)

  const requestCountAfterConflict = requests.length
  const repeated = await harness.registered.get('memory_confirm').execute({
    memory_id: candidateId,
  }, exec)
  assert.equal(repeated, conflict)
  assert.equal(requests.length, requestCountAfterConflict)

  const blocked = await harness.registered.get('memory_propose').execute({
    ...proposal,
    title: 'Rephrased duplicate',
    content: 'Keep supporting the same compatibility boundary.',
  }, exec)
  assert.match(blocked, /proposal_blocked=pending_conflict/u)
  assert.match(blocked, new RegExp(`candidate_memory_id=${candidateId}`, 'u'))
  assert.equal(requests.length, requestCountAfterConflict)

  const resolved = await harness.registered.get('memory_confirm').execute({
    memory_id: candidateId,
    strategy: 'keep_both',
    rationale: 'Both durable facts can coexist.',
  }, exec)
  assert.equal(resolved, `memory_id=${candidateId}\nstatus=active\nconflict_resolved=true`)
  assert.equal(requests.at(-1).body.strategy, 'keep_both')

  await harness.registered.get('memory_propose').execute({
    ...proposal,
    title: 'A different atomic fact',
    content: 'A separate independently updateable constraint.',
    key: 'compatibility.separate-constraint',
  }, exec)
  assert.equal(proposalCount, 2)
})

test('nested Harness bridge conflict remains actionable to the model', async () => {
  const candidateId = '2af9cd62-7f77-4f3b-bcaf-fc2cdde3343f'
  const conflictId = 'bb630c2a-97d4-45bc-81aa-f4638ce5a624'
  const direct = {
    ok: false,
    error: {
      code: 'CONFLICT_DETECTED',
      message: 'candidate conflicts with active memory; choose a resolution strategy',
      details: { candidate_id: candidateId, conflict_ids: [conflictId] },
    },
  }
  let calls = 0
  const fetchImpl = async (url) => {
    calls += 1
    if (url.endsWith('/api/memories')) {
      return response({ ok: true, memory: { id: candidateId, status: 'candidate' } })
    }
    return response({
      error: 'RuntimeError',
      message: JSON.stringify({
        code: 'RuntimeError',
        message: `MemoryOS HTTP 409: ${JSON.stringify(direct)}`,
      }),
    }, 400)
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
    toolProfile: 'cross-session-write',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  const exec = {
    agent: {
      id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
      session: { id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6' },
    },
    signal: new AbortController().signal,
  }
  await harness.registered.get('memory_propose').execute({
    title: 'Atomic fact',
    content: 'One independently updateable fact.',
    category: 'decision',
    source_excerpt: '一个持久事实。',
    key: 'project.atomic-fact',
  }, exec)
  const conflict = await harness.registered.get('memory_confirm').execute({
    memory_id: candidateId,
  }, exec)

  assert.equal(calls, 2)
  assert.match(conflict, /status=conflict/u)
  assert.match(conflict, new RegExp(`conflict_memory_ids=${conflictId}`, 'u'))
})

test('reject resolves a pending conflict without activating the candidate', async () => {
  const candidateId = '2af9cd62-7f77-4f3b-bcaf-fc2cdde3343f'
  let confirmCalls = 0
  const fetchImpl = async (url, options) => {
    if (url.endsWith('/api/memories')) {
      return response({ ok: true, memory: { id: candidateId, status: 'candidate' } })
    }
    const body = JSON.parse(options.body)
    confirmCalls += 1
    if (body.strategy === undefined) {
      return response({
        ok: false,
        error: {
          code: 'CONFLICT_DETECTED',
          message: 'candidate conflicts with active memory',
          details: { candidate_id: candidateId, conflict_ids: ['existing-memory'] },
        },
      }, 409)
    }
    assert.equal(body.strategy, 'reject')
    return response({ ok: true, memory: { id: candidateId, status: 'rejected' } })
  }
  const harness = fixtureHarness(fetchImpl)
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
    toolProfile: 'cross-session-write',
  }, {
    defineTool: value => value,
    fetchImpl,
    environment: {},
  })
  const exec = {
    agent: {
      id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
      session: { id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6' },
    },
    signal: new AbortController().signal,
  }
  await harness.registered.get('memory_propose').execute({
    title: 'Atomic fact',
    content: 'One independently updateable fact.',
    category: 'decision',
    source_excerpt: '一个持久事实。',
    key: 'project.atomic-fact',
  }, exec)
  await harness.registered.get('memory_confirm').execute({ memory_id: candidateId }, exec)
  const rejected = await harness.registered.get('memory_confirm').execute({
    memory_id: candidateId,
    strategy: 'reject',
    rationale: 'The candidate duplicates existing durable truth.',
  }, exec)

  assert.equal(rejected, [
    `memory_id=${candidateId}`,
    'status=rejected',
    'memory_activated=false',
    'conflict_resolved=true',
  ].join('\n'))
  assert.equal(confirmCalls, 2)
})

test('non-conflict MemoryOS errors preserve their stable code and message', async () => {
  const harness = fixtureHarness()
  registerMemoryOSPlugin(harness, {
    baseUrl: 'http://memoryos.invalid',
    condition: 'msc_context_only',
    task: 'ordinary project discussion',
    repository: 'fixture://bound-repository',
  }, {
    defineTool: value => value,
    fetchImpl: async () => response({
      ok: false,
      error: {
        code: 'AUTH_REQUIRED',
        message: 'a valid local bearer token is required',
        details: {},
      },
    }, 401),
    environment: {},
  })
  const exec = {
    agent: {
      id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6',
      session: { id: 'session-804f9116-1a60-4ff6-90c3-216bdd8cefc6' },
    },
    signal: new AbortController().signal,
  }

  await assert.rejects(
    harness.registered.get('memory_context').execute({}, exec),
    /MemoryOS AUTH_REQUIRED \(HTTP 401\): a valid local bearer token is required/u,
  )
})
