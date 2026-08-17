import assert from 'node:assert/strict'
import { access, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'
import test from 'node:test'
import { pathToFileURL } from 'node:url'

const profileDir = process.env.DSH_TEST_PROFILE_DIR
const skipReason = profileDir
  ? await missingReason(join(profileDir, 'package.json'))
  : 'set DSH_TEST_PROFILE_DIR to an installed DeepSeek Harness profile'

test('installed bundle toggles MemoryOS schemas and preserves usage through real Loader HMR', {
  skip: skipReason || false,
}, async () => {
  const requireFromProfile = createRequire(join(profileDir, '__memoryos_loader_test__.cjs'))
  const importFromProfile = async specifier => {
    const resolved = requireFromProfile.resolve(specifier)
    return import(pathToFileURL(resolved).href)
  }
  const [
    { Context },
    loaderModule,
    includeModule,
    appBootModule,
    systemPromptModule,
    toolsModule,
    memoryTools,
    memoryUsage,
    memoryResume,
  ] = (
    await Promise.all([
      importFromProfile('@deepseek-ai/cordis'),
      importFromProfile('@deepseek-ai/cordis-plugin-loader'),
      importFromProfile('@deepseek-ai/cordis-plugin-include'),
      importFromProfile('@deepseek-ai/dsh-app-boot'),
      importFromProfile('@deepseek-ai/dsh-system-prompt'),
      importFromProfile('@deepseek-ai/dsh-tools'),
      importFromProfile('dsh-memoryos'),
      importFromProfile('dsh-memoryos/usage'),
      importFromProfile('dsh-memoryos/resume'),
    ])
  )
  assert.equal('default' in memoryTools, false)
  assert.equal('default' in memoryUsage, false)
  assert.equal(memoryResume.name, 'headless-runner')
  const installedEntry = requireFromProfile.resolve('dsh-memoryos')
  const bundlePatchPath = join(dirname(installedEntry), 'cordis.patch.yml')

  const root = await mkdtemp(join(tmpdir(), 'dsh-memoryos-loader-'))
  const configPath = join(root, 'cordis.yml')
  await writeFile(configPath, [
    "- id: system-prompt",
    "  name: '@deepseek-ai/dsh-system-prompt'",
    "- id: tools-runtime",
    "  name: '@deepseek-ai/dsh-tools'",
    "- id: headless-runner",
    "  name: '@deepseek-ai/dsh-headless'",
    '',
  ].join('\n'), 'utf8')

  const modules = new Map([
    ['@deepseek-ai/dsh-system-prompt', systemPromptModule.default],
    ['@deepseek-ai/dsh-tools', toolsModule.default],
    ['dsh-memoryos', memoryTools],
    ['dsh-memoryos/usage', memoryUsage],
    ['@deepseek-ai/dsh-headless', { name: 'headless-runner', apply() {} }],
    ['dsh-memoryos/resume', { name: 'headless-runner', apply() {} }],
  ])
  const boot = async () => {
    const context = new Context()
    context.baseUrl = pathToFileURL(root).href + '/'
    await context.plugin(loaderModule.default)
    context.loader.builtins.include = includeModule.default
    context.loader.internal = {
      version: 'v2',
      async import(specifier) {
        if (!modules.has(specifier)) throw new Error(`unexpected Loader import: ${specifier}`)
        return modules.get(specifier)
      },
    }
    await context.loader.create({
      name: 'cordis:include',
      config: {
        path: pathToFileURL(configPath).href,
        patches: appBootModule.loadOverlayPatches('dsh-memoryos-test', bundlePatchPath),
      },
    })
    await context.loader.await()
    return context
  }

  const previousEnabled = process.env.MEMORYOS_ENABLED
  const previousCondition = process.env.MEMORYOS_CONDITION
  const previousToolProfile = process.env.MEMORYOS_TOOL_PROFILE
  const previousRepository = process.env.MEMORYOS_REPOSITORY
  const previousBaseUrl = process.env.MEMORYOS_BASE_URL
  const previousAuthToken = process.env.MEMORYOS_AUTH_TOKEN
  const previousStateFile = process.env.MEMORYOS_STATE_FILE
  const previousResumeSessionId = process.env.MEMORYOS_RESUME_SESSION_ID
  let baseline
  let strictBaseline
  let treatment
  let writer
  let resumed
  let healthServer
  try {
    healthServer = createServer((request, response) => {
      if (request.url === '/api/health') {
        response.writeHead(200, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ ok: true, version: '2.3.0', database: 'ok' }))
        return
      }
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ok: false, error: { code: 'NOT_FOUND' } }))
    })
    await new Promise((resolve, reject) => {
      healthServer.once('error', reject)
      healthServer.listen(0, '127.0.0.1', resolve)
    })
    const healthAddress = healthServer.address()
    assert.ok(healthAddress && typeof healthAddress === 'object')
    process.env.MEMORYOS_BASE_URL = `http://127.0.0.1:${healthAddress.port}`
    process.env.MEMORYOS_AUTH_TOKEN = 'loader-test-local-token'
    process.env.MEMORYOS_STATE_FILE = join(root, 'memoryos-control-state.json')
    delete process.env.MEMORYOS_RESUME_SESSION_ID
    process.env.MEMORYOS_ENABLED = '0'
    process.env.MEMORYOS_CONDITION = 'msc_progressive'
    process.env.MEMORYOS_TOOL_PROFILE = 'read-only'
    baseline = await boot()
    const baselineEntries = [...baseline.loader.entries()]
    const baselineTools = baselineEntries.find(entry => entry.options.id === 'memoryos-tools')
    const baselineUsage = baselineEntries.find(entry => entry.options.id === 'memoryos-usage')
    assert.ok(baselineTools?.fiber)
    assert.ok(baselineUsage?.fiber)
    assert.ok(baseline.tools.get('memoryos_control'))
    assert.equal(baseline.tools.get('memory_context'), undefined)
    assert.equal(baseline.tools.get('memory_explain'), undefined)
    const baselineRunner = baselineEntries.find(entry => entry.options.id === 'headless-runner')
    const baselineResume = baselineEntries.find(entry => entry.options.id === 'memoryos-resume-runner')
    assert.ok(baselineRunner?.fiber)
    assert.equal(baselineResume?.fiber, undefined)
    await baseline.fiber.dispose()
    baseline = undefined

    process.env.MEMORYOS_ENABLED = '0'
    process.env.MEMORYOS_CONDITION = 'no_memory'
    strictBaseline = await boot()
    const strictEntries = [...strictBaseline.loader.entries()]
    const strictTools = strictEntries.find(entry => entry.options.id === 'memoryos-tools')
    const strictUsage = strictEntries.find(entry => entry.options.id === 'memoryos-usage')
    assert.equal(strictTools?.fiber, undefined)
    assert.ok(strictUsage?.fiber)
    assert.equal(strictBaseline.tools.get('memoryos_control'), undefined)
    assert.equal(strictBaseline.tools.get('memory_context'), undefined)
    await strictBaseline.fiber.dispose()
    strictBaseline = undefined

    process.env.MEMORYOS_ENABLED = '1'
    process.env.MEMORYOS_CONDITION = 'msc_progressive'
    process.env.MEMORYOS_TOOL_PROFILE = 'read-only'
    treatment = await boot()
    const entries = [...treatment.loader.entries()]
    const toolsEntry = entries.find(entry => entry.options.id === 'memoryos-tools')
    const usageEntry = entries.find(entry => entry.options.id === 'memoryos-usage')
    assert.ok(toolsEntry?.fiber)
    assert.ok(usageEntry?.fiber)
    assert.deepEqual(
      treatment.tools.schemas().map(schema => schema.name).sort(),
      ['memory_context', 'memory_explain', 'memoryos_control'],
    )

    const disableResult = await treatment.tools.execute({
      signal: new AbortController().signal,
      callId: 'memoryos-loader-disable',
      name: 'memoryos_control',
      arguments: { action: 'disable' },
    })
    assert.equal(disableResult.isError, false, JSON.stringify(disableResult))
    assert.deepEqual(
      treatment.tools.schemas().map(schema => schema.name),
      ['memoryos_control'],
    )
    const enableResult = await treatment.tools.execute({
      signal: new AbortController().signal,
      callId: 'memoryos-loader-enable',
      name: 'memoryos_control',
      arguments: { action: 'enable' },
    })
    assert.equal(enableResult.isError, false, JSON.stringify(enableResult))
    assert.deepEqual(
      treatment.tools.schemas().map(schema => schema.name).sort(),
      ['memory_context', 'memory_explain', 'memoryos_control'],
    )

    await toolsEntry.update({ disabled: true })
    await treatment.loader.await()
    assert.equal(treatment.tools.get('memory_context'), undefined)
    assert.equal(treatment.tools.get('memory_explain'), undefined)
    assert.equal(treatment.tools.get('memoryos_control'), undefined)
    assert.ok(usageEntry.fiber, 'usage collector must remain mounted for the baseline')

    await toolsEntry.update({ disabled: false })
    await treatment.loader.await()
    assert.ok(treatment.tools.get('memory_context'))
    assert.ok(treatment.tools.get('memory_explain'))
    assert.ok(treatment.tools.get('memoryos_control'))

    await treatment.fiber.dispose()
    treatment = undefined
    process.env.MEMORYOS_ENABLED = '1'
    process.env.MEMORYOS_CONDITION = 'msc_context_only'
    process.env.MEMORYOS_TOOL_PROFILE = 'cross-session-write'
    process.env.MEMORYOS_REPOSITORY = 'fixture://loader-write-profile'
    writer = await boot()
    const writerSchemas = writer.tools.schemas()
    assert.deepEqual(
      writerSchemas.map(schema => schema.name).sort(),
      ['memory_confirm', 'memory_context', 'memory_propose', 'memoryos_control'],
    )
    const proposeSchema = writerSchemas.find(schema => schema.name === 'memory_propose')
    assert.ok(proposeSchema)
    assert.match(proposeSchema.description, /exactly one independently updateable fact/u)
    assert.deepEqual(
      [...proposeSchema.parameters.required].sort(),
      ['category', 'content', 'key', 'source_excerpt', 'title'],
    )
    assert.match(
      proposeSchema.parameters.properties.key.description,
      /Stable dot-separated semantic key/u,
    )
    const confirmSchema = writerSchemas.find(schema => schema.name === 'memory_confirm')
    assert.ok(confirmSchema)
    assert.deepEqual(
      confirmSchema.parameters.properties.strategy.enum,
      ['supersede', 'keep_both', 'reject'],
    )
    assert.match(confirmSchema.description, /same candidate/u)
    await writer.fiber.dispose()
    writer = undefined

    process.env.MEMORYOS_ENABLED = '0'
    process.env.MEMORYOS_CONDITION = 'msc_progressive'
    process.env.MEMORYOS_TOOL_PROFILE = 'read-only'
    process.env.MEMORYOS_RESUME_SESSION_ID = 'session-00000000-0000-4000-8000-000000000000'
    resumed = await boot()
    const resumedEntries = [...resumed.loader.entries()]
    const originalRunner = resumedEntries.find(entry => entry.options.id === 'headless-runner')
    const resumeRunner = resumedEntries.find(entry => entry.options.id === 'memoryos-resume-runner')
    assert.equal(originalRunner?.fiber, undefined)
    assert.ok(resumeRunner?.fiber)
  } finally {
    await baseline?.fiber.dispose()
    await strictBaseline?.fiber.dispose()
    await treatment?.fiber.dispose()
    await writer?.fiber.dispose()
    await resumed?.fiber.dispose()
    restoreEnvironment('MEMORYOS_ENABLED', previousEnabled)
    restoreEnvironment('MEMORYOS_CONDITION', previousCondition)
    restoreEnvironment('MEMORYOS_TOOL_PROFILE', previousToolProfile)
    restoreEnvironment('MEMORYOS_REPOSITORY', previousRepository)
    restoreEnvironment('MEMORYOS_BASE_URL', previousBaseUrl)
    restoreEnvironment('MEMORYOS_AUTH_TOKEN', previousAuthToken)
    restoreEnvironment('MEMORYOS_STATE_FILE', previousStateFile)
    restoreEnvironment('MEMORYOS_RESUME_SESSION_ID', previousResumeSessionId)
    if (healthServer) {
      await new Promise((resolve, reject) => {
        healthServer.close(error => error ? reject(error) : resolve())
      })
    }
    await rm(root, { recursive: true, force: true })
  }
})

function restoreEnvironment(name, value) {
  if (value === undefined) delete process.env[name]
  else process.env[name] = value
}

async function missingReason(path) {
  try {
    await access(path)
    return ''
  } catch {
    return `DeepSeek Harness profile is missing: ${dirname(path)}`
  }
}
