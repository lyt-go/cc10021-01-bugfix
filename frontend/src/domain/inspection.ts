/** 巡检统计与日报复查的公共类型和展示口径。 */

export type RuleStatus = '通过' | '失败' | '跳过' | '未执行'

export type RuleDetail = {
  规则编号: string
  规则名称?: string
  规则类型?: string
  目标表名?: string
  状态: RuleStatus
  执行状态?: string
  失败原因?: string
  跳过原因?: string
  原因?: string
}

export type TableInspectionStat = {
  目标表名: string
  适用规则数: number
  通过数: number
  失败数: number
  未执行数: number
  跳过数: number
  已执行数: number
  通过率: string
  通过率数值: number
  结果状态: '已出结果' | '未出结果'
  可确认: boolean
  复查状态: '待复查' | '无需复查'
}

export type InspectionStats = {
  任务编号: string
  轮次标识?: string
  关联规则集?: string
  数据范围?: string
  总规则数: number
  规则总数?: number
  适用规则数?: number
  范围外规则数?: number
  通过数: number
  失败数: number
  执行失败数?: number
  跳过数: number
  未执行数: number
  已执行数: number
  实际执行数?: number
  通过率: string
  巡检通过率?: string
  完成状态?: string
  规则明细: RuleDetail[]
  表统计: Record<string, TableInspectionStat>
  待复查表: string[]
  可确认表: string[]
  未出结果表: string[]
}

export type RecheckTableItem = TableInspectionStat & {
  复查原因: '' | '未执行' | '执行失败'
  本轮结果可用: boolean
  确认状态: string
  确认人: string
  确认时间: string
  复查备注: string
}

export type RecheckPanel = {
  报告编号: string
  关联巡检轮次: string
  统计: {
    总规则数: number
    通过数: number
    失败数: number
    未执行数: number
    跳过数: number
    已执行数: number
    通过率: string
  }
  表: RecheckTableItem[]
  待复查清单: string[]
  可确认表: string[]
  未出结果表: string[]
}

export const STATUS_COLORS: Record<RuleStatus, string> = {
  通过: '#22c55e',
  失败: '#ef4444',
  跳过: '#94a3b8',
  未执行: '#f59e0b',
}

/** 通过率只按已执行规则计算；没有已执行规则时不展示成 0% 的假象。 */
export function displayPassRate(stats: Pick<InspectionStats, '通过率' | '已执行数'>): string {
  return stats.已执行数 > 0 ? stats.通过率 : '—'
}

/** 本轮能确认的表：没有未执行规则且没有执行失败。 */
export function isTableConfirmable(item: Pick<TableInspectionStat, '未执行数'>): boolean {
  return item.未执行数 === 0
}
