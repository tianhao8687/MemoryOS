import assert from 'node:assert/strict'
import test from 'node:test'
import {
  assertHarnessCompatibility,
  assertHarnessUsageCompatibility,
  deepSeekVisibleRequest,
  harnessRequestEvidence,
} from '../lib/core.js'
import {
  registerMemoryOSPlugin,
  renderDeepSeekContext,
  renderDeepSeekExplanation,
} from '../lib/plugin.js'
import { registerMemoryOSUsage } from '../lib/usage.js'

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

  assert.deepEqual([...harness.registered.keys()], ['memory_context', 'memory_explain'])
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

  assert.deepEqual([...harness.registered.keys()], ['memory_context'])
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

  assert.deepEqual([...harness.registered.keys()], ['memory_context'])
})
