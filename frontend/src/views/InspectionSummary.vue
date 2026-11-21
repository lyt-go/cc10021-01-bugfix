<template>
  <section class="inspection-panel" data-testid="inspection-panel">
    <header class="panel-head">
      <div>
        <h3>巡检统计</h3>
        <p class="page-desc">
          通过率只按已执行规则计算；范围外规则记“跳过”不进分母，尚未执行的规则单独统计。
        </p>
      </div>
      <button class="btn ghost" data-testid="inspection-refresh" type="button" @click="reload">
        刷新
      </button>
    </header>

    <div class="stat-row" v-if="stats">
      <article class="stat-card">
        <span class="stat-label">适用规则数</span>
        <strong class="stat-value" data-testid="stat-total">{{ stats.总规则数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">通过</span>
        <strong class="stat-value" data-testid="stat-passed">{{ stats.通过数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">执行失败</span>
        <strong class="stat-value" data-testid="stat-failed">{{ stats.失败数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未执行</span>
        <strong class="stat-value" data-testid="stat-not-run">{{ stats.未执行数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">范围外跳过</span>
        <strong class="stat-value" data-testid="stat-skipped">{{ stats.跳过数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">通过率</span>
        <strong class="stat-value" data-testid="stat-pass-rate">{{ passRate }}</strong>
      </article>
    </div>

    <p v-if="stats" class="status-legend">
      巡检轮次：{{ stats.任务编号 || '—' }} · 完成状态：{{ stats.完成状态 || '—' }}
      <span v-for="item in statusSummary" :key="item.status" class="legend-item" :style="{ color: item.color }">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <table v-if="stats" class="data-table" data-testid="inspection-table">
      <thead>
        <tr>
          <th>规则编号</th>
          <th>规则类型</th>
          <th>目标表名</th>
          <th>状态</th>
          <th>说明</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="detail in stats.规则明细" :key="detail.规则编号" :data-status="detail.状态">
          <td>{{ detail.规则编号 }}</td>
          <td>{{ detail.规则类型 ?? '—' }}</td>
          <td>{{ detail.目标表名 ?? '—' }}</td>
          <td :style="{ color: STATUS_COLORS[detail.状态] }">{{ detail.状态 }}</td>
          <td>{{ detail.失败原因 ?? detail.跳过原因 ?? detail.原因 ?? '—' }}</td>
        </tr>
        <tr v-if="!stats.规则明细.length">
          <td colspan="5" class="empty-state">本模块暂无巡检执行明细</td>
        </tr>
      </tbody>
    </table>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { loadInspectionStats } from '@/api/backend-service'
import {
  STATUS_COLORS,
  displayPassRate,
  type InspectionStats,
  type RuleStatus,
} from '@/domain/inspection'

const stats = ref<InspectionStats | null>(null)
const errorMessage = ref('')

const passRate = computed(() => (stats.value ? displayPassRate(stats.value) : '—'))
const statusSummary = computed(() => {
  if (!stats.value) return []
  const counts: Record<RuleStatus, number> = { 通过: 0, 失败: 0, 跳过: 0, 未执行: 0 }
  for (const detail of stats.value.规则明细) {
    counts[detail.状态] = (counts[detail.状态] ?? 0) + 1
  }
  return (Object.keys(counts) as RuleStatus[]).map((status) => ({
    status,
    count: counts[status],
    color: STATUS_COLORS[status],
  }))
})

async function reload() {
  errorMessage.value = ''
  try {
    stats.value = await loadInspectionStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '巡检统计读取失败'
  }
}

onMounted(reload)
</script>
