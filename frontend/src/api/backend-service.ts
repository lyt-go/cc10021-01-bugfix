import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'
import type { InspectionStats, RecheckPanel, RuleDetail } from '@/domain/inspection'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function loadInspectionStats(taskId?: string): Promise<InspectionStats> {
  const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''
  const response = await request(`/api/dq_check_run/stats${query}`)
  if (!response.ok) throw new Error('巡检统计读取失败')
  return await response.json()
}

export async function submitLateResults(
  taskId: string,
  details: RuleDetail[],
): Promise<InspectionStats> {
  const response = await request(`/api/dq_check_run/${encodeURIComponent(taskId)}/late-results`, {
    method: 'POST',
    body: JSON.stringify({ values: { 规则明细: details } }),
  })
  if (!response.ok) throw new Error('迟到分区补算失败')
  return await response.json()
}

export async function loadRecheckPanel(reportId: number): Promise<RecheckPanel> {
  const response = await request(`/api/dq_report/${reportId}/recheck`)
  if (!response.ok) throw new Error('日报复查面板读取失败')
  return await response.json()
}

export async function confirmRecheck(
  reportId: number,
  tableName: string,
  conclusion: string,
  note = '',
): Promise<void> {
  const response = await request(`/api/dq_report/${reportId}/recheck/confirm`, {
    method: 'POST',
    body: JSON.stringify({
      values: { 目标表名: tableName, 确认结论: conclusion, 复查备注: note },
    }),
  })
  if (!response.ok) throw new Error('复查确认写入失败')
}
