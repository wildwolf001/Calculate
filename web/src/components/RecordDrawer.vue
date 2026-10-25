<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import SqlPanel from './SqlPanel.vue'
import { useSessionStore } from '../stores/session'

const store = useSessionStore()
const tab = ref<'preview' | 'sql' | 'scope' | 'checks'>('preview')

watch(() => store.activeRecord?.result_id, () => {
  tab.value = 'preview'
})

const rec = computed(() => store.activeRecord)
const metricName = computed(() => {
  const id = rec.value?.metric
  return store.meta?.metrics.find((m) => m.id === id)?.name || id || '—'
})
const dimNames = computed(() =>
  (rec.value?.dimensions || []).map(
    (d) => store.meta?.dimensions.find((x) => x.id === d)?.name || d,
  ),
)
const kindLabel = computed(() => {
  const kind = rec.value?.kind
  if (kind === 'analysis') return '分析板块'
  if (kind === 'subquestion') return '子问题'
  if (kind === 'interpretation') return '口径解释'
  return '查询'
})
const timeLabel = computed(() => {
  const c = rec.value?.comparison
  if (c) {
    return `${c.current?.start || '—'} 至 ${c.current?.end || '—'} vs ${c.baseline?.start || '—'} 至 ${c.baseline?.end || '—'}`
  }
  const t = rec.value?.time
  return t ? `${t.start} 至 ${t.end}` : '—'
})
const filterLabel = computed(() => {
  const filters = rec.value?.filters || {}
  return Object.keys(filters).length ? JSON.stringify(filters) : '无'
})
const factsJson = computed(() => {
  const facts = rec.value?.facts || {}
  return Object.keys(facts).length ? JSON.stringify(facts, null, 2) : ''
})
const verdictText = computed(() => {
  switch (store.verify?.verdict) {
    case 'reproduced':
      return '复现成功：SQL 与结果一致'
    case 'data_changed':
      return '结果与历史快照不一致，数据可能已变化'
    case 'query_changed':
      return '当前语义层编译出的 SQL 与记录不一致'
    case 'error':
      return '重跑失败'
    case 'unavailable':
      return '无法重跑：记录缺少计划或 SQL'
    default:
      return ''
  }
})

function openInCanvas() {
  if (rec.value) store.loadHistoryItem(rec.value)
}

function fmt(value: any): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return value.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
  }
  return String(value)
}
</script>

<template>
  <div v-if="store.recordOpen && rec" class="record-mask" @click.self="store.closeRecord()">
    <aside class="record-drawer">
      <header class="rd-head">
        <div style="min-width: 0">
          <div class="eyebrow">QUERY RECORD · 查询记录</div>
          <div class="rd-title">{{ rec.question || rec.result_id }}</div>
          <div class="rd-sub">{{ rec.ts }} · {{ kindLabel }} · {{ rec.result_id }}</div>
        </div>
        <button class="ghost" @click="store.closeRecord()">×</button>
      </header>

      <div class="rd-actions">
        <button class="analysis-btn" @click="openInCanvas">在画布中打开</button>
        <button class="analysis-btn" :disabled="store.verifying" @click="store.runVerify()">
          {{ store.verifying ? '重跑中…' : '重跑验证' }}
        </button>
        <span class="rd-status" :class="rec.status">{{ rec.status === 'blocked' ? '已拦截' : '成功' }}</span>
      </div>

      <div v-if="store.verify" class="rd-verdict" :class="store.verify.verdict">
        <div class="rdv-title">{{ verdictText }}</div>
        <div class="rdv-meta">
          <span>SQL {{ store.verify.checks?.sql_match ? '一致' : '不一致' }}</span>
          <span>指纹 {{ store.verify.checks?.fingerprint_match ? '一致' : '不一致' }}</span>
          <span>行内容 {{ store.verify.checks?.rows_match ? '一致' : '不一致' }}</span>
          <span v-if="store.verify.stored?.row_count !== store.verify.current?.row_count">
            行数 {{ store.verify.stored?.row_count }} → {{ store.verify.current?.row_count }}
          </span>
          <span v-if="store.verify.warehouse_changed">数据版本已变化</span>
          <span v-if="store.verify.current?.duration_ms != null">
            重跑 {{ store.verify.current.duration_ms }} ms
          </span>
        </div>
        <div v-if="store.verify.message" class="rdv-msg">{{ store.verify.message }}</div>
      </div>

      <div class="rd-tabs">
        <button :class="{ active: tab === 'preview' }" @click="tab = 'preview'">结果预览</button>
        <button :class="{ active: tab === 'sql' }" @click="tab = 'sql'">SQL</button>
        <button :class="{ active: tab === 'scope' }" @click="tab = 'scope'">口径</button>
        <button :class="{ active: tab === 'checks' }" @click="tab = 'checks'">校验</button>
      </div>

      <div class="rd-body">
        <template v-if="tab === 'preview'">
          <table class="rd-table">
            <thead>
              <tr><th v-for="c in rec.columns" :key="c">{{ c }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in rec.preview || []" :key="i">
                <td v-for="(value, j) in row" :key="j">{{ fmt(value) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="rd-note">
            预览前 {{ (rec.preview || []).length }} 行，共 {{ rec.row_count }} 行{{ rec.truncated ? '（已截断）' : '' }}。
            点击「在画布中打开」查看完整结果。
          </div>
        </template>

        <SqlPanel
          v-else-if="tab === 'sql'"
          :sql="rec.sql || ''"
          :params="rec.params || []"
          :fingerprint="rec.fingerprint"
        />

        <div v-else-if="tab === 'scope'" class="rd-kv">
          <div><b>指标</b><span>{{ metricName }}</span></div>
          <div><b>维度</b><span>{{ dimNames.length ? dimNames.join(' / ') : '总体' }}</span></div>
          <div><b>时间 / 对比</b><span>{{ timeLabel }}</span></div>
          <div><b>筛选</b><span>{{ filterLabel }}</span></div>
          <div><b>limit</b><span>{{ rec.limit ?? '—' }}</span></div>
          <div v-if="rec.degraded_reason"><b>退化说明</b><span>{{ rec.degraded_reason }}</span></div>
          <div><b>fingerprint</b><span>{{ rec.fingerprint || '—' }}</span></div>
          <div>
            <b>语义层版本</b>
            <span>
              v{{ store.historyMeta?.semantic_version ?? '—' }}
              · compiler v{{ store.historyMeta?.compiler_version ?? '—' }}
            </span>
          </div>
          <div><b>数据版本</b><span>{{ rec.warehouse_version || '—' }}</span></div>
          <div><b>耗时</b><span>{{ rec.duration_ms ?? '—' }} ms</span></div>
          <div v-if="rec.answer"><b>当时的回答</b><span class="rd-answer">{{ rec.answer }}</span></div>
        </div>

        <div v-else class="rd-checks">
          <div v-if="!(rec.checks || []).length" class="rd-empty">没有校验提示。</div>
          <div
            v-for="c in rec.checks || []"
            :key="c.id + (c.title || '')"
            class="chk"
            :class="c.severity"
          >
            <span class="sev">{{ c.severity }}</span>
            <span><b>{{ c.title }}</b><template v-if="c.detail">：{{ c.detail }}</template></span>
          </div>
          <pre v-if="factsJson" class="rd-facts">{{ factsJson }}</pre>
        </div>
      </div>
    </aside>
  </div>
</template>
