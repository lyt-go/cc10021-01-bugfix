<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "质量规则定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "巡检任务管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量事件管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "元数据采集器", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据表目录", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段画像分析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "血缘图谱", "created": 0, "pending": 0, "abnormal": 0}, {"name": "指标波动监控", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量评分卡", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据分类标签", "created": 0, "pending": 0, "abnormal": 0}, {"name": "规则模板库", "created": 0, "pending": 0, "abnormal": 0}, {"name": "异常模式识别", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量报告生成", "created": 0, "pending": 0, "abnormal": 0}, {"name": "元数据变更对比", "created": 0, "pending": 0, "abnormal": 0}, {"name": "告警通知通道", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量基线管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "主数据实体注册", "created": 0, "pending": 0, "abnormal": 0}, {"name": "质量趋势看板", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
