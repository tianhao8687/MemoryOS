import AxeBuilder from '@axe-core/playwright'
import { expect, type Page, test } from '@playwright/test'

async function navigate(page: Page, label: string) {
  const link = page.getByRole('link', { name: label, exact: true })
  if ((page.viewportSize()?.width ?? 1024) <= 900) await page.getByRole('button', { name: 'Open navigation' }).click()
  await link.click()
}

test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await page.waitForLoadState('networkidle')
  ;(page as typeof page & { __memoryosErrors?: string[] }).__memoryosErrors = errors
})

test.afterEach(({ page }) => {
  const errors = (page as typeof page & { __memoryosErrors?: string[] }).__memoryosErrors ?? []
  expect(errors, `browser console errors: ${errors.join('\n')}`).toEqual([])
})

test('overview renders project context without accessibility violations', async ({ page }, testInfo) => {
  await expect(page.getByRole('heading', { name: 'Project memory' })).toBeVisible()
  await expect(page.getByText('Backend framework: FastAPI')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('Conflict detected')).toBeVisible({ timeout: 15_000 })
  if (testInfo.project.name === 'desktop-chromium') {
    const results = await new AxeBuilder({ page }).analyze()
    expect(results.violations).toEqual([])
  }
})

test('mobile candidate drawer stays within the viewport', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile-chromium', 'Mobile layout assertion')
  await navigate(page, 'Candidates')
  await expect(page.getByRole('heading', { name: 'Candidates', exact: true })).toBeVisible()
  await expect(page.getByRole('complementary', { name: /Review candidate:/ })).toBeVisible()
  await expect(page.getByText('Candidate (not confirmed)')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Close candidate details' })).toBeVisible()
  const metrics = await page.evaluate(() => {
    const drawer = document.querySelector('.inspector')?.getBoundingClientRect()
    return {
      innerWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      bodyWidth: document.body.scrollWidth,
      drawerWidth: drawer?.width ?? 0,
    }
  })
  expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.innerWidth)
  expect(metrics.bodyWidth).toBeLessThanOrEqual(metrics.innerWidth)
  expect(metrics.drawerWidth).toBeLessThanOrEqual(metrics.innerWidth)
})

test('candidate confirm and explain workflow updates real server state', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === 'mobile-chromium', 'Mutating workflow runs once against the shared fixture')
  await navigate(page, 'Candidates')
  await expect(page.getByRole('heading', { name: 'Candidates' })).toBeVisible()
  await page.getByRole('button', { name: 'Admin endpoints require auth', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Admin endpoints require auth' })).toBeVisible()
  await expect(page.getByText('Why this memory?')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm', exact: true }).click()
  await expect(page.getByText('Admin endpoints require auth')).toHaveCount(0)
  await page.reload()
  await page.waitForLoadState('networkidle')
  await navigate(page, 'Memories')
  await page.getByRole('searchbox', { name: 'Search all memory' }).fill('Admin endpoints')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await expect(page.getByText('Admin endpoints require auth')).toBeVisible()
})

test('conflict resolution requires rationale and typed confirmation', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === 'mobile-chromium', 'Mutating workflow runs once against the shared fixture')
  await navigate(page, 'Conflicts')
  await expect(page.getByRole('heading', { name: 'Conflicts' })).toBeVisible()
  const code = await page.locator('.safe-confirm code').textContent()
  await page.getByLabel('Rationale (required)').fill('The existing FastAPI implementation is already verified.')
  await page.getByLabel('Confirmation code').fill(code ?? '')
  await page.getByRole('button', { name: 'Confirm resolution' }).click()
  await expect(page.getByRole('button', { name: 'Backend framework: Django', exact: true })).toHaveCount(0)
})

test('memory explain and logical forget remove an active result', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === 'mobile-chromium', 'Mutating workflow runs once against the shared fixture')
  await navigate(page, 'Memories')
  await page.getByRole('searchbox', { name: 'Search all memory' }).fill('Windows path case')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await page.getByRole('button', { name: 'Windows path case mismatch', exact: true }).click()
  await expect(page.getByText('Why this memory?')).toBeVisible()
  await page.getByRole('button', { name: 'Forget memory' }).click()
  await expect(page.getByText('Windows path case mismatch')).toHaveCount(0)
})

test('intelligence workbench exposes truth, retrieval traces, and benchmark provenance', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-chromium', 'Desktop intelligence workflow')
  await navigate(page, 'Current Truth')
  await expect(page.getByRole('heading', { name: 'Current Truth' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resolve truth' })).toBeVisible()

  await navigate(page, 'Retrieval Debugger')
  await page.getByLabel('Coding task').fill('Which confirmed architecture decision constrains the API?')
  await page.getByRole('button', { name: 'Run trace' }).click()
  await expect(page.getByRole('heading', { name: 'Deterministic Query Planner' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Compiled context' })).toBeVisible()

  await navigate(page, 'Benchmarks')
  await expect(page.getByRole('heading', { name: 'Benchmark Dashboard' })).toBeVisible()
  await expect(page.getByText('R Retrieval')).toBeVisible()
  await expect(page.getByText('Real-model Agent A/B: external blocker')).toBeVisible()
  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('reality hardening workbench exposes conflict audit and health governance', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-chromium', 'Desktop V2.1 workflow')
  await navigate(page, 'Possible Conflicts')
  await expect(page.getByRole('heading', { name: 'Possible Conflicts' })).toBeVisible()
  await expect(page.getByText('Abstention is safe')).toBeVisible()

  await navigate(page, 'Memory Health')
  await expect(page.getByRole('heading', { name: 'Memory Health' })).toBeVisible()
  await page.getByRole('button', { name: 'Evaluate health' }).click()
  await expect(page.getByRole('table')).toBeVisible()
  await expect(page.getByText('Only Cold or Archived memories can be distilled.')).toBeVisible()

  await navigate(page, 'Settings')
  await expect(page.getByRole('heading', { name: 'Vector index' })).toBeVisible()
  await expect(page.getByText('sqlite vec runtime')).toBeVisible()
  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('mobile intelligence navigation stays within the viewport', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile-chromium', 'Mobile intelligence layout assertion')
  await navigate(page, 'Benchmarks')
  await expect(page.getByRole('heading', { name: 'Benchmark Dashboard' })).toBeVisible()
  const metrics = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }))
  expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.innerWidth)
  expect(metrics.bodyWidth).toBeLessThanOrEqual(metrics.innerWidth)

  await navigate(page, 'Memory Health')
  await expect(page.getByRole('heading', { name: 'Memory Health' })).toBeVisible()
  const healthMetrics = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }))
  expect(healthMetrics.documentWidth).toBeLessThanOrEqual(healthMetrics.innerWidth)
  expect(healthMetrics.bodyWidth).toBeLessThanOrEqual(healthMetrics.innerWidth)
})
