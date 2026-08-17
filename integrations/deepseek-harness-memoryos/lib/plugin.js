import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import {
  canonicalJson,
  createMemoryOSClient,
  isMemoryOSConflict,
  normalizeConfig,
} from './core.js'

const EXPLAIN_CONDITIONS = new Set(['msc_progressive', 'msc_delta', 'msc_delta_core'])
const PROGRESSIVE_COMPACT = 'deepseek-progressive-compact'

export function registerMemoryOSPlugin(ctx, rawConfig, dependencies) {
  const config = normalizeConfig(rawConfig)
  const defineTool = dependencies.defineTool
  if (typeof defineTool !== 'function') throw new Error('DeepSeek Harness defineTool is unavailable')
  const client = createMemoryOSClient(config, dependencies)
  const actionStates = new Map()
  const writeStates = new Map()
  const contextParameters = {
    ...(config.task === undefined
      ? { task: { type: 'string', required: true, description: 'Current task' } }
      : {}),
    ...(config.repository === undefined
      ? { repository: { type: 'string', required: true, description: 'Repository scope' } }
      : {}),
  }

  ctx.tools.register(defineTool({
    name: 'memory_context',
    description: contextDescription(config.responseFormat),
    parameters: contextParameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const sessionId = executionSessionId(exec)
      const value = await client.context(args, sessionId, exec.signal)
      if (config.responseFormat === 'deepseek-compact') return renderDeepSeekContext(value)
      if (config.responseFormat === PROGRESSIVE_COMPACT) {
        const actionContract = await resolvedProgressiveContract(value, client, exec.signal)
        const state = actionStates.get(sessionId) ?? recoveryState()
        state.actionReady = actionContract !== undefined
        actionStates.set(sessionId, state)
        if (actionContract !== undefined) return actionContract
        return renderDeepSeekProgressiveContext(value)
      }
      return canonicalJson({ ok: true, result: value })
    },
  }))

  if (config.toolProfile === 'cross-session-write') {
    ctx.tools.register(defineTool({
      name: 'memory_propose',
      description: [
        'Propose source-backed project knowledge for future sessions only when the user has',
        'established a durable decision, constraint, or rejected approach. Do not save temporary',
        'environment details, unresolved choices, current task state, or facts recoverable from code.',
        'Each proposal must contain exactly one independently updateable fact with one stable semantic',
        'key. Split facts that can change independently into separate proposals. The proposal remains',
        'inactive until memory_confirm is called.',
      ].join(' '),
      parameters: {
        title: {
          type: 'string',
          required: true,
          description: 'Short neutral title for the durable project knowledge',
        },
        content: {
          type: 'string',
          required: true,
          description: 'One atomic fact only: a self-contained durable statement with its boundary and essential rationale',
        },
        category: {
          type: 'string',
          required: true,
          enum: ['decision', 'constraint', 'failed_approach'],
          description: 'Durable knowledge category',
        },
        source_excerpt: {
          type: 'string',
          required: true,
          description: 'Exact user excerpt supporting only this atomic fact, without unrelated facts',
        },
        key: {
          type: 'string',
          required: true,
          description: 'Stable dot-separated semantic key for this one fact. For an update, reuse the exact write_key shown by memory_context; independently changeable facts require different keys',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      timeoutMs: config.timeoutMs,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const sessionId = executionSessionId(exec)
        const state = sessionWriteState(writeStates, sessionId)
        if (state.pendingConflict !== undefined) {
          return renderProposalBlocked(state.pendingConflict)
        }
        const value = await client.propose(args, sessionId, exec.signal)
        const memoryId = nonEmptyText(value?.id)
        if (memoryId === undefined || value?.status !== 'candidate') {
          throw new Error('MemoryOS returned an invalid candidate memory')
        }
        return [
          `candidate_memory_id=${memoryId}`,
          'status=candidate',
          `atomic_fact_key=${args.key}`,
          'Review the candidate against the user statement, then call memory_confirm with this exact id only if it is accurate.',
        ].join('\n')
      },
    }))

    ctx.tools.register(defineTool({
      name: 'memory_confirm',
      description: [
        'Activate one accurate candidate returned by memory_propose. Never confirm a candidate that',
        'turns temporary, unresolved, or repository-readable information into lasting project truth.',
        'On a conflict, call memory_confirm again for the same candidate with one strategy and a',
        'rationale. Do not create a replacement candidate to recover from a conflict.',
      ].join(' '),
      parameters: {
        memory_id: {
          type: 'string',
          required: true,
          description: 'Exact candidate_memory_id returned by memory_propose',
        },
        rationale: {
          type: 'string',
          description: 'Short reason the candidate is durable and accurate; explain the choice when strategy is set',
        },
        strategy: {
          type: 'string',
          enum: ['supersede', 'keep_both', 'reject'],
          description: 'Conflict resolution for the same candidate: supersede only when it replaces old truth; keep_both when both durable facts can coexist; reject when this candidate is wrong, temporary, or redundant',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      timeoutMs: config.timeoutMs,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const sessionId = executionSessionId(exec)
        const state = sessionWriteState(writeStates, sessionId)
        if (state.pendingConflict !== undefined) {
          if (args.memory_id !== state.pendingConflict.candidateId) {
            return renderConfirmationBlocked(state.pendingConflict)
          }
          if (args.strategy === undefined) return renderConfirmConflict(state.pendingConflict)
        }
        let value
        try {
          value = await client.confirm(args, exec.signal)
        } catch (error) {
          if (!isMemoryOSConflict(error)) throw error
          const conflict = conflictState(error, args.memory_id)
          state.pendingConflict = conflict
          return renderConfirmConflict(conflict)
        }
        const memoryId = nonEmptyText(value?.id)
        if (memoryId === undefined || !['active', 'rejected'].includes(value?.status)) {
          throw new Error('MemoryOS did not resolve the candidate memory')
        }
        const conflictResolved = args.strategy !== undefined || state.pendingConflict !== undefined
        state.pendingConflict = undefined
        if (value.status === 'rejected') {
          return [
            `memory_id=${memoryId}`,
            'status=rejected',
            'memory_activated=false',
            'conflict_resolved=true',
          ].join('\n')
        }
        return [
          `memory_id=${memoryId}`,
          'status=active',
          ...(conflictResolved ? ['conflict_resolved=true'] : []),
        ].join('\n')
      },
    }))
  }

  if (EXPLAIN_CONDITIONS.has(config.condition)) {
    ctx.tools.register(defineTool({
      name: 'memory_explain',
      description: config.responseFormat === PROGRESSIVE_COMPACT
        ? 'Expand one indexed record only when memory_context says expansion is needed. Do not call after it returns an action-ready contract.'
        : 'Expand one MemoryOS record selected from an indexed context.',
      parameters: config.responseFormat === PROGRESSIVE_COMPACT
        ? {
            memory_id: {
              type: 'string',
              required: true,
              description: 'Exact UUID @ SHA256 handle shown by memory_context',
            },
          }
        : {
            memory_id: { type: 'string', required: true, description: 'Memory handle UUID' },
            expected_atom_sha256: {
              type: 'string',
              description: 'Optional atom fingerprint from memory_context',
            },
            sections: {
              type: 'array',
              items: { type: 'string' },
              description: 'Optional evidence sections',
            },
            budget_tokens: {
              type: 'integer',
              description: 'Optional evidence budget',
            },
          },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      timeoutMs: config.timeoutMs,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        const request = config.responseFormat === PROGRESSIVE_COMPACT
          ? progressiveExplainRequest(args)
          : args
        const value = await client.explain(request, exec.signal)
        return config.responseFormat === PROGRESSIVE_COMPACT
          ? renderDeepSeekExplanation(value)
          : canonicalJson({ ok: true, result: value })
      },
    }))
  }

  registerOfflineRecovery(ctx, config, actionStates, dependencies)

  return Object.freeze({ client, config, actionStates, writeStates })
}

function sessionWriteState(states, sessionId) {
  const current = states.get(sessionId)
  if (current !== undefined) return current
  const state = { pendingConflict: undefined }
  states.set(sessionId, state)
  return state
}

function conflictState(error, fallbackCandidateId) {
  const candidateId = nonEmptyText(error?.details?.candidate_id) ?? fallbackCandidateId
  const conflictIds = Array.isArray(error?.details?.conflict_ids)
    ? error.details.conflict_ids.map(nonEmptyText).filter(Boolean)
    : []
  return { candidateId, conflictIds }
}

function renderConfirmConflict(conflict) {
  return [
    `candidate_memory_id=${conflict.candidateId}`,
    'status=conflict',
    'memory_activated=false',
    `conflict_memory_ids=${conflict.conflictIds.join(',') || 'unknown'}`,
    'allowed_strategies=supersede|keep_both|reject',
    'strategy_guidance=supersede only for explicit replacement; keep_both for coexisting durable facts; reject for an inaccurate, temporary, or redundant candidate',
    'next_action=call memory_confirm again with the same candidate_memory_id, one strategy, and a rationale',
    'do_not_call=memory_propose',
  ].join('\n')
}

function renderProposalBlocked(conflict) {
  return [
    'proposal_blocked=pending_conflict',
    `candidate_memory_id=${conflict.candidateId}`,
    'next_action=resolve the pending candidate with memory_confirm before proposing another fact',
    'allowed_strategies=supersede|keep_both|reject',
  ].join('\n')
}

function renderConfirmationBlocked(conflict) {
  return [
    'confirmation_blocked=pending_conflict',
    `candidate_memory_id=${conflict.candidateId}`,
    'next_action=resolve this candidate first with memory_confirm and one strategy',
    'allowed_strategies=supersede|keep_both|reject',
  ].join('\n')
}

export function renderDeepSeekContext(value) {
  const text = value?.context?.text
  if (typeof text !== 'string' || text.trim().length === 0) {
    return 'MemoryOS found no relevant project context. Inspect the repository directly.'
  }
  return [
    'MemoryOS project context (verify against repository code):',
    normalizeContextStatusText(text),
    'Use this only to narrow inspection. Do not call MemoryOS again.',
  ].join('\n')
}

export function renderDeepSeekProgressiveContext(value) {
  const text = value?.context?.text
  if (typeof text !== 'string' || text.trim().length === 0) {
    return 'MemoryOS found no relevant project records. Inspect the repository directly.'
  }
  return [
    'MemoryOS project index (expand only the record needed for this task):',
    compactProgressiveIndexText(text),
    'Call memory_explain once with that record\'s exact UUID @ SHA256 handle before broad inspection.',
  ].join('\n')
}

export function renderDeepSeekExplanation(value) {
  const sections = value?.sections
  if (!sections || typeof sections !== 'object' || Array.isArray(sections)) {
    return 'MemoryOS returned no usable evidence. Inspect the repository directly.'
  }
  const lines = ['MemoryOS task contract:']
  const rendered = new Set()
  appendSectionLines(lines, 'Facts', sections.fact, item => item?.fact, rendered)
  appendEvidenceLines(lines, sections.evidence, rendered)
  appendSectionLines(lines, 'History', sections.history, item => (
    item?.summary ?? item?.fact ?? item?.excerpt
  ), rendered)
  const freshness = Array.isArray(sections.freshness)
    ? sections.freshness
      .map(renderStatus)
      .filter(Boolean)
    : []
  if (freshness.length > 0) {
    lines.push('Status:')
    for (const item of new Set(freshness)) lines.push(`- ${item}`)
  }
  if (lines.length > 1) {
    lines.push(
      'Use boundary: treat related clauses as one contract. Check the named symbols or nearest matching code once; when they match, proceed to implementation and tests.',
    )
  }
  return lines.length === 1
    ? 'MemoryOS returned no usable evidence. Inspect the repository directly.'
    : lines.join('\n')
}

export function renderDeepSeekActionContract(value) {
  const parts = contractParts(value)
  if (parts.constraints.length === 0) return undefined
  const lines = [
    'MemoryOS action-ready contract (verify against local code):',
    'status=resolved',
    'readiness=ready_to_implement',
    'external_lookup_required=false',
    'Resolved constraints:',
    ...parts.constraints.map(item => `- ${item}`),
    'Target anchors:',
    ...(parts.anchors.length > 0
      ? parts.anchors.map(item => `- ${item}`)
      : ['- No local path was stored; locate the nearest matching symbol once.']),
    'Open choices:',
    '- The memory fixes behavior and boundaries, not code shape; choose the smallest implementation consistent with local code.',
    'Validation fallback:',
    '- If network or optional dependencies are unavailable, use focused runnable tests plus local/static checks. Missing infrastructure limits validation; it does not by itself block implementation.',
    'Reopen condition:',
    '- Investigate further only if local code contradicts a resolved constraint or a focused check exposes unresolved behavior.',
    'No memory_explain call is needed. Inspect the anchors or nearest matching code once, then implement and test.',
  ]
  return lines.join('\n')
}

function contextDescription(responseFormat) {
  if (responseFormat === 'deepseek-compact') {
    return 'One-shot project context. Call once for previously established project decisions or before broad search, then verify code-related facts in code.'
  }
  if (responseFormat === PROGRESSIVE_COMPACT) {
    return 'Retrieve compact project memory for previously established decisions or before code inspection. One resolved record is expanded automatically; otherwise expand only needed evidence.'
  }
  return 'Retrieve task-relevant project memory before answering about previously established decisions, or before inspecting or editing code.'
}

function appendSectionLines(lines, label, items, select, rendered = new Set()) {
  if (!Array.isArray(items)) return
  const values = uniqueUnrendered(items.map(select), rendered)
  if (values.length === 0) return
  lines.push(`${label}:`)
  for (const value of values) lines.push(`- ${value}`)
}

function appendEvidenceLines(lines, items, rendered) {
  if (!Array.isArray(items)) return
  const values = []
  for (const item of items) {
    const excerpt = nonEmptyText(item?.excerpt)
    if (excerpt === undefined || rendered.has(excerpt)) continue
    rendered.add(excerpt)
    const anchor = evidenceAnchor(item)
    values.push(anchor === undefined ? excerpt : `[${anchor}] ${excerpt}`)
  }
  if (values.length === 0) return
  lines.push('Evidence:')
  for (const value of values) lines.push(`- ${value}`)
}

function evidenceAnchor(item) {
  const path = nonEmptyText(item?.observed_path) ?? nonEmptyText(item?.path)
  if (path !== undefined) {
    const start = sourceLine(item?.observed_line_start) ?? sourceLine(item?.line_start)
    const end = sourceLine(item?.observed_line_end) ?? sourceLine(item?.line_end)
    if (start === undefined) return path
    return end === undefined || end === start ? `${path}:${start}` : `${path}:${start}-${end}`
  }
  const source = nonEmptyText(item?.source_ref)
  return source !== undefined && isLocalSourceReference(source) ? source : undefined
}

function sourceLine(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : undefined
}

function isLocalSourceReference(value) {
  if (/^[a-z][a-z0-9+.-]*:/iu.test(value)) return false
  return value.includes('/') || value.includes('\\') || /\.[a-z0-9]{1,10}$/iu.test(value)
}

function renderStatus(item) {
  const truth = nonEmptyText(item?.truth_state)
  let freshness = nonEmptyText(item?.freshness)
  if (freshness === 'unknown' && truth !== undefined && truth !== 'unknown') freshness = undefined
  if (freshness === truth) freshness = undefined
  return [truth, freshness].filter(Boolean).join('; ') || undefined
}

function compactProgressiveIndexText(value) {
  return value.trim().split('\n').map(line => {
    const match = /^(\s*)[^;\n]+; state=([^/;\s]+)\/([^;\s]+); policy=[^;\n]+; evidence=\d+; (?:(write_key=[^;\n]+); )?details=memory_explain\s*$/u.exec(line)
    if (match === null) return line
    const [, indentation, truth, freshness, writeKey] = match
    const status = freshness === 'unknown' && truth !== 'unknown'
      ? truth
      : `${truth}/${freshness}`
    return `${indentation}status=${status}; ${writeKey === undefined ? '' : `${writeKey}; `}expand=memory_explain`
  }).join('\n')
}

function normalizeContextStatusText(value) {
  return value.trim().split('\n').map(line => line.replace(
    /state=([^/;\s]+)\/unknown(?=;|\s|$)/gu,
    'status=$1',
  )).join('\n')
}

async function resolvedProgressiveContract(value, client, signal) {
  const records = progressiveRecords(value?.context?.text)
  if (records.length !== 1 || records[0].truth !== 'resolved') return undefined
  let explanation
  try {
    explanation = await client.explain({
      memory_id: records[0].memoryId,
      expected_atom_sha256: records[0].atomSha256,
    }, signal)
  } catch {
    return undefined
  }
  if (explanationTruth(explanation, records[0].truth) !== 'resolved') return undefined
  return renderDeepSeekActionContract(explanation)
}

function progressiveRecords(text) {
  if (typeof text !== 'string') return []
  const lines = text.split('\n')
  const records = []
  for (let index = 0; index < lines.length; index += 1) {
    const heading = /^\s*-\s+\[([^\]\n]+)\]\s+(.+?)\s*$/u.exec(lines[index])
    if (heading === null) continue
    const state = /^\s*(?:record|fact);\s+state=([^/;\s]+)\/([^;\s]+);/u.exec(lines[index + 1] ?? '')
    if (state === null) continue
    const parsed = parseProgressiveHandle(heading[1])
    if (parsed === undefined) continue
    records.push({ ...parsed, title: heading[2].trim(), truth: state[1] })
  }
  return records
}

function progressiveExplainRequest(args) {
  const parsed = parseProgressiveHandle(args?.memory_id)
  if (parsed === undefined) return args
  return {
    ...args,
    memory_id: parsed.memoryId,
    expected_atom_sha256: parsed.atomSha256,
  }
}

function parseProgressiveHandle(value) {
  if (typeof value !== 'string') return undefined
  const match = /^\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*@\s*([0-9a-f]{64})\s*$/iu.exec(value)
  if (match === null) return undefined
  return { memoryId: match[1], atomSha256: match[2].toLowerCase() }
}

function explanationTruth(value, fallback) {
  const truth = new Set(
    (Array.isArray(value?.sections?.freshness) ? value.sections.freshness : [])
      .map(item => nonEmptyText(item?.truth_state))
      .filter(Boolean),
  )
  if (truth.has('contested')) return 'contested'
  if (truth.has('resolved')) return 'resolved'
  if (truth.size > 0) return [...truth][0]
  return fallback
}

function contractParts(value) {
  const sections = value?.sections
  if (!sections || typeof sections !== 'object' || Array.isArray(sections)) {
    return { constraints: [], anchors: [] }
  }
  const constraints = []
  const rendered = new Set()
  for (const item of sections.fact ?? []) appendContractValue(constraints, rendered, item?.fact)
  for (const item of sections.evidence ?? []) appendContractValue(constraints, rendered, item?.excerpt)
  for (const item of sections.history ?? []) {
    appendContractValue(constraints, rendered, item?.summary ?? item?.fact ?? item?.excerpt)
  }
  const anchors = [...new Set(
    (Array.isArray(sections.evidence) ? sections.evidence : [])
      .map(evidenceAnchor)
      .filter(Boolean),
  )]
  return { constraints, anchors }
}

function appendContractValue(values, rendered, raw) {
  const value = nonEmptyText(raw)
  if (value === undefined || rendered.has(value)) return
  rendered.add(value)
  values.push(value)
}

function registerOfflineRecovery(ctx, config, actionStates, dependencies) {
  if (config.responseFormat !== PROGRESSIVE_COMPACT || typeof ctx.on !== 'function') return
  const workspaceIsClean = dependencies.workspaceIsClean ?? defaultWorkspaceIsClean
  ctx.on('tools/post-execute', async (exec, result, next) => {
    const downstream = await next()
    const sessionId = optionalExecutionSessionId(exec)
    if (sessionId === undefined) return downstream
    const state = actionStates.get(sessionId)
    if (state === undefined || !state.actionReady || state.recoveryDelivered) return downstream
    if (isLocalInspection(exec?.name) && result?.isError !== true) state.inspected = true
    if (!state.inspected || !isOfflineOrDependencyFailure(toolResultText(result))) return downstream
    let clean = false
    try {
      clean = await workspaceIsClean()
    } catch {
      clean = false
    }
    if (!clean) return downstream
    state.recoveryDelivered = true
    return {
      ...downstream,
      additionalContexts: [
        offlineRecoveryMessage(),
        ...(downstream.additionalContexts ?? []),
      ],
    }
  })
}

function recoveryState() {
  return { actionReady: false, inspected: false, recoveryDelivered: false }
}

function isLocalInspection(name) {
  return typeof name === 'string' && /^(?:read|grep|glob|search|find|list|ls)$/iu.test(name)
}

function toolResultText(result) {
  const values = []
  for (const block of Array.isArray(result?.content) ? result.content : []) {
    if (block?.type === 'text' && typeof block.text === 'string') values.push(block.text)
  }
  if (typeof result?.value === 'string') values.push(result.value)
  if (typeof result?.error?.message === 'string') values.push(result.error.message)
  return values.join('\n')
}

function isOfflineOrDependencyFailure(value) {
  return /(?:temporary failure in name resolution|could not resolve host|network is unreachable|network access (?:is )?disabled|offline mode|unable to access ['"]https?:|modulenotfounderror|no module named|cannot find module|command not found|missing optional dependency|distribution not found|not installed)/iu.test(value)
}

function offlineRecoveryMessage() {
  const text = [
    'MemoryOS one-time execution recovery:',
    '- The selected memory is resolved, relevant local code has been inspected, and the worktree is still unchanged.',
    '- The latest failure indicates unavailable network access or a missing dependency. Treat that as a validation limitation, not proof that implementation is blocked.',
    '- Do not retry external lookup or reconstruct an upstream patch. Apply the smallest local change consistent with the resolved contract, then run any focused test or static/minimal check that is available.',
    '- Reopen investigation only if local code contradicts the contract or a focused check exposes unresolved behavior.',
  ].join('\n')
  const message = {
    id: randomUUID(),
    role: 'user',
    content: Object.freeze([{ type: 'text', text }]),
    source: Object.freeze({
      kind: 'plugin',
      plugin: 'dsh-memoryos',
      form: 'notice',
      summary: 'offline recovery',
    }),
  }
  return Object.freeze(message)
}

function defaultWorkspaceIsClean() {
  return new Promise(resolve => {
    execFile(
      'git',
      ['status', '--porcelain', '--untracked-files=normal', '--'],
      {
        cwd: process.cwd(),
        encoding: 'utf8',
        maxBuffer: 1_048_576,
        timeout: 5_000,
        windowsHide: true,
      },
      (error, stdout) => resolve(error === null && stdout.trim().length === 0),
    )
  })
}

function uniqueUnrendered(rawValues, rendered) {
  const values = []
  for (const raw of rawValues) {
    const value = nonEmptyText(raw)
    if (value === undefined || rendered.has(value)) continue
    rendered.add(value)
    values.push(value)
  }
  return values
}

function nonEmptyText(value) {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : undefined
}

function executionSessionId(exec) {
  const id = optionalExecutionSessionId(exec)
  if (typeof id !== 'string' || id.length === 0) {
    throw new Error('MemoryOS tools require a Harness agent/session execution context')
  }
  return id
}

function optionalExecutionSessionId(exec) {
  const id = exec?.agent?.session?.id ?? exec?.agent?.id
  return typeof id === 'string' && id.length > 0 ? id : undefined
}
