<script setup lang="ts">
import { computed, ref } from 'vue'
import BarChart from './BarChart.vue'
import ChartTypeIcon from './ChartTypeIcon.vue'
import { useSessionStore } from '../stores/session'
import type { ChartSpec, ChartType } from '../types'

const store = useSessionStore()

const props = defineProps<{
  columns: string[]
  rows: any[][]
  format: (col: string, value: any) => string
  labels?: Record<string, string>
  compare?: boolean
  metricKey?: string
  metricLabel?: string
  dimLabel?: string
  metricType?: string
  unit?: string
  chart?: ChartSpec | null
  rowCount?: number
  truncated?: boolean
}>()

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

const PIE_COLORS = ['#175E58', '#C4552F', '#9C6C18', '#3F6B52', '#817A6D', '#B23B2B', '#5B7C99', '#8C6A9B']
const hiddenSeries = ref<Set<string>>(new Set())

function toggleSeries(name: string) {
  const next = new Set(hiddenSeries.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  hiddenSeries.value = next
}

function drillValue(value: string) {
  const dim = xKey.value
  if (!dim || !value || store.busy) return
  void store.drillCurrent('filter', dim, value)
}

function labelOf(key: string | null | undefined): string {
  if (!key) return ''
  return props.labels?.[key] || key
}

const key = computed(() => props.chart?.value || props.chart?.y || props.metricKey || 'value')
const xKey = computed(() => props.chart?.x || props.columns[0] || '')
const valueIndex = computed(() => {
  const direct = props.columns.indexOf(key.value)
  if (direct >= 0) return direct
  if (props.columns.includes('value')) return props.columns.indexOf('value')
  if (props.columns.includes('current')) return props.columns.indexOf('current')
  return props.columns.length ? props.columns.length - 1 : -1
})
const xIndex = computed(() => {
  const direct = props.columns.indexOf(xKey.value)
  return direct >= 0 ? direct : 0
})
const seriesIndex = computed(() => {
  const declared = props.chart?.series
  if (!declared || declared === 'period') return -1
  const direct = props.columns.indexOf(declared)
  if (direct >= 0 && direct !== valueIndex.value && direct !== xIndex.value) return direct
  if (props.columns.length >= 3 && valueIndex.value !== 1 && 1 !== xIndex.value) return 1
  return -1
})
const hasSeriesDim = computed(() => seriesIndex.value >= 0)

function defaultTopN(type: ChartType): number {
  return ({
    kpi: 4, line: 60, area: 60, bar: 20, horizontal_bar: 20,
    grouped_bar: 30, stacked_bar: 30, donut: 8, pie: 8,
    scatter: 120, heatmap: 200, waterfall: 30, table: 80,
  } as Record<ChartType, number>)[type] || 20
}

function compatible(type: ChartType): boolean {
  const n = props.rows.length
  if ((type === 'bar' || type === 'horizontal_bar') && hasSeriesDim.value) return false
  if (type === 'kpi') return n <= 1
  if (type === 'line' || type === 'area') return n >= 2
  if (type === 'donut' || type === 'pie') {
    return n > 1 && n <= 8 && props.metricType !== 'ratio' && props.unit !== '%'
  }
  if (type === 'scatter') return numericColumns.value.length >= 2 && n >= 2
  if (type === 'heatmap') return props.columns.length >= 3 && n >= 3
  if (type === 'grouped_bar' || type === 'stacked_bar') {
    return n > 1 && (!!props.compare || props.columns.length >= 3)
  }
  if (type === 'waterfall') return !!props.compare && props.columns.includes('delta')
  if (type === 'table') return true
  return n > 0
}

const fallbackType = computed<ChartType>(() => {
  if (!props.rows.length) return 'table'
  if (props.rows.length <= 1) return 'kpi'
  if (props.compare) return 'grouped_bar'
  if (props.columns.includes('month') || props.dimLabel === '月份') return 'line'
  if (props.rows.length <= 8 && props.metricType !== 'ratio' && props.unit !== '%') return 'donut'
  if (props.rows.length > 8) return 'horizontal_bar'
  return 'bar'
})

const autoType = computed<ChartType>(() => {
  const recommended = props.chart?.recommended
  if (recommended && compatible(recommended)) return recommended
  return fallbackType.value
})

const autoSource = computed(() => {
  if (props.chart?.source === 'llm' && props.chart.recommended === autoType.value) return '模型建议'
  if (props.chart?.source === 'rule') return '规则推荐'
  return '规则兜底'
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

const totalRows = computed(() =>
  Math.max(props.chart?.total_rows ?? props.rowCount ?? props.rows.length, props.rows.length),
)

const topN = computed(() => {
  if (props.chart?.recommended === mode.value && props.chart.top_n) return props.chart.top_n
  return defaultTopN(mode.value)
})

const orderedRows = computed(() => {
  const list = [...props.rows]
  const sort = props.chart?.sort || 'source_order'
  const vi = valueIndex.value
  const xi = xIndex.value
  if (sort === 'value_desc' && vi >= 0) {
    list.sort((a, b) => Number(b[vi] || 0) - Number(a[vi] || 0))
  } else if (sort === 'category_asc' && xi >= 0) {
    list.sort((a, b) => String(a[xi] ?? '').localeCompare(String(b[xi] ?? ''), 'zh-CN'))
  } else if (sort === 'time_asc' && xi >= 0) {
    list.sort((a, b) => String(a[xi] ?? '').localeCompare(String(b[xi] ?? ''), 'zh-CN'))
  }
  return list
})

const displayRows = computed(() => orderedRows.value.slice(0, topN.value))
const omittedRows = computed(() => Math.max(0, totalRows.value - displayRows.value.length))
const limitNote = computed(() => {
  if (omittedRows.value > 0) {
    return `图表展示前 ${displayRows.value.length} 行，共 ${totalRows.value} 行${props.truncated ? '；查询结果已截断' : ''}`
  }
  if (props.truncated) return `查询结果已截断，当前为返回的 ${props.rows.length} 行`
  return ''
})

const valueAxisLabel = computed(() => {
  const label = props.chart?.value_label || labelOf(key.value)
  if (props.unit && !String(label).includes(props.unit)) return `${label}（${props.unit}）`
  return label
})

const numericColumns = computed(() =>
  props.columns.filter((col) => {
    if (['value', 'current', 'baseline', 'delta', 'growth_rate', 'numerator', 'denominator'].includes(col)) return true
    return props.rows.some((row) => Number.isFinite(Number(row[props.columns.indexOf(col)])))
  }),
)

const points = computed(() =>
  displayRows.value
    .map((row) => {
      const vi = valueIndex.value
      const xi = xIndex.value
      return {
        name: String(row[xi] ?? ''),
        value: Number(row[vi] ?? NaN),
        row,
      }
    })
    .filter((item) => Number.isFinite(item.value)),
)

const verticalBars = computed(() => {
  const max = Math.max(1, ...points.value.map((p) => Math.abs(p.value)))
  return [...points.value]
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
    .map((p) => ({
      ...p,
      height: Math.max(2, (Math.abs(p.value) / max) * 150),
      negative: p.value < 0,
    }))
})

const barRows = computed(() => (props.compare ? orderedRows.value : displayRows.value).slice(0, topN.value))

const compareSeries = computed(() =>
  !!props.compare && props.columns.includes('current') && props.columns.includes('baseline'),
)

const seriesGroups = computed(() => {
  const empty = {
    series: [] as string[],
    legendSeries: [] as string[],
    hiddenSeries: hiddenSeries.value,
    groups: [] as any[],
    omittedGroups: 0,
    omittedSeries: 0,
  }
  if (compareSeries.value) return empty
  if (seriesIndex.value < 0 || seriesIndex.value === valueIndex.value) return empty
  const groupMap = new Map<string, { name: string; total: number; values: Map<string, number> }>()
  const seriesTotals = new Map<string, number>()
  for (const row of displayRows.value) {
    const group = String(row[xIndex.value] ?? '')
    const series = String(row[seriesIndex.value] ?? '')
    const value = Number(row[valueIndex.value] ?? 0)
    if (!group || !series || !Number.isFinite(value)) continue
    const entry = groupMap.get(group) || { name: group, total: 0, values: new Map<string, number>() }
    entry.total += value
    entry.values.set(series, (entry.values.get(series) || 0) + value)
    groupMap.set(group, entry)
    seriesTotals.set(series, (seriesTotals.get(series) || 0) + value)
  }
  const allSeries = [...seriesTotals.entries()].sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
  const allGroups = [...groupMap.values()].sort((a, b) => Math.abs(b.total) - Math.abs(a.total))
  const legendSeries = allSeries.slice(0, 7).map(([name]) => name)
  const series = legendSeries.filter((name) => !hiddenSeries.value.has(name))
  const groups = allGroups.slice(0, 10)
  const max = Math.max(1, ...groups.flatMap((g) => series.map((s) => Math.abs(g.values.get(s) || 0))))
  return {
    series,
    legendSeries,
    hiddenSeries: hiddenSeries.value,
    omittedGroups: Math.max(0, allGroups.length - groups.length),
    omittedSeries: Math.max(0, allSeries.length - legendSeries.length),
    groups: groups.map((g) => {
      const bars = series.map((s, idx) => {
        const value = g.values.get(s) || 0
        return {
          name: s,
          value,
          color: PIE_COLORS[idx % PIE_COLORS.length],
          pct: (Math.abs(value) / max) * 100,
          stackPct: 0,
        }
      })
      const total = bars.reduce((sum, bar) => sum + Math.abs(bar.value), 0) || 1
      return {
        name: g.name,
        total: bars.reduce((sum, bar) => sum + bar.value, 0),
        bars: bars.map((bar) => ({ ...bar, stackPct: (Math.abs(bar.value) / total) * 100 })),
      }
    }),
  }
})

const groupedRows = computed(() => {
  const cur = props.columns.indexOf('current')
  const base = props.columns.indexOf('baseline')
  const max = Math.max(
    1,
    ...displayRows.value.map((r) => Math.max(
      Math.abs(Number(cur >= 0 ? r[cur] : 0)),
      Math.abs(Number(base >= 0 ? r[base] : 0)),
    )),
  )
  return displayRows.value.map((r) => ({
    name: String(r[xIndex.value] ?? ''),
    current: Number(cur >= 0 ? r[cur] : r[valueIndex.value] || 0),
    baseline: Number(base >= 0 ? r[base] : 0),
    pct: (Math.abs(Number(cur >= 0 ? r[cur] : 0)) / max) * 100,
    pct2: (Math.abs(Number(base >= 0 ? r[base] : 0)) / max) * 100,
  }))
})

const pie = computed(() => {
  const source = displayRows.value
  const points = source
    .map((row, idx) => ({
      name: String(row[xIndex.value] ?? ''),
      value: Number(row[valueIndex.value] ?? 0),
      idx,
      row,
    }))
    .filter((p) => Number.isFinite(p.value) && p.value > 0)
  const rest = orderedRows.value.slice(displayRows.value.length)
    .map((row) => Number(row[valueIndex.value] ?? 0))
    .filter((v) => Number.isFinite(v) && v > 0)
  if (rest.length) {
    points.push({ name: '其他', value: rest.reduce((sum, v) => sum + v, 0), idx: points.length, row: [] })
  }
  const total = points.reduce((sum, p) => sum + p.value, 0)
  if (!total) return []
  let angle = -Math.PI / 2
  return points.map((p) => {
    const frac = p.value / total
    const next = angle + frac * Math.PI * 2
    const x1 = 100 + 80 * Math.cos(angle)
    const y1 = 100 + 80 * Math.sin(angle)
    const x2 = 100 + 80 * Math.cos(next)
    const y2 = 100 + 80 * Math.sin(next)
    const large = frac > 0.5 ? 1 : 0
    const d = `M 100 100 L ${x1} ${y1} A 80 80 0 ${large} 1 ${x2} ${y2} Z`
    angle = next
    return { ...p, d, frac, color: PIE_COLORS[p.idx % PIE_COLORS.length] }
  })
})

const pieExcluded = computed(() =>
  displayRows.value.filter((row) => Number(row[valueIndex.value] ?? 0) <= 0).length,
)

const line = computed(() => {
  const pts = points.value
  if (!pts.length) return []
  const values = pts.map((p) => p.value)
  const min = Math.min(0, ...values)
  const max = Math.max(0, ...values)
  const span = max - min || 1
  const W = 640
  const H = 250
  const PAD = 38
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
  return `${first.x},212 ${linePoints.value} ${last.x},212`
})
const lineStats = computed(() => {
  const values = points.value.map((p) => p.value)
  const min = Math.min(0, ...values)
  const max = Math.max(0, ...values)
  const span = max - min || 1
  return {
    min,
    max,
    ticks: [0, 1, 2, 3].map((i) => {
      const value = min + (span * i) / 3
      return {
        value,
        y: 250 - 38 - ((value - min) / span) * (250 - 76),
      }
    }),
  }
})

const lineMax = computed(() => lineStats.value.max)

const heat = computed(() => {
  if (props.columns.length < 3) return { xs: [] as string[], ys: [] as string[], cells: [] as any[], max: 1, min: 0 }
  const xi = xIndex.value
  const yi = seriesIndex.value >= 1 ? seriesIndex.value : (xi === 1 ? 0 : 1)
  if (yi === xIndex.value || yi === valueIndex.value) {
    return { xs: [] as string[], ys: [] as string[], cells: [] as any[], max: 1, min: 0 }
  }
  const rows = displayRows.value
  const xs = [...new Set(rows.map((r) => String(r[xi] ?? '')))]
  const ys = [...new Set(rows.map((r) => String(r[yi] ?? '')))]
  const values = rows.map((r) => Number(r[valueIndex.value] || 0)).filter(Number.isFinite)
  const max = Math.max(1, ...values.map(Math.abs))
  const min = Math.min(0, ...values)
  const cells = rows.map((r) => ({
    x: String(r[xi] ?? ''),
    y: String(r[yi] ?? ''),
    value: Number(r[valueIndex.value] || 0),
    frac: Math.abs(Number(r[valueIndex.value] || 0)) / max,
  }))
  return { xs, ys, cells, max, min }
})

const waterfall = computed(() => {
  const di = props.columns.indexOf('delta')
  if (di < 0) return []
  const values = displayRows.value.map((r) => ({ name: String(r[xIndex.value]), value: Number(r[di] || 0) }))
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

const scatter = computed(() => {
  const declaredX = props.chart?.x || null
  const declaredY = props.chart?.y || null
  const xCol = declaredX && props.columns.includes(declaredX) ? declaredX : numericColumns.value[0]
  const yCol = declaredY && props.columns.includes(declaredY) ? declaredY : numericColumns.value[1]
  if (!xCol || !yCol) return []
  const xi = props.columns.indexOf(xCol)
  const yi = props.columns.indexOf(yCol)
  if (xi < 0 || yi < 0) return []
  const pts = displayRows.value.map((r) => ({
    x: Number(r[xi]),
    y: Number(r[yi]),
    label: String(r[xIndex.value] ?? ''),
  })).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
  if (!pts.length) return []
  const xMin = Math.min(...pts.map((p) => p.x))
  const xMax = Math.max(...pts.map((p) => p.x))
  const yMin = Math.min(...pts.map((p) => p.y))
  const yMax = Math.max(...pts.map((p) => p.y))
  const xSpan = xMax - xMin || 1
  const ySpan = yMax - yMin || 1
  return pts.map((p) => ({
    ...p,
    cx: 38 + ((p.x - xMin) / xSpan) * 560,
    cy: 210 - ((p.y - yMin) / ySpan) * 175,
  }))
})

const kpiValues = computed(() =>
  props.columns.slice(0, 4).map((col, index) => ({
    label: index === 0 && col !== 'value'
      ? (labelOf(col) || props.dimLabel || col)
      : col === 'value'
        ? (props.metricLabel || labelOf(col) || '数值')
        : col === 'current' ? '本期'
          : col === 'baseline' ? '基期' : (labelOf(col) || col),
    value: props.format(col, props.rows[0]?.[props.columns.indexOf(col)]),
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
        自动：{{ typeLabel(autoType) }}（{{ autoSource }}）
      </span>
    </div>
    <div v-if="chart?.reason" class="chart-reason">{{ chart.reason }}</div>
    <div v-if="limitNote" class="chart-note">{{ limitNote }}</div>

    <div v-if="mode === 'kpi'" class="kpi-chart">
      <div v-for="item in kpiValues" :key="item.label" class="kpi-box">
        <span>{{ item.label }}</span><b>{{ item.value }}</b>
      </div>
    </div>

    <table v-else-if="mode === 'table'" class="mini-table">
      <thead><tr><th v-for="c in columns" :key="c">{{ labelOf(c) }}</th></tr></thead>
      <tbody>
        <tr v-for="(row, ri) in displayRows" :key="ri">
          <td v-for="(c, ci) in columns" :key="c">{{ format(c, row[ci]) }}</td>
        </tr>
      </tbody>
    </table>

    <div v-else-if="mode === 'grouped_bar' || mode === 'stacked_bar'" class="grouped-wrap">
      <template v-if="compareSeries">
        <div class="legend"><span><i class="sw a"></i>本期</span><span><i class="sw b"></i>基期</span></div>
        <div v-for="item in groupedRows" :key="item.name" class="grow">
          <span class="gname" :title="item.name">{{ item.name }}</span>
          <div class="gtrack"><i class="gbar a" :style="{ width: item.pct + '%' }"></i><i class="gbar b" :style="{ width: item.pct2 + '%' }"></i></div>
          <span class="g2val">{{ format('current', item.current) }} / {{ format('baseline', item.baseline) }}</span>
        </div>
      </template>
      <template v-else-if="seriesGroups.legendSeries.length">
        <div class="legend">
          <button
            v-for="s in seriesGroups.legendSeries"
            :key="s"
            class="legend-item"
            :class="{ off: seriesGroups.hiddenSeries.has(s) }"
            :title="seriesGroups.hiddenSeries.has(s) ? '显示该系列' : '隐藏该系列'"
            @click="toggleSeries(s)"
          >
            <i class="sw" :style="{ background: PIE_COLORS[seriesGroups.legendSeries.indexOf(s) % PIE_COLORS.length] }"></i>{{ s }}
          </button>
        </div>
        <div v-if="!seriesGroups.series.length" class="chart-empty">已隐藏全部系列；点击图例可恢复。</div>
        <template v-else>
          <div v-for="group in seriesGroups.groups" :key="group.name" class="g2row" @click="drillValue(group.name)">
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
        <div v-if="seriesGroups.omittedGroups || seriesGroups.omittedSeries" class="chart-note">
          分组图展示前 {{ seriesGroups.groups.length }} 组、{{ seriesGroups.series.length }} 个系列
          <template v-if="seriesGroups.omittedGroups">；另有 {{ seriesGroups.omittedGroups }} 组未展开</template>
          <template v-if="seriesGroups.omittedSeries">；另 {{ seriesGroups.omittedSeries }} 个系列未展开</template>
        </div>
        </template>
      </template>
      <div v-else class="chart-empty">
        这个结果没有第二分类维度，分组柱/堆叠柱无法展开；请切回表格或柱状图。
      </div>
    </div>

    <div v-else-if="mode === 'horizontal_bar'" class="hbar-wrap">
      <BarChart
        :columns="columns" :rows="barRows" :format="format" :compare="compare"
        :metric-key="key" :metric-label="metricLabel" :dim-label="dimLabel"
        :labels="labels"
      />
    </div>

    <div v-else-if="mode === 'bar'" class="vbar-wrap">
      <div v-for="item in verticalBars" :key="item.name" class="vcol" @click="drillValue(item.name)">
        <span class="vval" :title="item.name">{{ format(key, item.value) }}</span>
        <i :class="{ neg: item.negative }" :style="{ height: item.height + 'px' }" :title="`${item.name}: ${format(key, item.value)}`"></i>
        <span class="vname" :title="item.name">{{ item.name }}</span>
      </div>
    </div>

    <div v-else-if="mode === 'line' || mode === 'area'" class="linewrap">
      <div class="axis-title">{{ valueAxisLabel }}</div>
      <svg :viewBox="'0 0 640 250'" class="linesvg">
        <line x1="38" y1="212" x2="606" y2="212" class="axis" />
        <line x1="38" y1="38" x2="38" y2="212" class="axis" />
        <line v-for="tick in lineStats.ticks" :key="tick.value" x1="34" :y1="tick.y" x2="606" :y2="tick.y" class="grid" />
        <text v-for="tick in lineStats.ticks" :key="'t' + tick.value" x="4" :y="tick.y + 3" class="ymax">{{ format(key, tick.value) }}</text>
        <polygon v-if="mode === 'area'" :points="areaPoints" class="areafill" />
        <polyline :points="linePoints" class="linepath" />
        <circle v-for="p in line" :key="p.name" :cx="p.x" :cy="p.y" r="3" class="dot">
          <title>{{ p.name }}: {{ format(key, p.value) }}</title>
        </circle>
        <text v-if="lineMax" x="606" y="18" text-anchor="end" class="axis-caption">max {{ format(key, lineMax) }}</text>
      </svg>
      <div class="xlabels">
        <span v-for="(p, i) in line" :key="p.name">{{ i % Math.max(1, Math.ceil(line.length / 8)) === 0 ? p.name : '' }}</span>
      </div>
      <div class="axis-sub">{{ chart?.x_label || labelOf(xKey) }}</div>
    </div>

    <div v-else-if="mode === 'donut' || mode === 'pie'" class="piewrap">
      <svg viewBox="0 0 200 200" class="piesvg">
        <path v-for="p in pie" :key="p.name" :d="p.d" :fill="p.color" @click="drillValue(p.name)"><title>{{ p.name }}: {{ format(key, p.value) }}</title></path>
        <circle v-if="mode === 'donut'" cx="100" cy="100" r="43" fill="var(--card)" />
      </svg>
      <div class="pielegend">
        <div v-for="p in pie" :key="p.name" class="pierow">
          <i :style="{ background: p.color }"></i>
          <span class="piename">{{ p.name }}</span>
          <span class="pieval">{{ format(key, p.value) }}</span>
          <span class="piepct">{{ (p.frac * 100).toFixed(1) }}%</span>
        </div>
      </div>
      <div v-if="pieExcluded" class="chart-note">饼图只展示正值；当前另有 {{ pieExcluded }} 个零值或负值未纳入扇区。</div>
    </div>

    <div v-else-if="mode === 'scatter'" class="scatter-wrap">
      <div class="axis-title">{{ chart?.value_label || labelOf(numericColumns[1]) }} × {{ chart?.x_label || labelOf(numericColumns[0]) }}</div>
      <svg viewBox="0 0 640 250" class="scattersvg">
        <line x1="38" y1="210" x2="610" y2="210" class="axis" />
        <line x1="38" y1="34" x2="38" y2="210" class="axis" />
        <circle v-for="(p, i) in scatter" :key="i" :cx="p.cx" :cy="p.cy" r="3.2" class="scatter-dot">
          <title>{{ p.label }}: {{ format(numericColumns[0], p.x) }} / {{ format(numericColumns[1], p.y) }}</title>
        </circle>
        <text x="38" y="232" class="axis-sub">{{ labelOf(numericColumns[0]) }}</text>
        <text x="8" y="28" class="axis-sub">{{ labelOf(numericColumns[1]) }}</text>
      </svg>
    </div>

    <div v-else-if="mode === 'heatmap'" class="heat-wrap">
      <div v-if="heat.xs.length && heat.ys.length">
        <div class="heat-grid" :style="{ gridTemplateColumns: `80px repeat(${heat.xs.length}, minmax(42px, 1fr))` }">
          <span></span><span v-for="x in heat.xs" :key="x" class="heat-x">{{ x }}</span>
          <template v-for="y in heat.ys" :key="y">
            <span class="heat-y">{{ y }}</span>
            <span
              v-for="x in heat.xs"
              :key="`${x}-${y}`"
              class="heat-cell"
              :style="{ opacity: 0.12 + (heat.cells.find((c) => c.x === x && c.y === y)?.frac || 0) * 0.88 }"
              :title="`${x} / ${y}: ${format(key, heat.cells.find((c) => c.x === x && c.y === y)?.value ?? 0)}`"
              @click="drillValue(x)"
            >
              <span>{{ heat.cells.find((c) => c.x === x && c.y === y)?.value ?? '—' }}</span>
            </span>
          </template>
        </div>
        <div class="chart-note">颜色越深数值越大；悬停可查看单元格数值。热力范围 {{ format(key, heat.min) }} ~ {{ format(key, heat.max) }}。</div>
      </div>
      <div v-else class="chart-empty">这个结果没有两个可展开的分类维度，无法绘制热力图。</div>
    </div>

    <div v-else-if="mode === 'waterfall'" class="waterfall-wrap">
      <div v-for="item in waterfall" :key="item.name" class="wrow">
        <span class="wname" :title="item.name">{{ item.name }}</span>
        <div class="wtrack">
          <i :class="{ neg: !item.positive }" :style="{ marginLeft: item.startPct + '%', width: item.pct + '%' }" :title="`${item.name}: ${format('delta', item.value)}`"></i>
        </div>
        <span class="wval">{{ format('delta', item.value) }}</span>
      </div>
    </div>

    <div v-else class="chart-empty">当前结果没有可绘制的数据。</div>
  </div>
</template>

<style scoped>
.chartview { min-height: 200px; }
.chartbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:6px 26px 0; }
.chartmodes { display:flex; gap:3px; align-items:center; flex-wrap:wrap; padding:2px; border:1px solid var(--rule); border-radius:3px; background:var(--card); }
.chartmodes button { display:flex; align-items:center; gap:4px; border:0; background:none; color:var(--ink-3); padding:4px 7px; border-radius:2px; cursor:pointer; font:9px var(--mono); }
.chartmodes button:hover { color:var(--ink); }
.chartmodes button.on { background:var(--ink); color:#F7F2E9; }
.chart-more { border:0; background:transparent; color:var(--ink-3); font:9px var(--mono); max-width:64px; }
.auto-note { max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chart-reason { padding:6px 26px 4px; color:var(--ink-3); font-size:11px; }
.chart-note { padding:6px 26px 2px; color:var(--ink-3); font-size:10px; line-height:1.5; }
.kpi-chart { display:flex; gap:10px; flex-wrap:wrap; padding:20px 26px; }
.kpi-box { min-width:150px; border:1px solid var(--rule); background:var(--card); padding:14px; display:flex; flex-direction:column; gap:8px; }
.kpi-box span { color:var(--ink-3); font-size:10px; }
.kpi-box b { font-family:var(--serif); font-size:26px; font-weight:500; color:var(--ink); }
.mini-table { margin:14px 26px 30px; border-collapse:collapse; width:calc(100% - 52px); font-size:11px; }
.mini-table th, .mini-table td { border-bottom:1px solid var(--rule); padding:8px 10px; text-align:left; }
.mini-table th { color:var(--ink-3); font-weight:500; }
.vbar-wrap { display:flex; align-items:flex-end; gap:12px; height:250px; padding:20px 30px 16px; overflow:auto; }
.vcol { display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:5px; min-width:44px; height:100%; cursor:pointer; }
.vcol i { width:26px; background:var(--accent); opacity:.8; border-radius:1px 1px 0 0; display:block; }
.vcol i.neg { background:var(--rust); }
.vval { font-size:9px; color:var(--ink-3); white-space:nowrap; }
.vname { font-size:9px; color:var(--ink-2); max-width:54px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.grouped-wrap { padding:14px 26px 30px; max-width:920px; }
.legend { display:flex; gap:16px; color:var(--ink-3); font-size:10px; margin-bottom:10px; flex-wrap:wrap; align-items:center; }
.legend .sw { width:8px; height:8px; display:inline-block; margin-right:5px; }
.legend .sw.a { background:var(--accent); }
.legend .sw.b { background:#9C6C18; }
.legend-item { border:0; background:none; color:var(--ink-3); font:10px var(--mono); display:inline-flex; align-items:center; padding:2px 0; cursor:pointer; }
.legend-item:hover { color:var(--ink); }
.legend-item.off { opacity:.35; text-decoration:line-through; }
.grow { display:flex; align-items:center; gap:12px; margin:7px 0; }
.gname { width:120px; text-align:right; color:var(--ink-2); font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.gtrack { flex:1; display:flex; flex-direction:column; gap:2px; }
.gbar { display:block; height:7px; border-radius:2px; }
.gbar.a { background:var(--accent); }
.gbar.b { background:#9C6C18; opacity:.75; }
.g2row { display:flex; align-items:center; gap:10px; margin:7px 0; cursor:pointer; }
.g2bars { flex:1; display:flex; flex-direction:column; gap:2px; }
.g2bars i { display:block; height:7px; border-radius:2px; min-width:1px; }
.g2stack { flex:1; display:flex; height:13px; gap:1px; background:#f0eadd; border-radius:2px; overflow:hidden; }
.g2stack i { display:block; height:100%; min-width:1px; }
.g2val { width:124px; text-align:right; font-size:10px; color:var(--ink-3); }
.chart-empty { padding:24px 26px; color:var(--ink-3); font-size:11px; }
.areafill { fill:var(--accent); opacity:.13; }
.linewrap { padding:12px 26px 24px; }
.linesvg { width:100%; max-width:920px; display:block; overflow:visible; }
.axis { stroke:var(--rule); stroke-width:1; }
.grid { stroke:var(--rule); stroke-width:1; stroke-dasharray:2 4; opacity:.65; }
.linepath { fill:none; stroke:var(--accent); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.dot { fill:var(--accent); }
.ymax { fill:var(--ink-3); font:8px var(--mono); }
.axis-caption { fill:var(--ink-3); font:8px var(--mono); }
.axis-title { color:var(--ink-2); font-size:10px; margin:0 0 6px; }
.axis-sub { color:var(--ink-3); font-size:9px; }
.xlabels { display:flex; justify-content:space-between; margin:2px 34px 0 38px; color:var(--ink-3); font-size:8px; }
.xlabels span { flex:1; text-align:center; overflow:hidden; white-space:nowrap; }
.scatter-dot { fill:var(--accent); opacity:.7; }
.scatter-wrap { padding:10px 26px 30px; }
.scattersvg { width:100%; max-width:920px; }
.heat-wrap { padding:16px 26px 30px; overflow:auto; }
.heat-grid { display:grid; gap:3px; align-items:center; min-width:480px; }
.heat-x { font-size:9px; color:var(--ink-3); text-align:center; }
.heat-y { font-size:9px; color:var(--ink-3); text-align:right; padding-right:8px; }
.heat-cell { height:28px; min-width:34px; background:var(--accent); border-radius:2px; display:flex; align-items:center; justify-content:center; cursor:pointer; }
.heat-cell span { font:8px var(--mono); color:#fff; text-shadow:0 1px 2px rgba(0,0,0,.35); }
.waterfall-wrap { padding:16px 26px 30px; max-width:920px; }
.wrow { display:flex; align-items:center; gap:10px; margin:7px 0; }
.wname { width:120px; text-align:right; font-size:10px; color:var(--ink-2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.wtrack { flex:1; height:8px; background:#f0eadd; border-radius:2px; }
.wtrack i { display:block; height:100%; background:var(--accent); border-radius:2px; }
.wtrack i.neg { background:var(--rust); }
.wval { width:90px; text-align:right; font-size:10px; color:var(--ink-3); }
.piewrap { display:flex; align-items:center; gap:22px; padding:14px 26px 30px; flex-wrap:wrap; }
.piesvg { width:200px; height:200px; flex:0 0 200px; }
.pielegend { min-width:240px; flex:1; }
.piesvg path { cursor:pointer; }
.pierow { display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--rule); padding:5px 0; font-size:10px; cursor:pointer; }
.pierow i { width:8px; height:8px; display:block; flex:0 0 8px; }
.piename { flex:1; color:var(--ink-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.piepct { width:48px; text-align:right; color:var(--ink-3); }
.pieval { width:100px; text-align:right; color:var(--ink); }
@media (max-width: 720px) {
  .chartbar { padding:6px 14px 0; }
  .chart-reason, .chart-note { padding-left:14px; padding-right:14px; }
  .gname, .wname { width:80px; }
  .g2val { width:96px; font-size:9px; }
  .xlabels { margin-left:20px; margin-right:20px; }
  .pielegend { min-width:180px; }
}
</style>
