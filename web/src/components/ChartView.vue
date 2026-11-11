<script setup lang="ts">
import { computed } from 'vue'
import BarChart from './BarChart.vue'
import ChartTypeIcon from './ChartTypeIcon.vue'
import { useSessionStore } from '../stores/session'
import type { ChartSpec, ChartType } from '../types'

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
  chart?: ChartSpec | null
}>()

const key = computed(() => props.metricKey || 'value')
const valueIndex = computed(() => props.columns.indexOf(key.value))
const nameIndex = computed(() => 0)
const rows = computed(() => props.rows || [])

const numeric = computed(() =>
  rows.value
    .map((r) => ({ name: String(r[nameIndex.value]), value: Number(r[valueIndex.value] ?? 0) }))
    .filter((x) => Number.isFinite(x.value)),
)
const numericColumns = computed(() =>
  props.columns.filter((col) => {
    if (['value', 'current', 'baseline', 'delta', 'growth_rate', 'numerator', 'denominator'].includes(col)) return true
    return rows.value.some((row) => Number.isFinite(Number(row[props.columns.indexOf(col)])))
  }),
)

const ALL_TYPES: { type: ChartType; label: string }[] = [
  { type: 'kpi', label: 'KPI' },
  { type: 'bar', label: '柱状' },
  { type: 'horizontal_bar', label: '横向条形' },
  { type: 'line', label: '折线' },
  { type: 'area', label: '面积' },
  { type: 'grouped_bar', label: '分组柱' },
  { type: 'stacked_bar', label: '堆叠柱' },
  { type: 'donut', label: '环形' },
  { type: 'pie', label: '饼图' },
  { type: 'scatter', label: '散点' },
  { type: 'heatmap', label: '热力' },
  { type: 'waterfall', label: '瀑布' },
  { type: 'table', label: '表格' },
]

function compatible(type: ChartType): boolean {
  const n = rows.value.length
  const hasSeriesDim = !!props.chart?.series && props.chart.series !== 'period'
  if ((type === 'bar' || type === 'horizontal_bar') && hasSeriesDim) {
    // A one-dimensional bar would silently drop the second grouping column.
    return false
  }
  if (type === 'kpi') return n <= 1
  if (type === 'line' || type === 'area') return n >= 2
  if (type === 'donut' || type === 'pie') {
    return n > 1 && n <= 8 && props.metricType !== 'ratio' && props.unit !== '%'
  }
  if (type === 'scatter') return numericColumns.value.length >= 2 && n >= 2
  if (type === 'heatmap') return props.columns.length >= 3 && n >= 3
  if (type === 'grouped_bar' || type === 'stacked_bar') {
    return n > 1 && (props.compare || props.columns.length >= 3)
  }
  if (type === 'waterfall') {
    return !!props.compare && props.columns.includes('delta')
  }
  if (type === 'table') return true
  return n > 0
}

const fallbackType = computed<ChartType>(() => {
  if (!rows.value.length) return 'table'
  if (rows.value.length <= 1) return 'kpi'
  if (props.compare) return 'grouped_bar'
  if (props.columns.includes('month') || props.dimLabel === '月份') return 'line'
  if (
    rows.value.length <= 8 &&
    props.metricType !== 'ratio' &&
    props.unit !== '%'
  ) return 'donut'
  if (rows.value.length > 8) return 'horizontal_bar'
  return 'bar'
})

const autoType = computed<ChartType>(() => {
  const recommended = props.chart?.recommended
  if (recommended && compatible(recommended)) return recommended
  return fallbackType.value
})
const autoSource = computed(() => {
  if (props.chart?.source === 'llm' && props.chart.recommended === autoType.value) return '模型建议'
  return props.chart?.recommended ? '规则兜底' : '规则兜底'
})
const mode = computed<ChartType>(() => {
  if (store.chartType !== 'auto' && compatible(store.chartType)) return store.chartType
  return autoType.value
})

const visibleTypes = computed(() => {
  const alternatives = props.chart?.alternatives || []
  const wanted = new Set<ChartType>([autoType.value, ...alternatives])
  const selected = ALL_TYPES.filter((item) => wanted.has(item.type) && compatible(item.type))
  return selected.length ? selected : ALL_TYPES.filter((item) => compatible(item.type)).slice(0, 5)
})

const extraTypes = computed(() =>
  ALL_TYPES.filter((item) => !visibleTypes.value.some((v) => v.type === item.type) && compatible(item.type)),
)

function setMode(type: 'auto' | ChartType) {
  store.setChartType(type)
}

function typeLabel(type: ChartType) {
  return ALL_TYPES.find((item) => item.type === type)?.label || type
}

const line = computed(() => {
  const pts = numeric.value.slice(0, 30)
  if (!pts.length) return []
  const max = Math.max(...pts.map((p) => p.value), 1)
  const min = Math.min(...pts.map((p) => p.value), 0)
  const span = max - min || 1
  const W = 640
  const H = 240
  const PAD = 34
  return pts.map((p, i) => ({
    ...p,
    x: PAD + (i * (W - PAD * 2)) / Math.max(1, pts.length - 1),
    y: H - PAD - ((p.value - min) / span) * (H - PAD * 2),
  }))
})
const linePoints = computed(() => line.value.map((p) => `${p.x},${p.y}`).join(' '))
const areaPoints = computed(() => {
  if (!line.value.length) return ''
  const last = line.value[line.value.length - 1]
  const first = line.value[0]
  return `${first.x},206 ${linePoints.value} ${last.x},206`
})
const lineMax = computed(() => Math.max(...numeric.value.map((p) => p.value), 1))

const pie = computed(() => {
  const pts = numeric.value.filter((p) => p.value > 0).slice(0, 8)
  const total = pts.reduce((s, p) => s + p.value, 0)
  if (!total) return []
  let angle = -Math.PI / 2
  return pts.map((p, i) => {
    const frac = p.value / total
    const next = angle + frac * Math.PI * 2
    const x1 = 100 + 80 * Math.cos(angle)
    const y1 = 100 + 80 * Math.sin(angle)
    const x2 = 100 + 80 * Math.cos(next)
    const y2 = 100 + 80 * Math.sin(next)
    const large = frac > 0.5 ? 1 : 0
    const d = `M 100 100 L ${x1} ${y1} A 80 80 0 ${large} 1 ${x2} ${y2} Z`
    angle = next
    return { ...p, d, frac, idx: i }
  })
})
const pieColors = ['#175E58', '#C4552F', '#9C6C18', '#3F6B52', '#817A6D', '#B23B2B', '#5B7C99', '#8C6A9B']

const verticalBars = computed(() => {
  const max = Math.max(...numeric.value.map((p) => Math.abs(p.value)), 1)
  return [...numeric.value]
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
    .slice(0, 14)
    .map((p) => ({
      ...p,
      height: Math.max(2, (Math.abs(p.value) / max) * 150),
      negative: p.value < 0,
    }))
})

const barRows = computed(() => {
  const vi = valueIndex.value
  if (vi < 0 || props.compare) return rows.value
  return [...rows.value].sort((a, b) => Number(b[vi] || 0) - Number(a[vi] || 0))
})

const compareSeries = computed(() =>
  !!props.compare && props.columns.includes('current') && props.columns.includes('baseline'),
)

const chartSeries = computed(() => {
  const id = props.chart?.series
  const idx = id ? props.columns.indexOf(id) : -1
  return idx >= 1 ? idx : 1
})

const seriesGroups = computed(() => {
  if (compareSeries.value) return { series: [] as string[], groups: [] as any[] }
  if (props.columns.length < 3) return { series: [] as string[], groups: [] as any[] }
  const si = chartSeries.value
  const vi = valueIndex.value >= 0 ? valueIndex.value : props.columns.length - 1
  if (si < 0 || si >= props.columns.length || si === vi) {
    return { series: [] as string[], groups: [] as any[] }
  }
  const groupMap = new Map<string, { name: string; total: number; values: Map<string, number> }>()
  const seriesTotals = new Map<string, number>()
  for (const row of rows.value) {
    const group = String(row[0] ?? '')
    const series = String(row[si] ?? '')
    const value = Number(row[vi] ?? 0)
    if (!group || !series || !Number.isFinite(value)) continue
    const entry = groupMap.get(group) || { name: group, total: 0, values: new Map<string, number>() }
    entry.total += value
    entry.values.set(series, (entry.values.get(series) || 0) + value)
    groupMap.set(group, entry)
    seriesTotals.set(series, (seriesTotals.get(series) || 0) + value)
  }
  const series = [...seriesTotals.entries()]
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
    .slice(0, 7)
    .map(([name]) => name)
  const groups = [...groupMap.values()]
    .sort((a, b) => Math.abs(b.total) - Math.abs(a.total))
    .slice(0, 10)
  const max = Math.max(1, ...groups.flatMap((g) => series.map((s) => Math.abs(g.values.get(s) || 0))))
  return {
    series,
    groups: groups.map((g) => {
      const total = g.total || 1
      return {
        name: g.name,
        total: g.total,
        bars: series.map((s, idx) => {
          const value = g.values.get(s) || 0
          return {
            name: s,
            value,
            color: pieColors[idx % pieColors.length],
            pct: (Math.abs(value) / max) * 100,
            stackPct: (Math.abs(value) / Math.abs(total)) * 100,
          }
        }),
      }
    }),
  }
})

const groupedRows = computed(() => {
  const cur = props.columns.indexOf('current')
  const base = props.columns.indexOf('baseline')
  const max = Math.max(
    1,
    ...rows.value.map((r) => Math.max(
      Math.abs(Number(cur >= 0 ? r[cur] : 0)),
      Math.abs(Number(base >= 0 ? r[base] : 0)),
    )),
  )
  return rows.value.slice(0, 12).map((r) => ({
    name: String(r[0]),
    current: Number(cur >= 0 ? r[cur] : r[valueIndex.value] || 0),
    baseline: Number(base >= 0 ? r[base] : 0),
    pct: (Math.abs(Number(cur >= 0 ? r[cur] : 0)) / max) * 100,
    pct2: (Math.abs(Number(base >= 0 ? r[base] : 0)) / max) * 100,
  }))
})

const scatter = computed(() => {
  const xCol = numericColumns.value[0]
  const yCol = numericColumns.value[1]
  if (!xCol || !yCol) return []
  const xi = props.columns.indexOf(xCol)
  const yi = props.columns.indexOf(yCol)
  const pts = rows.value.slice(0, 120).map((r) => ({ x: Number(r[xi]), y: Number(r[yi]), label: String(r[0]) }))
    .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
  const maxX = Math.max(...pts.map((p) => Math.abs(p.x)), 1)
  const maxY = Math.max(...pts.map((p) => Math.abs(p.y)), 1)
  return pts.map((p) => ({
    ...p,
    cx: 28 + (Math.abs(p.x) / maxX) * 560,
    cy: 210 - (Math.abs(p.y) / maxY) * 175,
  }))
})

const heat = computed(() => {
  if (props.columns.length < 3) return { xs: [] as string[], ys: [] as string[], cells: [] as any[], max: 1 }
  const vi = props.columns.indexOf('value') >= 0 ? props.columns.indexOf('value') : 2
  const xs = [...new Set(rows.value.map((r) => String(r[0])))]
  const ys = [...new Set(rows.value.map((r) => String(r[1])))]
  const max = Math.max(1, ...rows.value.map((r) => Math.abs(Number(r[vi] || 0))))
  const cells = rows.value.map((r) => ({
    x: String(r[0]), y: String(r[1]), value: Number(r[vi] || 0),
    frac: Math.abs(Number(r[vi] || 0)) / max,
  }))
  return { xs, ys, cells, max }
})

const waterfall = computed(() => {
  const di = props.columns.indexOf('delta')
  if (di < 0) return []
  const values = rows.value.slice(0, 12).map((r) => ({ name: String(r[0]), value: Number(r[di] || 0) }))
  const max = Math.max(1, ...values.map((v) => Math.abs(v.value)))
  let running = 0
  return values.map((v) => {
    const start = running
    running += v.value
    return {
      ...v,
      startPct: (Math.abs(start) / max) * 100,
      pct: (Math.abs(v.value) / max) * 100,
      positive: v.value >= 0,
    }
  })
})

const kpiValues = computed(() =>
  props.columns.slice(0, 4).map((col, index) => ({
    label: index === 0 && col !== 'value'
      ? (props.dimLabel || col)
      : col === 'value'
        ? (props.metricLabel || '数值')
        : col === 'current' ? '本期'
          : col === 'baseline' ? '基期' : col,
    value: props.format(col, rows.value[0]?.[props.columns.indexOf(col)]),
  })),
)
</script>

<template>
  <div class="chartview">
    <div class="chartbar">
      <span class="lab">图表类型</span>
      <div class="chartmodes">
        <button :class="{ on: store.chartType === 'auto' }" title="按结果形状自动推荐" @click="setMode('auto')">自动</button>
        <button
          v-for="item in visibleTypes"
          :key="item.type"
          :class="{ on: mode === item.type && store.chartType !== 'auto' }"
          :title="item.label"
          @click="setMode(item.type)"
        >
          <ChartTypeIcon :type="item.type" />
          <span>{{ item.label }}</span>
        </button>
        <select
          v-if="extraTypes.length"
          class="chart-more"
          title="更多图表"
          :value="mode"
          @change="setMode(($event.target as HTMLSelectElement).value as ChartType)"
        >
          <option v-for="item in extraTypes" :key="item.type" :value="item.type">{{ item.label }}</option>
        </select>
      </div>
      <span class="lab auto-note" :title="chart?.reason || ''">
        自动：{{ typeLabel(mode) }}（{{ autoSource }}）
      </span>
    </div>
    <div v-if="chart?.reason" class="chart-reason">{{ chart.reason }}</div>

    <div v-if="mode === 'kpi'" class="kpi-chart">
      <div v-for="item in kpiValues" :key="item.label" class="kpi-box">
        <span>{{ item.label }}</span><b>{{ item.value }}</b>
      </div>
    </div>

    <table v-else-if="mode === 'table'" class="mini-table">
      <thead><tr><th v-for="c in columns" :key="c">{{ c }}</th></tr></thead>
      <tbody>
        <tr v-for="(row, ri) in rows.slice(0, 80)" :key="ri">
          <td v-for="(c, ci) in columns" :key="c">{{ format(c, row[ci]) }}</td>
        </tr>
      </tbody>
    </table>

    <div v-else-if="mode === 'grouped_bar' || mode === 'stacked_bar'" class="grouped-wrap">
      <template v-if="compareSeries">
        <div class="legend"><span><i class="sw a"></i>本期</span><span><i class="sw b"></i>基期</span></div>
        <div v-for="item in groupedRows" :key="item.name" class="grow">
          <span class="gname">{{ item.name }}</span>
          <div class="gtrack"><i class="gbar a" :style="{ width: item.pct + '%' }"></i><i class="gbar b" :style="{ width: item.pct2 + '%' }"></i></div>
        </div>
      </template>
      <template v-else-if="seriesGroups.series.length">
        <div class="legend">
          <span v-for="s in seriesGroups.series" :key="s">
            <i class="sw" :style="{ background: pieColors[seriesGroups.series.indexOf(s) % pieColors.length] }"></i>{{ s }}
          </span>
        </div>
        <div v-for="group in seriesGroups.groups" :key="group.name" class="g2row">
          <span class="gname" :title="group.name">{{ group.name }}</span>
          <div v-if="mode === 'stacked_bar'" class="g2stack">
            <i
              v-for="bar in group.bars.filter((b: any) => b.value !== 0)"
              :key="bar.name"
              :style="{ width: bar.stackPct + '%', background: bar.color }"
              :title="`${group.name} / ${bar.name}: ${format(key, bar.value)}`"
            ></i>
          </div>
          <div v-else class="g2bars">
            <i
              v-for="bar in group.bars.filter((b: any) => b.value !== 0)"
              :key="bar.name"
              :style="{ width: bar.pct + '%', background: bar.color }"
              :title="`${group.name} / ${bar.name}: ${format(key, bar.value)}`"
            ></i>
          </div>
          <span class="g2val">{{ format(key, group.total) }}</span>
        </div>
      </template>
      <div v-else class="chart-empty">
        这个结果没有第二分类维度，分组柱/堆叠柱无法展开；请切回表格或柱状图。
      </div>
    </div>

    <div v-else-if="mode === 'horizontal_bar'" class="hbar-wrap">
      <BarChart
        :columns="columns" :rows="barRows" :format="format" :compare="compare"
        :metric-key="metricKey" :metric-label="metricLabel" :dim-label="dimLabel"
      />
    </div>

    <div v-else-if="mode === 'bar'" class="vbar-wrap">
      <div v-for="item in verticalBars" :key="item.name" class="vcol">
        <span class="vval">{{ format(key, item.value) }}</span>
        <i :class="{ neg: item.negative }" :style="{ height: item.height + 'px' }"></i>
        <span class="vname">{{ item.name }}</span>
      </div>
    </div>

    <div v-else-if="mode === 'line' || mode === 'area'" class="linewrap">
      <svg :viewBox="'0 0 640 240'" class="linesvg">
        <line x1="34" y1="206" x2="606" y2="206" class="axis" />
        <line x1="34" y1="34" x2="34" y2="206" class="axis" />
        <polygon v-if="mode === 'area'" :points="areaPoints" class="areafill" />
        <polyline :points="linePoints" class="linepath" />
        <circle v-for="p in line" :key="p.name" :cx="p.x" :cy="p.y" r="3" class="dot" />
        <text x="4" y="16" class="ymax">{{ format(key, lineMax) }}</text>
      </svg>
      <div class="xlabels">
        <span v-for="(p, i) in line" :key="p.name">{{ i % Math.max(1, Math.ceil(line.length / 8)) === 0 ? p.name : '' }}</span>
      </div>
    </div>

    <div v-else-if="mode === 'donut' || mode === 'pie'" class="piewrap">
      <svg viewBox="0 0 200 200" class="piesvg">
        <path v-for="p in pie" :key="p.name" :d="p.d" :fill="pieColors[p.idx % pieColors.length]" />
        <circle v-if="mode === 'donut'" cx="100" cy="100" r="43" fill="var(--card)" />
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

    <div v-else-if="mode === 'scatter'" class="scatter-wrap">
      <svg viewBox="0 0 640 240" class="scattersvg">
        <line x1="28" y1="210" x2="610" y2="210" class="axis" />
        <line x1="28" y1="34" x2="28" y2="210" class="axis" />
        <circle v-for="(p, i) in scatter" :key="i" :cx="p.cx" :cy="p.cy" r="3.2" class="scatter-dot">
          <title>{{ p.label }}</title>
        </circle>
      </svg>
    </div>

    <div v-else-if="mode === 'heatmap'" class="heat-wrap">
      <div class="heat-grid" :style="{ gridTemplateColumns: `80px repeat(${heat.xs.length}, minmax(36px, 1fr))` }">
        <span></span><span v-for="x in heat.xs" :key="x" class="heat-x">{{ x }}</span>
        <template v-for="y in heat.ys" :key="y">
          <span class="heat-y">{{ y }}</span>
          <span
            v-for="x in heat.xs"
            :key="`${x}-${y}`"
            class="heat-cell"
            :style="{ opacity: 0.12 + (heat.cells.find((c) => c.x === x && c.y === y)?.frac || 0) * 0.88 }"
            :title="`${x} / ${y}`"
          ></span>
        </template>
      </div>
    </div>

    <div v-else-if="mode === 'waterfall'" class="waterfall-wrap">
      <div v-for="item in waterfall" :key="item.name" class="wrow">
        <span class="wname">{{ item.name }}</span>
        <div class="wtrack">
          <i :class="{ neg: !item.positive }" :style="{ marginLeft: item.startPct + '%', width: item.pct + '%' }"></i>
        </div>
        <span class="wval">{{ format('delta', item.value) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chartbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:6px 26px 0; }
.chartmodes { display:flex; gap:3px; align-items:center; flex-wrap:wrap; padding:2px; border:1px solid var(--rule); border-radius:3px; background:var(--card); }
.chartmodes button { display:flex; align-items:center; gap:4px; border:0; background:none; color:var(--ink-3); padding:4px 7px; border-radius:2px; cursor:pointer; font:9px var(--mono); }
.chartmodes button:hover { color:var(--ink); }
.chartmodes button.on { background:var(--ink); color:#F7F2E9; }
.chart-more { border:0; background:transparent; color:var(--ink-3); font:9px var(--mono); max-width:64px; }
.auto-note { max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chart-reason { padding:6px 26px 4px; color:var(--ink-3); font-size:11px; }
.kpi-chart { display:flex; gap:10px; flex-wrap:wrap; padding:20px 26px; }
.kpi-box { min-width:150px; border:1px solid var(--rule); background:var(--card); padding:14px; display:flex; flex-direction:column; gap:8px; }
.kpi-box span { color:var(--ink-3); font-size:10px; }
.kpi-box b { font-family:var(--serif); font-size:26px; font-weight:500; color:var(--ink); }
.mini-table { margin:14px 26px 30px; border-collapse:collapse; width:calc(100% - 52px); font-size:11px; }
.mini-table th, .mini-table td { border-bottom:1px solid var(--rule); padding:8px 10px; text-align:left; }
.mini-table th { color:var(--ink-3); font-weight:500; }
.vbar-wrap { display:flex; align-items:flex-end; gap:12px; height:250px; padding:20px 30px 16px; overflow:auto; }
.vcol { display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:5px; min-width:44px; height:100%; }
.vcol i { width:26px; background:var(--accent); opacity:.8; border-radius:1px 1px 0 0; display:block; }
.vcol i.neg { background:var(--rust); }
.vval { font-size:9px; color:var(--ink-3); white-space:nowrap; }
.vname { font-size:9px; color:var(--ink-2); max-width:54px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.grouped-wrap { padding:14px 26px 30px; max-width:920px; }
.legend { display:flex; gap:16px; color:var(--ink-3); font-size:10px; margin-bottom:10px; }
.legend .sw { width:8px; height:8px; display:inline-block; margin-right:5px; }
.legend .sw.a { background:var(--accent); }
.legend .sw.b { background:#9C6C18; }
.grow { display:flex; align-items:center; gap:12px; margin:7px 0; }
.gname { width:120px; text-align:right; color:var(--ink-2); font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.gtrack { flex:1; display:flex; flex-direction:column; gap:2px; }
.gbar { display:block; height:7px; border-radius:2px; }
.gbar.a { background:var(--accent); }
.gbar.b { background:#9C6C18; opacity:.75; }
.g2row { display:flex; align-items:center; gap:10px; margin:7px 0; }
.g2bars { flex:1; display:flex; flex-direction:column; gap:2px; }
.g2bars i { display:block; height:7px; border-radius:2px; min-width:1px; }
.g2stack { flex:1; display:flex; height:13px; gap:1px; background:#f0eadd; border-radius:2px; overflow:hidden; }
.g2stack i { display:block; height:100%; min-width:1px; }
.g2val { width:86px; text-align:right; font-size:10px; color:var(--ink-3); }
.chart-empty { padding:24px 0; color:var(--ink-3); font-size:11px; }
.areafill { fill:var(--accent); opacity:.13; }
.scatter-dot { fill:var(--accent); opacity:.7; }
.scatter-wrap { padding:10px 26px 30px; }
.scattersvg { width:100%; max-width:820px; }
.heat-wrap { padding:16px 26px 30px; overflow:auto; }
.heat-grid { display:grid; gap:3px; align-items:center; min-width:480px; }
.heat-x { font-size:9px; color:var(--ink-3); text-align:center; }
.heat-y { font-size:9px; color:var(--ink-3); text-align:right; padding-right:8px; }
.heat-cell { height:26px; min-width:30px; background:var(--accent); border-radius:2px; }
.waterfall-wrap { padding:16px 26px 30px; max-width:920px; }
.wrow { display:flex; align-items:center; gap:10px; margin:7px 0; }
.wname { width:120px; text-align:right; font-size:10px; color:var(--ink-2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.wtrack { flex:1; height:8px; background:#f0eadd; border-radius:2px; }
.wtrack i { display:block; height:100%; background:var(--accent); border-radius:2px; }
.wtrack i.neg { background:var(--rust); }
.wval { width:90px; text-align:right; font-size:10px; color:var(--ink-3); }
</style>
