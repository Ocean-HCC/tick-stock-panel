// @vitest-environment jsdom
import { act, type ComponentProps } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError, type PipelineJob } from '@/lib/api'
import { ExtendHistoryPanel } from './ExtendHistoryPanel'
import { ActiveJobCard } from './ActiveJobCard'

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})
async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
async function render(props: Partial<ComponentProps<typeof ExtendHistoryPanel>> = {}) {
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}>
    <ExtendHistoryPanel hasCap isRunning={false} earliestDate="2025-09-22" onStart={() => {}} assetType="etf" {...props} />
  </QueryClientProvider></MemoryRouter>))
}
function button(text: string) {
  return [...host.querySelectorAll('button')].find(b => b.textContent === text)!
}
it('keeps ETF form compact, exposes risk by keyboard and submits without an adjustment capability', async () => {
  const call = vi.spyOn(api, 'extendHistory').mockResolvedValue({ status: 'started', job_id: 'etf-job' })
  const started = vi.fn()
  await render({ onStart: started })
  expect(host.textContent).not.toContain('原始价格')
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="ETF 复权说明"]')!.focus())
  expect(host.textContent).toContain('原始价格')
  await act(async () => button('获取数据').click())
  await settle()
  expect(call).toHaveBeenCalledWith(6, 'month', { asset_type: 'etf', expected_earliest_date: '2025-09-22' })
  expect(started).toHaveBeenCalledWith('etf-job')
})
it.each([{ loading: true }, { isRunning: true }, { hasCap: false }, { earliestDate: null }])('disables all range controls: %j', async props => {
  await render(props)
  for (const text of ['−', '+', '月', '年', '获取数据']) expect(button(text).disabled).toBe(true)
})
it('shows conflict without retrying or clearing input', async () => {
  const call = vi.spyOn(api, 'extendHistory').mockRejectedValue(new ApiError('范围已变化', 409))
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await render()
  await act(async () => button('+').click())
  await act(async () => button('获取数据').click())
  await settle()
  expect(call).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('范围已变化')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['data-status'] })
  expect(call).toHaveBeenCalledWith(7, 'month', expect.anything())
})
it('preserves the stock two-argument API call', async () => {
  const call = vi.spyOn(api, 'extendHistory').mockResolvedValue({ status: 'started', job_id: 'stock' })
  await render({ assetType: 'stock' })
  await act(async () => button('获取数据').click())
  await settle()
  expect(call).toHaveBeenCalledWith(6, 'month')
})
it('uses one ETF result line and real partial status, without rendering internal reports', async () => {
  const job = { id: 'etf', status: 'succeeded', stage: 'extend_etf_history', progress: 100, stage_pct: 100,
    log: [], started_at: null, finished_at: null, duration_s: 3, error: null,
    result: { asset_type: 'etf', outcome: 'partial', universe_size: 2, daily_rows_written: 42,
      enriched_rows_written: 530, earliest_after: '2025-08-25', warnings: ['secret-warning'],
      failures: [{ symbol: 'secret-symbol', reason: 'internal' }] },
  } as PipelineJob
  await act(async () => root.render(<ActiveJobCard job={job} />))
  expect(host.textContent).toContain('部分完成')
  expect(host.textContent).toContain('日 K 写入 42 行 · 指标写入 530 行 · 最早 2025-08-25')
  expect(host.textContent).not.toMatch(/secret|标的池|除权因子|分钟K/)
})
