<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  columns: string[]
  rows: any[][]
  format: (col: string, value: any) => string
  compare?: boolean
  metricKey?: string
  metricLabel?: string
  dimLabel?: string
}>()

const key = computed(() => props.metricKey || 'value')

const items = computed(() => {
  const ci = (name: string) => props.columns.indexOf(name)
  const primaryIdx = ci(key.value)
  const cur = ci('current')
  const base = ci('baseline')
  const values = props.rows.map((r) => {
    const primary = Math.abs(Number(r[primaryIdx] ?? (props.compare ? r[cur] : 0) ?? 0))
    const secondary = props.compare && base >= 0 ? Math.abs(Number(r[base] || 0)) : 0
    return { name: String(r[0]), primary, secondary, row: r }
  })
  const max = Math.max(1, ...values.map((x) => Math.max(x.primary, x.secondary)))
  return values.slice(0, 20).map((x) => ({
    ...x,
    pct: (x.primary / max) * 100,
    pct2: (x.secondary / max) * 100,
  }))
})
</script>

<template>
  <div class="chart">
    <div class="legend">
      <span><i class="sw a"></i>{{ compare ? '本期' : (metricLabel || dimLabel || '数值') }}</span>
      <span v-if="compare"><i class="sw b"></i>基期</span>
    </div>
    <div v-for="item in items" :key="item.name" class="crow">
      <div class="cname" :title="item.name">{{ item.name }}</div>
      <div class="track">
        <div class="bar" :style="{ width: item.pct + '%' }"></div>
        <div v-if="compare" class="bar y24" :style="{ width: item.pct2 + '%' }"></div>
      </div>
      <div class="cval">
        <span>{{ format(compare ? 'current' : key, item.row[columns.indexOf(compare ? 'current' : key)]) }}</span>
        <span v-if="compare">{{ format('baseline', item.row[columns.indexOf('baseline')]) }}</span>
      </div>
    </div>
  </div>
</template>
