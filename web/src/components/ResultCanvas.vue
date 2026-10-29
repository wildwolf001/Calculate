<script setup lang="ts">
import { computed } from 'vue'
import AnalysisView from './AnalysisView.vue'
import ChartView from './ChartView.vue'
import DataTable from './DataTable.vue'
import SqlPanel from './SqlPanel.vue'
import { useSessionStore } from '../stores/session'
import type { HistoryItem } from '../types'

const store = useSessionStore()

const metric = computed(() =>
  store.meta?.metrics.find((m) => m.id === store.activePlan?.metric),
)
const unit = computed(() => metric.value?.unit || '')
const dims = computed(() => store.activePlan?.dimensions || [])
const compare = computed(() => !!(store.activePlan?.compare || (store.activePlan as any)?.comparison))
const columns = computed(() => store.result?.columns || [])
const rows = computed(() => store.result?.rows || [])
const display = computed(() => (store.result as any)?.display || null)
const provenance = computed(() => (display.value as any)?.provenance || 'documented')
const provenanceLabel = computed(() => {
  if (provenance.value === 'inferred') return '推断口径'
  if (provenance.value === 'user') return '用户口径'
  return '已确认口径'
})
const isLookup = computed(() => !!store.result?.meta?.lookup)

const title = computed(() => {
  const lastUser = store.messages.filter((m) => m.role === 'user').at(-1)?.text
  if (store.busy && lastUser) return lastUser
  return store.result?.question || lastUser || '等待查询'
})

const dimName = (id: string) =>
  store.meta?.dimensions.find((d) => d.id === id)?.name || id

const baseLabels = computed<Record<string, string>>(() => {
  const base: Record<string, string> = {
    value: metric.value?.name || '数值',
    numerator: '分子',
    denominator: '分母',
    order_count: '订单数',
    current: compare.value ? '本期' : '当前',
    baseline: '基期',
    delta: '变化',
    growth_rate: '增长率',
  }
  for (const d of dims.value) base[d] = dimName(d)
  return base
})

const labels = computed<Record<string, string>>(() => {
  const merged = { ...baseLabels.value }
  if (display.value) {
    for (const c of display.value.columns) merged[c.key] = c.label
  }
  return merged
})

const columnTypes = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {}
  if (display.value) {
    for (const c of display.value.columns) out[c.key] = c.type
  }
  return out
})

const shownKeys = computed<string[]>(() => {
  if (display.value) {
    return display.value.columns.filter((c: any) => c.show).map((c: any) => c.key)
  }
  return columns.value
})

const shownRows = computed(() =>
  rows.value.map((row) => shownKeys.value.map((key: string) => row[columns.value.indexOf(key)])),
)

const chartKey = computed(() => {
  if (isLookup.value) return 'order_count'
  return columns.value.includes('value') ? 'value' : 'order_count'
})
const chartLabel = computed(() =>
  isLookup.value ? '已送达订单数' : (metric.value?.name || '数值'),
)

const metaItems = computed(() => {
  const range = store.result?.meta?.current_range?.join(' → ')
    || (store.activePlan?.time ? `${store.activePlan.time.start} → ${store.activePlan.time.end}` : '')
  const metricText = isLookup.value
    ? (display.value?.metric_name || '维度清单')
    : metric.value
      ? `${metric.value.name}${metric.value.definition ? ' · ' + metric.value.definition : ''}`
      : (store.activePlan?.metric || '—')
  const grainText = isLookup.value
    ? '维度清单'
    : dims.value.length ? dims.value.map(dimName).join(' / ') : '总体'
  return [
    { k: 'METRIC · 口径', v: metricText, cls: '' },
    { k: 'RANGE · 时间', v: isLookup.value ? '全部数据（不受时间筛选）' : (range || '—'), cls: '' },
    { k: 'GRAIN · 粒度', v: grainText, cls: '' },
    { k: 'ROWS · 行数', v: String(store.result?.row_count ?? '—'), cls: '' },
    { k: 'CHECK · 校验', v: checkLabel.value, cls: checkClass.value },
    { k: 'FP · 指纹', v: store.result?.fingerprint || '—', cls: '' },
  ]
})

const checkLabel = computed(() => {
  if (!store.checks.length) return '通过'
  const errors = store.checks.filter((c) => c.severity === 'error').length
  const warns = store.checks.filter((c) => c.severity === 'warn').length
  const infos = store.checks.filter((c) => c.severity === 'info').length
  if (errors) return `未通过 · ${errors} 项错误`
  if (warns) return `通过 · ${warns} 项提醒`
  return `通过 · ${infos} 项说明`
})
const checkClass = computed(() =>
  store.checks.some((c) => c.severity === 'error') ? 'warn' : 'ok',
)

const topRow = computed(() => rows.value[0] || [])
const valueIndex = computed(() => columns.value.indexOf('value'))
const topValue = computed(() => {
  if (!rows.value.length) return null
  if (compare.value) {
    const i = columns.value.indexOf('current')
    return i >= 0 ? rows.value[0][i] : null
  }
  return valueIndex.value >= 0 ? rows.value[0][valueIndex.value] : null
})

const kpis = computed(() => {
  const out: { label: string; value: string; cls?: string }[] = []
  if (isLookup.value) {
    out.push({ label: display.value?.metric_name || '清单', value: `${store.result?.row_count ?? 0} 个` })
    if (topRow.value.length) {
      out.push({ label: '订单覆盖最多', value: `${topRow.value[0]} · ${fmt('order_count', topRow.value[1])}` })
    }
    out.push({ label: '耗时', value: `${store.result?.duration_ms ?? 0} ms` })
    return out
  }
  if (topValue.value !== null) {
    const topName = dims.value.length ? String(topRow.value[0]) : ''
    out.push({
      label: dims.value.length ? `${dimName(dims.value[0])} TOP` : (metric.value?.name || '结果'),
      value: fmt('value', topValue.value),
      cls: '',
    })
    if (topName) out.push({ label: 'TOP 成员', value: topName })
  }
  if (compare.value) {
    const gi = columns.value.indexOf('growth_rate')
    if (gi >= 0 && rows.value.length) {
      const g = rows.value[0][gi]
      out.push({
        label: '增长率',
        value: g == null ? '—' : `${g >= 0 ? '+' : ''}${(Number(g) * 100).toFixed(2)}%`,
        cls: g == null ? '' : g >= 0 ? 'up' : 'down',
      })
    }
  }
  const ni = columns.value.indexOf('numerator')
  const di = columns.value.indexOf('denominator')
  if (ni >= 0 && di >= 0 && rows.value.length) {
    out.push({
      label: `${labels.value.numerator || '分子'} / ${labels.value.denominator || '分母'}`,
      value: `${fmt('numerator', rows.value[0][ni])} / ${fmt('denominator', rows.value[0][di])}`,
    })
  }
  if (store.facts?.share_of_total != null) {
    out.push({
      label: '份额合计',
      value: `${(Number(store.facts.share_of_total) * 100).toFixed(2)}%`,
    })
  }
  out.push({ label: '返回行数', value: String(store.result?.row_count ?? 0) })
  out.push({ label: '耗时', value: `${store.result?.duration_ms ?? 0} ms` })
  return out.slice(0, 5)
})

const drillable = computed(
  () =>
    dims.value.length === 1 &&
    ['category', 'seller', 'product', 'state'].includes(dims.value[0]) &&
    rows.value.length > 0,
)

function fmt(col: string, value: any): string {
  if (value === null || value === undefined || value === '') return '—'
  const kind = columnTypes.value[col]
  if (kind === 'text') return String(value)
  if (typeof value === 'string') return value
  const num = Number(value)
  if (!Number.isFinite(num)) return String(value)
  if (kind === 'int') return Math.round(num).toLocaleString('zh-CN')
  if (kind === 'percent') return `${(num * 100).toFixed(2)}%`
  if (kind === 'delta') {
    const sign = num >= 0 ? '+' : ''
    return `${sign}${num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }
  if (col === 'growth_rate') {
    return `${num >= 0 ? '+' : ''}${(num * 100).toFixed(2)}%`
  }
  if (col === 'delta') {
    const sign = num >= 0 ? '+' : ''
    return `${sign}${num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }
  if (col === 'numerator' || col === 'denominator' || col === 'order_count') {
    return Math.round(num).toLocaleString('zh-CN')
  }
  if ((col === 'value' || col === 'current' || col === 'baseline') && unit.value === '%') {
    return `${(num * 100).toFixed(2)}%`
  }
  if (col === 'value' || col === 'current' || col === 'baseline') {
    if (['件', '笔', '人', '条', '款', '个', '种'].includes(unit.value)) {
      return Math.round(num).toLocaleString('zh-CN')
    }
    return num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

const navCrumbs = computed(() =>
  store.navStack
    .map((snap, index) => ({ ...snap, index }))
    .slice(-4),
)
const isAnalysisView = computed(() => store.tab === 'analysis' && !!store.analysis)
const metricName = (id?: string) =>
  store.meta?.metrics.find((item) => item.id === id)?.name || id || ''
const kindLabel = (item: HistoryItem) => {
  if (item.kind === 'analysis') return item.analysis_section || '分析板块'
  if (item.kind === 'subquestion') return '子问题'
  if (item.kind === 'interpretation') return '口径解释'
  return '查询'
}
const metricCrumb = computed(() =>
  isAnalysisView.value
    ? '经营分析'
    : isLookup.value
      ? (display.value?.metric_name || '维度清单')
      : (metric.value?.name || '查询'),
)
</script>

<template>
  <main class="canvas">
    <div class="canvas-top">
      <nav class="crumbs">
        <span>{{ store.meta?.dataset || 'ecommerce_demo' }}</span>
        <span class="sep">/</span>
        <span>{{ metricCrumb }}</span>
        <template v-for="c in navCrumbs" :key="c.result.result_id">
          <span class="sep">/</span>
          <button
            :class="{ last: c.index === store.navStack.length - 1 }"
            :title="c.label"
            @click="store.restoreNav(c.index)"
          >
            {{ c.label }}
          </button>
        </template>
        <template v-if="!navCrumbs.length && title">
          <span class="sep">/</span>
          <span class="last">{{ title }}</span>
        </template>
      </nav>
      <div class="top-actions">
        <button class="ghost" @click="store.toggleHistory()">历史结果 ▾</button>
        <button v-if="!isAnalysisView" class="ghost" @click="store.exportCurrent()">导出 CSV</button>
      </div>

      <div v-if="store.historyOpen" class="history-pop">
        <div class="hhead">查询记录 · 可复现</div>
        <div v-if="!store.history.length" class="hempty">还没有历史结果</div>
        <div
          v-for="h in store.history"
          :key="h.result_id"
          class="hitem"
          @click="store.openRecord(h)"
        >
          <div class="htitle">{{ h.question || h.result_id }}</div>
          <div class="hmeta">
            {{ h.ts }} · {{ h.row_count }} 行
            <template v-if="h.metric"> · {{ metricName(h.metric) }}</template>
            · {{ kindLabel(h) }}
            <span v-if="h.status === 'blocked'" class="hbad">已拦截</span>
          </div>
          <div class="hmeta">{{ h.result_id }}</div>
        </div>
      </div>
    </div>

    <div class="canvas-title">
      <div style="min-width: 0">
        <div class="eyebrow">{{ isAnalysisView ? '02 · BUSINESS REVIEW' : '02 · QUERY RESULT' }}</div>
        <h1>
          {{ isAnalysisView ? '经营分析' : title }}
          <span v-if="provenance !== 'documented'" class="prov-badge">{{ provenanceLabel }}</span>
        </h1>
      </div>
      <div class="tabs">
        <button :class="{ active: store.tab === 'table' }" @click="store.setTab('table')">表格</button>
        <button :class="{ active: store.tab === 'chart' }" @click="store.setTab('chart')">图表</button>
        <button :class="{ active: store.tab === 'sql' }" @click="store.setTab('sql')">SQL</button>
        <button
          v-if="store.analysis"
          :class="{ active: store.tab === 'analysis' }"
          @click="store.setTab('analysis')"
        >
          经营分析
        </button>
      </div>
    </div>

    <div v-if="store.results.length > 1 && !isAnalysisView" class="interp-tabs">
      <span class="itab-lab">{{ store.analysisMode ? '分析板块' : store.hasSubquestions ? '子问题' : '口径' }}</span>
      <button
        v-for="r in store.results"
        :key="r.id"
        :class="{ active: r.id === store.activeResultId }"
        @click="store.selectResult(r.id)"
      >
        {{ r.label || r.id }}
        <em v-if="r.preferred">{{ store.analysisMode ? '主板块' : store.hasSubquestions ? '主结果' : '首选' }}</em>
      </button>
    </div>

    <AnalysisView v-if="isAnalysisView" />

    <template v-else-if="store.result">
      <div class="meta">
        <div v-for="m in metaItems" :key="m.k" class="mitem">
          <div class="mlabel">{{ m.k }}</div>
          <div class="mval" :class="m.cls" :title="m.v">{{ m.v }}</div>
        </div>
      </div>

      <div class="kpis">
        <div v-for="k in kpis" :key="k.label" class="kpi">
          <div class="klabel">{{ k.label }}</div>
          <div class="kval" :class="k.cls">{{ k.value }}</div>
        </div>
      </div>

      <div v-show="store.tab === 'table'" class="view">
        <DataTable
          :columns="shownKeys"
          :rows="shownRows"
          :labels="labels"
          :format="fmt"
          :drillable="drillable"
          :row-count="store.result.row_count"
          :truncated="store.result.truncated"
          @drill="store.drillRow"
        />
        <div v-if="store.checks.length" class="checklist">
          <div v-for="c in store.checks" :key="c.id + c.title" class="chk" :class="c.severity">
            <span class="sev">{{ c.severity }}</span>
            <span><b>{{ c.title }}</b><template v-if="c.detail">：{{ c.detail }}</template></span>
          </div>
        </div>
      </div>

      <div v-show="store.tab === 'chart'" class="view scroll">
        <ChartView
          :columns="columns"
          :rows="rows"
          :format="fmt"
          :compare="compare"
          :metric-key="chartKey"
          :metric-label="chartLabel"
          :dim-label="isLookup ? '维度成员' : (dims.length ? dimName(dims[0]) : '数值')"
          :metric-type="metric?.type"
          :unit="unit"
        />
      </div>

      <div v-show="store.tab === 'sql'" class="view scroll">
        <SqlPanel :sql="store.sql" :params="store.result.params" :fingerprint="store.result.fingerprint" />
      </div>
    </template>

    <div v-else-if="store.busy" class="empty">
      正在查询：{{ store.stage || '处理中' }}…<br />
      <span>左侧会实时显示阶段和耗时；完成后结果会出现在这里。</span>
    </div>
    <div v-else class="empty">
      还没有查询结果。<br />
      <span>在左侧提问，或点击示例问题开始。所有计算在 DuckDB 只读连接中完成，结果可核对、可复现。</span>
    </div>

    <footer class="statusbar">
      <span>数据源 <b>{{ store.meta?.dataset || 'ecommerce_demo' }}</b> · 只读会话</span>
      <span>
        语义层 v{{ store.meta?.semantic_version ?? '—' }} ·
        {{ store.meta?.metrics.length ?? 0 }} 个指标 ·
        <template v-if="store.result">查询 <b>{{ store.result.duration_ms }} ms</b></template>
        <template v-else>状态 <b>{{ store.status }}</b></template>
      </span>
    </footer>
  </main>
</template>
