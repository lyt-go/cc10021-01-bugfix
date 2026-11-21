<template>
  <section class="recheck-panel" data-testid="recheck-panel">
    <header class="panel-head">
      <div>
        <h3>日报复查面板</h3>
        <p class="page-desc">
          逐表区分“未执行”和“执行失败”。有未执行规则的表不能用本轮结果确认，需等迟到分区补算。
        </p>
      </div>
      <label class="panel-select">
        <span>质量报告</span>
        <select v-model.number="reportId" data-testid="recheck-report-select" @change="reload">
          <option v-for="report in reports" :key="report.id" :value="Number(report.id)">
            {{ report.报告编号 }} · {{ report.报告周期 }}
          </option>        </select>
      </label>
    </header>

    <div class="stat-row" v-if="panel">
      <article class="stat-card">
        <span class="stat-label">已执行</span>
        <strong class="stat-value" data-testid="recheck-executed">{{ panel.统计.已执行数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">通过</span>
        <strong class="stat-value">{{ panel.统计.通过数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">执行失败</span>
        <strong class="stat-value" data-testid="recheck-failed">{{ panel.统计.失败数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未执行</span>
        <strong class="stat-value" data-testid="recheck-not-run">{{ panel.统计.未执行数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">通过率</span>
        <strong class="stat-value">{{ panel.统计.已执行数 ? panel.统计.通过率 : '—' }}</strong>
      </article>
    </div>

    <table v-if="panel" class="data-table" data-testid="recheck-table">
      <thead>
        <tr>
          <th>目标表名</th>
          <th>适用规则</th>
          <th>通过</th>
          <th>执行失败</th>
          <th>未执行</th>
          <th>通过率</th>
          <th>本轮结果</th>
          <th>复查原因</th>
          <th>确认状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in panel.表"
          :key="item.目标表名"
          :data-testid="`recheck-row-${item.目标表名}`"
          :data-reason="item.复查原因"
        >
          <td>{{ item.目标表名 }}</td>
          <td>{{ item.适用规则数 }}</td>
          <td>{{ item.通过数 }}</td>
          <td>{{ item.失败数 }}</td>
          <td>{{ item.未执行数 }}</td>
          <td>{{ item.已执行数 ? item.通过率 : '—' }}</td>
          <td :class="item.本轮结果可用 ? 'can-confirm' : 'cannot-confirm'">
            {{ item.本轮结果可用 ? '可用' : '不可用' }}
          </td>
          <td>{{ item.复查原因 || '—' }}</td>
          <td>{{ item.确认状态 }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`recheck-confirm-${item.目标表名}`"
              @click="confirm(item.目标表名)"
            >
              记录确认
            </button>
          </td>
        </tr>
        <tr v-if="!panel.表.length">
          <td colspan="10" class="empty-state">当前报告没有可复查的表统计</td>
        </tr>
      </tbody>
    </table>

    <p v-if="panel" class="status-legend" data-testid="recheck-summary">
      待复查：{{ panel.待复查清单.join('、') || '无' }} ·
      可确认：{{ panel.可确认表.join('、') || '无' }} ·
      未出结果：{{ panel.未出结果表.join('、') || '无' }}
    </p>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { confirmRecheck, listEntries, loadRecheckPanel } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import type { RecheckPanel } from '@/domain/inspection'

const props = defineProps<{ moduleKey: string }>()

const reports = ref<EntryRow[]>([])
const reportId = ref<number | null>(null)
const panel = ref<RecheckPanel | null>(null)
const errorMessage = ref('')

async function loadReports() {
  const payload = await listEntries(props.moduleKey)
  // 复查面板针对巡检轮次生成的报告：优先值班人员生成的质量日报，
  // 没有日报时退回巡检报告，两者统计口径一致。
  const rounds = payload.items.filter(
    (row) => row.关联巡检轮次 || String(row.报告周期 ?? '').startsWith('巡检-'),
  )
  const daily = rounds.filter((row) => String(row.报告类型 ?? '') === '质量日报')
  const candidates = (daily.length ? daily : rounds).slice()
  candidates.sort((a, b) => Number(b.id) - Number(a.id))
  reports.value = candidates.length ? candidates : payload.items
  const first = reports.value[0]
  reportId.value = first ? Number(first.id) : null
}

async function reload() {
  errorMessage.value = ''
  panel.value = null
  try {
    if (!reports.value.length) await loadReports()
    if (reportId.value === null) return
    panel.value = await loadRecheckPanel(reportId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '日报复查面板读取失败'
  }
}

async function confirm(tableName: string) {
  if (reportId.value === null) return
  errorMessage.value = ''
  try {
    await confirmRecheck(reportId.value, tableName, '')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复查确认写入失败'
  }
}

onMounted(reload)
</script>
