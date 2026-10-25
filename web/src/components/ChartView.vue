<script setup lang="ts">
import { computed } from 'vue'
import BarChart from './BarChart.vue'
import { useSessionStore } from '../stores/session'

const store = useSessionStore()

const props = defineProps<{
  columns: string[]
  rows: any[][]
  format: (col: string, value: any) => string
  compare?: boolean
  metricKey?: string
  metricLabel?: string
  dimLabel?: string
  metricType?: string
  unit?: string
}>()

const key = computed(() => props.metricKey || 'value')
const valueIndex = computed(() => props.columns.indexOf(key.value))
const nameIndex = computed(() => 0)

const numeric = computed(() =>
  props.rows
    .map((r) => ({ name: String(r[nameIndex.value]), value: Number(r[valueIndex.value] ?? 0) }))
    .filter((x) => Number.isFinite(x.value)),
)

const fallbackType = computed(() => {
  if (props.compare) return 'bar'
  if (props.columns.includes('month') || props.dimLabel === '月份') return 'line'
  if (
    numeric.value.length > 1 &&
    numeric.value.length <= 6 &&
    props.metricType !== 'ratio' &&
    props.unit !== '%'
  ) return 'pie'
  return 'bar'
})

// The model proposes a chart; the code only checks it against the actual
// result shape and falls back to the deterministic rule when it is not usable.
const suggested = computed(() => {
  const want = store.activePlan?.visualization?.type
  if (!want) return null
  if (want === 'grouped_bar' || want === 'kpi') return 'bar'
  if (want === 'line' || want === 'bar' || want === 'pie') {
    if (want === 'pie') {
      const pieOk =
        !props.compare &&
        props.metricType !== 'ratio' &&
        props.unit !== '%' &&
        numeric.value.length > 1 &&
        numeric.value.length <= 8
      if (!pieOk) return null
    }
    return want
  }
  return null
})

const autoType = computed(() => suggested.value || fallbackType.value)
const autoSource = computed(() =>
  suggested.value ? '模型建议' : '规则兜底',
)

const mode = computed(() => (store.chartType === 'auto' ? autoType.value : store.chartType))

const W = 640
const H = 240
const PAD = 34

const line = computed(() => {
  const pts = numeric.value.slice(0, 30)
  if (!pts.length) return []
  const max = Math.max(...pts.map((p) => p.value), 1)
  const min = Math.min(...pts.map((p) => p.value), 0)
  const span = max - min || 1
  return pts.map((p, i) => ({
    ...p,
    x: PAD + (i * (W - PAD * 2)) / Math.max(1, pts.length - 1),
    y: H - PAD - ((p.value - min) / span) * (H - PAD * 2),
  }))
})
const linePoints = computed(() => line.value.map((p) => `${p.x},${p.y}`).join(' '))
const lineMax = computed(() => Math.max(...numeric.value.map((p) => p.value), 1))

const pie = computed(() => {
  const pts = numeric.value.filter((p) => p.value > 0).slice(0, 8)
  const total = pts.reduce((s, p) => s + p.value, 0)
  if (!total) return []
  let angle = -Math.PI / 2
  const R = 80
  const CX = 100
  const CY = 100
  return pts.map((p, i) => {
    const frac = p.value / total
    const next = angle + frac * Math.PI * 2
    const x1 = CX + R * Math.cos(angle)
    const y1 = CY + R * Math.sin(angle)
    const x2 = CX + R * Math.cos(next)
    const y2 = CY + R * Math.sin(next)
    const large = frac > 0.5 ? 1 : 0
    const d = `M ${CX} ${CY} L ${x1} ${y1} A ${R} ${R} 0 ${large} 1 ${x2} ${y2} Z`
    angle = next
    return { ...p, d, frac, idx: i }
  })
})
const pieColors = ['#175E58', '#C4552F', '#9C6C18', '#3F6B52', '#817A6D', '#B23B2B', '#5B7C99', '#8C6A9B']
const pieTotal = computed(() => pie.value.reduce((s, p) => s + p.value, 0))

function setMode(m: 'auto' | 'line' | 'bar' | 'pie') {
  store.setChartType(m)
}
</script>

<template>
  <div class="chartview">
    <div class="chartbar">
      <span class="lab">图表类型</span>
      <div class="chartmodes">
        <button :class="{ on: store.chartType === 'auto' }" @click="setMode('auto')">自动</button>
        <button :class="{ on: store.chartType === 'line' }" @click="setMode('line')">折线</button>
        <button :class="{ on: store.chartType === 'bar' }" @click="setMode('bar')">柱状</button>
        <button :class="{ on: store.chartType === 'pie' }" @click="setMode('pie')">饼图</button>
      </div>
      <span class="lab" :title="store.activePlan?.visualization?.reason || ''">
        自动选择：{{ autoType }}（{{ autoSource }}）
      </span>
    </div>

    <BarChart
      v-if="mode === 'bar'"
      :columns="columns"
      :rows="rows"
      :format="format"
      :compare="compare"
      :metric-key="metricKey"
      :metric-label="metricLabel"
      :dim-label="dimLabel"
    />

    <div v-else-if="mode === 'line'" class="linewrap">
      <svg :viewBox="`0 0 ${W} ${H}`" class="linesvg">
        <line :x1="PAD" :y1="H - PAD" :x2="W - PAD" :y2="H - PAD" class="axis" />
        <line :x1="PAD" :y1="PAD" :x2="PAD" :y2="H - PAD" class="axis" />
        <polyline :points="linePoints" class="linepath" />
        <circle v-for="p in line" :key="p.name" :cx="p.x" :cy="p.y" r="3" class="dot" />
        <text x="4" y="16" class="ymax">{{ format(key, lineMax) }}</text>
      </svg>
      <div class="xlabels">
        <span v-for="(p, i) in line" :key="p.name">{{ i % Math.max(1, Math.ceil(line.length / 8)) === 0 ? p.name : '' }}</span>
      </div>
    </div>

    <div v-else class="piewrap">
      <svg viewBox="0 0 200 200" class="piesvg">
        <path v-for="p in pie" :key="p.name" :d="p.d" :fill="pieColors[p.idx % pieColors.length]" />
      </svg>
      <div class="pielegend">
        <div v-for="p in pie" :key="p.name" class="pierow">
          <i :style="{ background: pieColors[p.idx % pieColors.length] }"></i>
          <span class="piename">{{ p.name }}</span>
          <span class="pieval">{{ format(key, p.value) }}</span>
          <span class="piepct">{{ (p.frac * 100).toFixed(1) }}%</span>
        </div>
      </div>
    </div>
  </div>
</template>
