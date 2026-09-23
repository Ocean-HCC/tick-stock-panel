import { useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CircleHelp, Loader2 } from 'lucide-react'
import { api, ApiError } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'

// hasCap: 日K批量能力当前是否可用 (路由矩阵判定, 生效源含插件/自定义源)
export function ExtendHistoryPanel({ hasCap, isRunning, earliestDate, onStart, assetType = 'stock', loading = false }: {
  hasCap: boolean
  isRunning: boolean
  earliestDate: string | null
  onStart: (jobId: string) => void
  assetType?: 'stock' | 'etf'
  loading?: boolean
}) {
  const qc = useQueryClient()
  const [value, setValue] = useState(6)
  const [unit, setUnit] = useState<'month' | 'year'>('month')
  const hasBatchCap = hasCap
  const [helpOpen, setHelpOpen] = useState(false)
  const helpId = useId()

  const extend = useMutation({
    mutationFn: () => assetType === 'etf'
      ? api.extendHistory(value, unit, { asset_type: 'etf', expected_earliest_date: earliestDate! })
      : api.extendHistory(value, unit),
    onSuccess: ({ job_id }) => {
      onStart(job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
    onError: error => {
      if (error instanceof ApiError && error.status === 409) qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })
  const disabled = !hasBatchCap || isRunning || loading || extend.isPending || !earliestDate

  const offsetDays = unit === 'month' ? value * 30 : value * 365
  const estimate = earliestDate
    ? (() => {
        const d = new Date(earliestDate)
        d.setUTCDate(d.getUTCDate() - offsetDays)
        return d.toISOString().slice(0, 10)
      })()
    : null

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      <div className="relative flex items-center gap-1 text-[10px] text-secondary">
        向前扩展历史数据
        {assetType === 'etf' && <>
          <button aria-label="ETF 复权说明" aria-describedby={helpOpen ? helpId : undefined}
            onFocus={() => setHelpOpen(true)} onClick={() => setHelpOpen(true)} onBlur={() => setHelpOpen(false)}
            onKeyDown={e => { if (e.key === 'Escape') setHelpOpen(false) }}
            className="p-1 text-muted hover:text-secondary focus-visible:outline-accent">
            <CircleHelp className="h-3 w-3" />
          </button>
          {helpOpen && <div id={helpId} role="tooltip" className="absolute left-0 top-full z-10 mt-1 max-w-full w-72 rounded-btn border border-border bg-surface p-2 shadow-lg">
            优先使用本地 ETF 因子；缺少因子时按原始价格计算，未确认复权，可能影响历史指标和回测。
          </div>}
        </>}
      </div>

      <div className="flex items-center gap-2">
        <div className="flex items-center">
          <button
            onClick={() => setValue(Math.max(1, value - 1))}
            disabled={disabled || value <= 1}
            className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >−</button>
          <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
            {value}
          </div>
          <button
            onClick={() => setValue(Math.min(unit === 'year' ? 10 : 36, value + 1))}
            disabled={disabled || value >= (unit === 'year' ? 10 : 36)}
            className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >+</button>
        </div>

        <div className="flex rounded-btn border border-border overflow-hidden">
          {(['month', 'year'] as const).map(u => (
            <button
              key={u}
              disabled={disabled}
              onClick={() => { setUnit(u); if (u === 'year' && value > 10) setValue(1); if (u === 'month' && value > 36) setValue(6) }}
              className={`px-2 py-0.5 text-[10px] font-medium transition-colors ${
                unit === u ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated'
              }`}
            >{u === 'month' ? '月' : '年'}</button>
          ))}
        </div>
      </div>

      {estimate && (
        <div className="text-[10px] text-muted">
          预计扩展至 <span className="font-mono text-secondary">{estimate}</span>
          {earliestDate && <span> (当前最早: <span className="font-mono text-secondary">{earliestDate}</span>)</span>}
        </div>
      )}

      <button
        onClick={() => extend.mutate()}
        disabled={disabled}
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
      >
        {extend.isPending ? (
          <>
            <Loader2 className="h-3 w-3 animate-spin" />
            请求中…
          </>
        ) : (
          <>获取数据</>
        )}
      </button>

      {!hasBatchCap && (
        <MissingCapChip capKey="kline.daily.batch" />
      )}
      {assetType === 'etf' && !loading && !earliestDate && <div className="text-[10px] text-muted">本地无独立 ETF 日 K，请先同步 ETF。</div>}
      {assetType === 'etf' && !hasBatchCap && <div className="text-[10px] text-muted">ETF 扩展沿用原同步日线来源，请检查数据源配置。</div>}
      {extend.isError && <div role="alert" className="text-xs text-danger">{extend.error.message}</div>}
    </div>
  )
}
