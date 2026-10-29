<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  analyzeUpload,
  applyDocumentation,
  applyJobReview,
  cancelJob,
  createUpload,
  deleteDataset,
  editDatasetDraft,
  getDataset,
  getDatasetDraft,
  getDatasetProfile,
  getDatasetReadiness,
  getDocumentation,
  getJob,
  getJobReview,
  materializeAgentJob,
  listDatasets,
  publishDataset,
  rebuildDataset,
  startAgentProfile,
  submitJobReview,
  uploadDocumentation,
} from '../api'
import type { DatasetSummary, JobStatus } from '../types'

const datasets = ref<DatasetSummary[]>([])
const selectedId = ref('')
const detail = ref<any>(null)
const profile = ref<any>(null)
const draft = ref<any>(null)
const readiness = ref<any>(null)
const showCandidates = ref(false)
const loading = ref(false)
const error = ref('')
const toast = ref('')

const files = ref<File[]>([])
const datasetName = ref('')
const uploading = ref(false)
const job = ref<JobStatus | null>(null)
const publishing = ref(false)
const selectedMetrics = ref<string[]>([])
const selectedDims = ref<string[]>([])
const totalsPolicy = ref<'exclude' | 'keep'>('exclude')
const piiPolicy = ref<'mask' | 'show'>('mask')
const includeSuspected = ref(false)
const selectedFiles = ref<string[]>([])
const docFile = ref<File | null>(null)
const documentation = ref<any>(null)
const docBusy = ref(false)
const lastUploadId = ref('')
const agentJob = ref<JobStatus | null>(null)
const agentReview = ref<any>(null)
const agentBusy = ref(false)
const agentApplied = ref<any>(null)
const agentDataset = ref<any>(null)
let agentTimer: number | undefined
const editMode = ref(false)
const metricEdits = ref<Record<string, { name_zh: string; synonyms: string }>>({})
const dimEdits = ref<Record<string, { name_zh: string; synonyms: string }>>({})
const timeColumnValue = ref('')
const railWidth = ref(300)
const dragging = ref(false)
let pollTimer: number | undefined
let dragMove: ((ev: MouseEvent) => void) | undefined
let dragUp: (() => void) | undefined

const RAIL_KEY = 'mingzhang.manage.railWidth'

function clampRail(width: number): number {
  const max = Math.max(260, Math.min(620, window.innerWidth - 520))
  return Math.max(240, Math.min(width, max))
}

function resetRailWidth() {
  railWidth.value = 300
  try {
    localStorage.setItem(RAIL_KEY, String(railWidth.value))
  } catch {
    /* ignore storage failures */
  }
}

function startDrag(event: MouseEvent) {
  if (window.innerWidth < 1000) return
  dragging.value = true
  const startX = event.clientX
  const startWidth = railWidth.value
  dragMove = (ev: MouseEvent) => {
    railWidth.value = clampRail(startWidth + ev.clientX - startX)
  }
  dragUp = () => {
    dragging.value = false
    window.removeEventListener('mousemove', dragMove!)
    window.removeEventListener('mouseup', dragUp!)
    try {
      localStorage.setItem(RAIL_KEY, String(railWidth.value))
    } catch {
      /* ignore storage failures */
    }
  }
  window.addEventListener('mousemove', dragMove)
  window.addEventListener('mouseup', dragUp)
}

const current = computed(() => datasets.value.find((d) => d.dataset_id === selectedId.value))
const isDraft = computed(() => (detail.value?.mode || current.value?.status) === 'draft')
const profileTables = computed<any[]>(() => profile.value?.tables || [])
const overview = computed(() => profile.value?.overview || {})
const quality = computed<any[]>(() =>
  (profile.value?.quality || detail.value?.quality || [])
    .filter((q: any) => (q.count || 0) > 0),
)
const metrics = computed<any[]>(() => {
  const all = draft.value?.metrics || []
  return showCandidates.value ? all : all.filter((m: any) => m.selected !== false)
})
const dimensions = computed<any[]>(() => {
  const all = draft.value?.dimensions || []
  return showCandidates.value ? all : all.filter((d: any) => d.selected !== false)
})
const metricGroups = computed(() => {
  const labels: Record<string, string> = {}
  for (const t of draft.value?.tables || []) labels[t.table_id] = t.label
  const groups: Record<string, any[]> = {}
  for (const m of metrics.value) {
    const fact = m.fact || 'unknown'
    ;(groups[fact] ||= []).push(m)
  }
  return Object.entries(groups).map(([fact, items]) => ({
    fact,
    label: labels[fact] || fact,
    metrics: items,
  }))
})
const readinessChecks = computed<any[]>(() => readiness.value?.checks || [])
const readinessIssues = computed(() =>
  readinessChecks.value.filter((c: any) => c.status !== 'pass').slice(0, 12),
)
const capabilityList = computed(() => {
  const caps = detail.value?.capabilities || {}
  const labels: Record<string, string> = {
    member_list: '成员清单',
    ratio: '比率',
    entity_count: '实体计数',
    compare: '环比/同比',
  }
  return Object.entries(caps)
    .filter(([k, v]) => k in labels && v)
    .map(([k]) => labels[k])
})
const relations = computed<any[]>(() =>
  (draft.value?.relations || []).filter((r: any) => r.accepted),
)
const timeOptions = computed(() => {
  const out: { value: string; label: string }[] = []
  for (const table of profileTables.value) {
    for (const col of table.columns || []) {
      if (/date|timestamp/i.test(String(col.type || ''))) {
        out.push({
          value: `${table.table_id}.${col.physical}`,
          label: `${table.label}.${col.label}`,
        })
      }
    }
  }
  return out
})

function isSuspected(name: string): boolean {
  return /practice|question|readme|说明|练习题|题目|指标|口径|metrics?|dictionary|codebook|schema|数据字典/i.test(name)
}

function splitSynonyms(value: string): string[] {
  return value.split(/[,，、/]/).map((x) => x.trim()).filter(Boolean)
}

function notify(text: string) {
  toast.value = text
  window.setTimeout(() => {
    if (toast.value === text) toast.value = ''
  }, 2800)
}

async function loadDatasets(select?: string) {
  const data = await listDatasets()
  datasets.value = data.datasets
  const wanted = select || selectedId.value || data.default || data.datasets[0]?.dataset_id
  if (wanted) await selectDataset(wanted)
}

async function selectDataset(datasetId: string) {
  if (!datasetId) return
  selectedId.value = datasetId
  loading.value = true
  error.value = ''
  detail.value = null
  profile.value = null
  draft.value = null
  selectedMetrics.value = []
  selectedDims.value = []
  try {
    const [d, p, dr, docs, ready] = await Promise.allSettled([
      getDataset(datasetId),
      getDatasetProfile(datasetId),
      getDatasetDraft(datasetId),
      getDocumentation(datasetId),
      getDatasetReadiness(datasetId),
    ])
    if (d.status === 'fulfilled') detail.value = d.value
    if (p.status === 'fulfilled') profile.value = p.value
    documentation.value = docs.status === 'fulfilled' ? docs.value : null
    readiness.value = ready.status === 'fulfilled' ? ready.value : null
    if (dr.status === 'fulfilled') {
      draft.value = dr.value
      selectedMetrics.value = (dr.value.metrics || [])
        .filter((m: any) => m.selected !== false)
        .map((m: any) => m.id)
      selectedDims.value = (dr.value.dimensions || [])
        .filter((x: any) => x.selected !== false)
        .map((x: any) => x.id)
      metricEdits.value = {}
      dimEdits.value = {}
      for (const m of dr.value.metrics || []) {
        metricEdits.value[m.id] = {
          name_zh: m.name_zh || m.name || '',
          synonyms: (m.synonyms || []).join('、'),
        }
      }
      for (const x of dr.value.dimensions || []) {
        dimEdits.value[x.id] = {
          name_zh: x.name_zh || x.id,
          synonyms: (x.synonyms || []).join('、'),
        }
      }
      timeColumnValue.value = dr.value.time_column
        ? `${dr.value.time_column.table}.${dr.value.time_column.column}`
        : ''
    }
  } catch (err: any) {
    error.value = err.message || String(err)
  } finally {
    loading.value = false
  }
}

function onFiles(event: Event) {
  const input = event.target as HTMLInputElement
  files.value = Array.from(input.files || [])
  selectedFiles.value = files.value
    .filter((f) => !isSuspected(f.name))
    .map((f) => f.name)
  if (!datasetName.value && files.value.length) {
    datasetName.value = files.value[0].name.replace(/\.[^.]+$/, '')
  }
}

function clearFiles() {
  files.value = []
  selectedFiles.value = []
  datasetName.value = ''
  job.value = null
}

function ingestOptions() {
  return {
    totals_policy: totalsPolicy.value,
    pii_policy: piiPolicy.value,
    include_files: selectedFiles.value,
    include_suspected: includeSuspected.value,
  }
}

async function startUpload() {
  if (!files.value.length || !selectedFiles.value.length || uploading.value) return
  uploading.value = true
  error.value = ''
  try {
    const uploaded = await createUpload(files.value)
    lastUploadId.value = uploaded.upload_id
    const started = await analyzeUpload(
      uploaded.upload_id,
      datasetName.value || undefined,
      ingestOptions(),
    )
    job.value = {
      job_id: started.job_id,
      kind: 'ingest',
      status: 'queued',
      stage: 'queued',
      percent: 0,
      message: '等待执行',
      created_at: '',
      updated_at: '',
    }
    pollJob(started.job_id, started.dataset_id, false)
  } catch (err: any) {
    error.value = err.message || String(err)
    uploading.value = false
  }
}

async function startRebuild() {
  if (!selectedId.value || uploading.value) return
  uploading.value = true
  error.value = ''
  try {
    const started = await rebuildDataset(selectedId.value, ingestOptions())
    job.value = {
      job_id: started.job_id,
      kind: 'rebuild',
      status: 'queued',
      stage: 'queued',
      percent: 0,
      message: '等待执行',
      created_at: '',
      updated_at: '',
    }
    pollJob(started.job_id, selectedId.value, true)
  } catch (err: any) {
    error.value = err.message || String(err)
    uploading.value = false
  }
}

async function runAgentProfile() {
  if (!lastUploadId.value || agentBusy.value) return
  agentBusy.value = true
  error.value = ''
  agentReview.value = null
  try {
    const started = await startAgentProfile(lastUploadId.value)
    agentJob.value = {
      job_id: started.job_id,
      kind: 'ingest_agent',
      status: 'queued',
      stage: 'queued',
      percent: 0,
      message: '等待执行',
      created_at: '',
      updated_at: '',
    }
    pollAgent(started.job_id)
  } catch (err: any) {
    agentBusy.value = false
    error.value = err.message || String(err)
  }
}

function pollAgent(jobId: string) {
  if (agentTimer) window.clearTimeout(agentTimer)
  const tick = async () => {
    try {
      const status = await getJob(jobId)
      agentJob.value = status
      if (status.status === 'running' || status.status === 'queued') {
        agentTimer = window.setTimeout(tick, 700)
        return
      }
      agentBusy.value = false
      if (status.status === 'succeeded') {
        agentReview.value = await getJobReview(jobId)
        notify('Agent 画像完成，可审核候选口径')
      } else {
        error.value = status.error || status.message || 'Agent 解析失败'
      }
    } catch (err: any) {
      agentBusy.value = false
      error.value = err.message || String(err)
    }
  }
  agentTimer = window.setTimeout(tick, 500)
}

async function applyAgentReview() {
  if (!agentJob.value?.job_id) return
  try {
    agentApplied.value = await applyJobReview(agentJob.value.job_id)
    notify('已生成候选语义草稿（还需物理仓库/binding 才能发布）')
  } catch (err: any) {
    error.value = err.message || String(err)
  }
}

async function materializeAgent() {
  if (!agentJob.value?.job_id) return
  try {
    agentDataset.value = await materializeAgentJob(
      agentJob.value.job_id,
      datasetName.value || undefined,
    )
    notify('已生成数据集草稿，可在左侧数据源中查看')
    await loadDatasets(agentDataset.value.dataset_id)
  } catch (err: any) {
    error.value = err.message || String(err)
  }
}

async function decideAgent(itemId: string, action: string) {
  if (!agentJob.value?.job_id) return
  try {
    await submitJobReview(agentJob.value.job_id, [{ item_id: itemId, action }])
    agentReview.value = await getJobReview(agentJob.value.job_id)
    notify(action === 'accept' ? '已接受该口径' : action === 'reject' ? '已拒绝该口径' : '已标记为暂缓')
  } catch (err: any) {
    error.value = err.message || String(err)
  }
}

const agentMetrics = computed<any[]>(() =>
  Object.entries(agentReview.value?.draft?.metrics || {}).map(([id, metric]: [string, any]) => ({
    id,
    ...metric,
  })),
)
const agentTables = computed<any[]>(() =>
  Object.entries(agentReview.value?.draft?.tables || {}).map(([id, table]: [string, any]) => ({
    id,
    ...table,
  })),
)
const agentDecisions = computed<any[]>(() => agentReview.value?.review?.decisions || [])

function pollJob(jobId: string, datasetId: string, rebuild: boolean) {
  if (pollTimer) window.clearTimeout(pollTimer)
  const tick = async () => {
    try {
      const status = await getJob(jobId)
      job.value = status
      if (status.status === 'running' || status.status === 'queued') {
        pollTimer = window.setTimeout(tick, 700)
        return
      }
      uploading.value = false
      if (status.status === 'succeeded') {
        notify(rebuild ? '数据源已重建，请确认后重新发布' : '解析完成，草稿已生成')
        if (!rebuild) clearFiles()
        await loadDatasets(datasetId)
      } else {
        error.value = status.error || status.message || '解析失败'
      }
    } catch (err: any) {
      uploading.value = false
      error.value = err.message || String(err)
    }
  }
  pollTimer = window.setTimeout(tick, 500)
}

async function stopJob() {
  if (!job.value) return
  try {
    await cancelJob(job.value.job_id)
  } catch {
    /* task may already be finished */
  }
}

function onDocFile(event: Event) {
  const input = event.target as HTMLInputElement
  docFile.value = input.files?.[0] || null
}

async function parseDoc() {
  if (!selectedId.value || !docFile.value || docBusy.value) return
  docBusy.value = true
  error.value = ''
  try {
    documentation.value = await uploadDocumentation(selectedId.value, docFile.value)
    notify(`口径文档已解析：${documentation.value.rules?.length || 0} 条规则`)
  } catch (err: any) {
    error.value = err.message || String(err)
  } finally {
    docBusy.value = false
  }
}

async function applyDoc() {
  if (!selectedId.value || !documentation.value || docBusy.value) return
  docBusy.value = true
  error.value = ''
  try {
    const result = await applyDocumentation(selectedId.value)
    notify(`已应用 ${result.applied?.length || 0} 条口径，发布后生效`)
    await loadDatasets(selectedId.value)
  } catch (err: any) {
    error.value = err.message || String(err)
  } finally {
    docBusy.value = false
  }
}

async function saveEdits() {
  if (!selectedId.value) return
  try {
    const payload: any = { metrics: [], dimensions: [] }
    for (const [id, value] of Object.entries(metricEdits.value)) {
      const item: any = { id, name_zh: value.name_zh }
      if (value.synonyms.trim()) item.synonyms = splitSynonyms(value.synonyms)
      payload.metrics.push(item)
    }
    for (const [id, value] of Object.entries(dimEdits.value)) {
      const item: any = { id, name_zh: value.name_zh }
      if (value.synonyms.trim()) item.synonyms = splitSynonyms(value.synonyms)
      payload.dimensions.push(item)
    }
    if (timeColumnValue.value) {
      const [table, column] = timeColumnValue.value.split('.')
      payload.time_column = { table, column }
    }
    await editDatasetDraft(selectedId.value, payload)
    editMode.value = false
    notify('语义编辑已保存，发布后生效')
    await loadDatasets(selectedId.value)
  } catch (err: any) {
    error.value = err.message || String(err)
  }
}

function toggleMetric(id: string) {
  selectedMetrics.value = selectedMetrics.value.includes(id)
    ? selectedMetrics.value.filter((x) => x !== id)
    : [...selectedMetrics.value, id]
}

function toggleDim(id: string) {
  selectedDims.value = selectedDims.value.includes(id)
    ? selectedDims.value.filter((x) => x !== id)
    : [...selectedDims.value, id]
}

async function publish() {
  if (!selectedId.value || !selectedMetrics.value.length || publishing.value) return
  publishing.value = true
  error.value = ''
  try {
    const result = await publishDataset(
      selectedId.value,
      selectedMetrics.value,
      selectedDims.value,
    )
    notify(`已发布 v${result.semantic_version}`)
    await loadDatasets(selectedId.value)
  } catch (err: any) {
    error.value = err.message || String(err)
  } finally {
    publishing.value = false
  }
}

async function removeSelected() {
  if (!current.value || selectedId.value === 'ds_ecommerce_demo') return
  if (!window.confirm(`删除数据源「${current.value.name}」？源文件会移入回收站。`)) return
  try {
    await deleteDataset(selectedId.value)
    notify('数据源已移入回收站')
    selectedId.value = ''
    await loadDatasets()
  } catch (err: any) {
    error.value = err.message || String(err)
  }
}

function fmt(value: any): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return value.toLocaleString('zh-CN')
  return String(value)
}

function pct(value: any): string {
  if (value === null || value === undefined) return '—'
  return `${(Number(value) * 100).toFixed(1)}%`
}

onMounted(async () => {
  try {
    const saved = Number(localStorage.getItem(RAIL_KEY))
    if (Number.isFinite(saved) && saved > 0) railWidth.value = clampRail(saved)
  } catch {
    /* ignore storage failures */
  }
  try {
    await loadDatasets()
  } catch (err: any) {
    error.value = err.message || String(err)
  }
})

onUnmounted(() => {
  if (pollTimer) window.clearTimeout(pollTimer)
  if (agentTimer) window.clearTimeout(agentTimer)
  if (dragMove) window.removeEventListener('mousemove', dragMove)
  if (dragUp) window.removeEventListener('mouseup', dragUp)
})
</script>

<template>
  <div class="manage">
    <header class="topbar">
      <div class="brand">
        <div class="mark">账</div>
        <div>
          <div class="bname">明账 · 数据管理</div>
          <div class="bsub">MINGZHANG · DATA SOURCE WORKBENCH</div>
        </div>
      </div>
      <div class="top-actions">
        <a class="ghost" href="/">← 返回对话</a>
      </div>
    </header>

    <div class="layout" :style="{ '--rail-w': railWidth + 'px' }">
      <aside class="rail">
        <div class="rail-head">
          <span>数据源</span>
          <button class="ghost sm" @click="clearFiles()">＋ 接入</button>
        </div>
        <div class="rail-list">
          <button
            v-for="d in datasets"
            :key="d.dataset_id"
            class="ds-item"
            :class="{ active: d.dataset_id === selectedId }"
            @click="selectDataset(d.dataset_id)"
          >
            <span class="ds-dot" :class="d.status === 'published' ? 'live' : 'draft'"></span>
            <span class="ds-main">
              <b>{{ d.name }}</b>
              <small>
                {{ d.status === 'published' ? '已发布' : '草稿' }}
                · {{ d.rows == null ? '—' : d.rows.toLocaleString('zh-CN') }} 行
                · {{ d.tables ?? '—' }} 表
              </small>
            </span>
          </button>
          <div v-if="!datasets.length" class="rail-empty">还没有数据源</div>
        </div>

        <div class="upload-card">
          <div class="card-title">接入新数据</div>
          <label class="file-pick">
            <input type="file" multiple accept=".csv,.tsv,.xlsx,.xlsm" @change="onFiles" />
            <span>{{ files.length ? `${files.length} 个文件已选择` : '选择 CSV / Excel 文件' }}</span>
          </label>
          <div v-if="files.length" class="file-list">
            <label v-for="f in files" :key="f.name" class="file-row">
              <input v-model="selectedFiles" type="checkbox" :value="f.name" />
              <span>{{ f.name }}</span>
              <em v-if="isSuspected(f.name)">疑似说明</em>
            </label>
          </div>
          <input v-model="datasetName" class="text-input" placeholder="数据源名称（可选）" />
          <div class="policy-row">
            <label>
              合计行
              <select v-model="totalsPolicy">
                <option value="exclude">排除（推荐）</option>
                <option value="keep">保留并告警</option>
              </select>
            </label>
            <label>
              敏感列样例
              <select v-model="piiPolicy">
                <option value="mask">脱敏（推荐）</option>
                <option value="show">显示</option>
              </select>
            </label>
          </div>
          <button class="primary" :disabled="!selectedFiles.length || uploading" @click="startUpload">
            {{ uploading ? '解析中…' : '上传并自动解析' }}
          </button>
          <button class="ghost" :disabled="!lastUploadId || agentBusy" @click="runAgentProfile">
            {{ agentBusy ? 'Agent 分析中…' : 'Agent 画像（审计）' }}
          </button>
          <div v-if="job" class="job">
            <div class="job-line">
              <span>{{ job.stage }}</span>
              <span>{{ job.percent }}%</span>
            </div>
            <div class="bar"><i :style="{ width: job.percent + '%' }"></i></div>
            <div class="job-msg">{{ job.message }}</div>
            <button v-if="job.status === 'running' || job.status === 'queued'"
                    class="ghost sm" @click="stopJob">取消</button>
          </div>
        </div>
      </aside>

      <div
        class="splitter"
        :class="{ dragging }"
        title="拖动调整宽度 · 双击复位"
        @mousedown="startDrag"
        @dblclick="resetRailWidth"
      ></div>

      <main class="main">
        <div v-if="error" class="banner error">{{ error }}</div>
        <div v-if="toast" class="banner ok">{{ toast }}</div>

        <div v-if="!detail && !loading" class="empty">
          <h2>选择一个数据源</h2>
          <p>上传 CSV/XLSX 后会生成物理画像、质量报告和语义草稿；确认口径后即可用于问答。</p>
        </div>
        <div v-if="loading" class="empty"><p>加载中…</p></div>

        <template v-if="detail">
          <section class="hero">
            <div>
              <div class="eyebrow">DATA SOURCE</div>
              <h1>{{ detail.name }}</h1>
              <div class="hero-meta">
                <span class="pill" :class="isDraft ? 'draft' : 'live'">
                  {{ isDraft ? '草稿口径' : '已发布' }}
                </span>
                <span>{{ detail.dataset_id }}</span>
                <span>语义 v{{ detail.semantic_version }}</span>
                <span>{{ detail.data_version }}</span>
              </div>
            </div>
            <div class="hero-actions">
              <button class="ghost" :disabled="uploading" @click="startRebuild">
                按当前策略重建
              </button>
              <button
                v-if="detail.dataset_id !== 'ds_ecommerce_demo'"
                class="ghost danger"
                @click="removeSelected"
              >删除数据源</button>
            </div>
          </section>

          <section class="cards">
            <div class="stat">
              <small>总行数</small>
              <b>{{ fmt(overview.rows ?? Object.values(detail.tables || {}).reduce((a: any, b: any) => a + (b || 0), 0)) }}</b>
            </div>
            <div class="stat">
              <small>表 / 列</small>
              <b>{{ overview.tables ?? Object.keys(detail.tables || {}).length }} / {{ overview.columns ?? '—' }}</b>
            </div>
            <div class="stat">
              <small>指标 / 维度</small>
              <b>{{ detail.metrics?.length ?? 0 }} / {{ detail.dimensions?.length ?? 0 }}</b>
            </div>
            <div class="stat">
              <small>时间范围</small>
              <b class="small">{{ (overview.time_range || detail.time_range || []).join(' → ') || '—' }}</b>
            </div>
          </section>

          <section class="panel" v-if="agentJob || agentReview">
            <div class="panel-head">
              <h3>Agent 画像 / 审核</h3>
              <span class="pill" :class="agentJob?.status === 'succeeded' ? 'live' : 'draft'">
                {{ agentJob?.status || 'ready' }}
              </span>
              <button class="ghost sm" :disabled="!agentReview" @click="applyAgentReview">
                应用审核结果
              </button>
              <button class="ghost sm" :disabled="!agentApplied" @click="materializeAgent">
                生成数据集草稿
              </button>
            </div>
            <div v-if="agentJob" class="job">
              <div class="job-line">
                <span>{{ agentJob.stage }}</span>
                <span>{{ agentJob.percent }}%</span>
              </div>
              <div class="bar"><i :style="{ width: agentJob.percent + '%' }"></i></div>
              <div class="job-msg">{{ agentJob.message }}</div>
            </div>
            <div v-if="agentTables.length" class="table-grid">
              <div v-for="t in agentTables" :key="t.id" class="table-card">
                <div class="table-top">
                  <b>{{ t.name }}</b>
                  <span>{{ t.rows }} 行</span>
                </div>
                <div class="table-sub">
                  粒度 {{ (t.grain || []).join('、') || '未确认' }}
                  <span v-if="t.grain_unique === false"> · 不唯一</span>
                </div>
              </div>
            </div>
            <div v-if="agentMetrics.length" class="quality-list">
              <div v-for="m in agentMetrics" :key="m.id" class="quality">
                <span class="sev">{{ m.provenance || 'inferred' }}</span>
                <span class="q-title">{{ m.name }}</span>
                <span class="q-count">{{ m.expr }}</span>
                <span v-if="m.probe" class="muted">probe={{ m.probe.value }}</span>
                <button class="ghost sm" @click="decideAgent(m.id, 'accept')">接受</button>
                <button class="ghost sm" @click="decideAgent(m.id, 'reject')">拒绝</button>
              </div>
            </div>
            <div v-else-if="agentReview" class="muted">没有候选指标。</div>
            <div v-if="agentDecisions.length" class="doc-note">
              已审核：
              <span v-for="d in agentDecisions" :key="d.item_id">
                {{ d.item_id }} · {{ d.action }}
              </span>
            </div>
            <div v-if="agentApplied" class="doc-note">
              已生成候选语义草稿：接受 {{ agentApplied.accepted?.length || 0 }} 项，
              拒绝 {{ agentApplied.rejected?.length || 0 }} 项。
              {{ agentApplied.message }}
            </div>
            <div v-if="agentDataset" class="doc-note">
              已生成数据集草稿：{{ agentDataset.dataset_id }}
              · {{ Object.keys(agentDataset.tables || {}).length }} 张表
              · {{ agentDataset.metrics?.length || 0 }} 个指标
            </div>
          </section>

          <section class="panel" v-if="readiness">
            <div class="panel-head">
              <h3>可用性检查</h3>
              <span
                class="pill"
                :class="readiness.status === 'ready' ? 'live' : 'draft'"
              >
                {{ readiness.status === 'ready' ? '可以发布' : '存在阻断项' }}
              </span>
            </div>
            <div class="cap-row">
              <span class="cap-chip">指标 {{ readiness.metrics?.length || 0 }}</span>
              <span class="cap-chip">维度 {{ readiness.dimensions?.length || 0 }}</span>
              <span class="cap-chip">探测 {{ readiness.probes || 0 }}</span>
              <span v-for="cap in capabilityList" :key="cap" class="cap-chip">{{ cap }}</span>
            </div>
            <div v-if="readinessIssues.length" class="quality-list">
              <div
                v-for="check in readinessIssues"
                :key="check.id"
                class="quality"
                :class="check.status === 'error' ? 'error' : 'warn'"
              >
                <span class="sev">{{ check.status }}</span>
                <span class="q-title">{{ check.id }}</span>
                <span class="q-count">{{ check.detail }}</span>
              </div>
            </div>
            <div v-else class="muted">所有探测通过。</div>
            <div v-if="readiness.blockers?.length" class="doc-warn">
              阻断：{{ readiness.blockers.join('；') }}
            </div>
            <div v-if="readiness.warnings?.length" class="doc-note">
              提示：{{ readiness.warnings.slice(0, 8).join('；') }}
            </div>
          </section>

          <section class="panel">
            <div class="panel-head">
              <h3>数据摘要</h3>
              <span class="hint">自动生成，数字来自物理画像</span>
            </div>
            <p v-if="profile?.summary" class="summary">{{ profile.summary }}</p>
            <p v-else class="summary">
              当前数据源包含 {{ detail.metrics?.length || 0 }} 个指标、{{ detail.dimensions?.length || 0 }} 个维度。
              这是内置示例数据源，暂无自动画像文件。
            </p>
            <div v-if="profileTables.length" class="table-grid">
              <div v-for="t in profileTables" :key="t.table_id" class="table-card">
                <div class="table-top">
                  <b>{{ t.label }}</b>
                  <span>{{ t.rows.toLocaleString('zh-CN') }} 行</span>
                </div>
                <div class="table-sub">
                  粒度 {{ (t.grain || []).join('、') || '无唯一主键' }}
                  <span v-if="t.duplicate_rows"> · 重复行 {{ t.duplicate_rows }}</span>
                </div>
                <div class="cols">
                  <span v-for="c in t.columns.slice(0, 8)" :key="c.physical" class="col">
                    {{ c.label }}<em>{{ c.type }}</em>
                  </span>
                </div>
              </div>
            </div>
          </section>

          <section class="panel" v-if="detail">
            <div class="panel-head">
              <h3>质量与限制</h3>
              <span class="hint">error 阻断结论，warn/info 随答案展示</span>
            </div>
            <div v-if="quality.length" class="quality-list">
              <div v-for="q in quality" :key="q.id" class="quality" :class="q.severity">
                <span class="sev">{{ q.severity }}</span>
                <span class="q-title">{{ q.title }}</span>
                <span class="q-count">{{ q.count }}</span>
              </div>
            </div>
            <div v-else class="muted">未发现需要关注的质量问题。</div>
          </section>

          <section class="panel" v-if="detail">
            <div class="panel-head">
              <h3>口径文档（权威口径）</h3>
              <span class="hint">支持 md / txt / json / yaml；先解析预览，再应用</span>
            </div>
            <div class="doc-row">
              <input type="file" accept=".md,.markdown,.txt,.json,.yaml,.yml" @change="onDocFile" />
              <button class="ghost" :disabled="!docFile || docBusy" @click="parseDoc">
                {{ docBusy ? '处理中…' : '解析文档' }}
              </button>
              <button class="primary slim" :disabled="!documentation || docBusy" @click="applyDoc">
                应用到草稿
              </button>
            </div>
            <div v-if="documentation" class="doc-result">
              <div class="doc-meta">
                {{ documentation.source }} · {{ documentation.parser }} ·
                {{ documentation.rules?.length || 0 }} 条规则
              </div>
              <div class="doc-rules">
                <div v-for="(rule, idx) in documentation.rules || []" :key="idx" class="doc-rule">
                  <b>{{ rule.name }}</b>
                  <span class="agg">{{ rule.aggregation || '—' }}</span>
                  <span>{{ rule.table || '—' }}</span>
                  <span>{{ (rule.resolved_columns || []).map((c: any) => c.label).join(', ') }}</span>
                  <span v-if="rule.formula" class="formula">{{ rule.formula }}</span>
                  <span v-if="rule.filters && Object.keys(rule.filters).length" class="filter">
                    {{ Object.entries(rule.filters).map(([k, v]) => `${k}=${v}`).join(', ') }}
                  </span>
                </div>
              </div>
              <div v-if="documentation.warnings?.length" class="doc-warn">
                警告：{{ documentation.warnings.join('；') }}
              </div>
              <div v-if="documentation.notes?.length" class="doc-note">
                说明：{{ documentation.notes.join('；') }}
              </div>
            </div>
          </section>

          <section class="panel" v-if="draft">
            <div class="panel-head">
              <h3>语义草稿与发布</h3>
              <span class="hint">
                {{ draft.status === 'published' ? '已发布版本；可调整后重新发布' : '未确认口径只用于探索问答' }}
              </span>
            </div>

            <div class="draft-toolbar">
              <button class="ghost sm" @click="editMode = !editMode">
                {{ editMode ? '取消编辑' : '编辑名称/同义词' }}
              </button>
              <select v-if="editMode" v-model="timeColumnValue" class="text-input slim">
                <option value="">保持当前时间列</option>
                <option v-for="opt in timeOptions" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
              <button v-if="editMode" class="primary slim" @click="saveEdits">
                保存草稿
              </button>
              <label class="check-inline">
                <input v-model="showCandidates" type="checkbox" />
                显示未启用候选
              </label>
            </div>

            <div class="draft-cols">
              <div class="draft-block">
                <div class="block-title">
                  指标（已启用 {{ selectedMetrics.length }} / 共 {{ draft.metrics?.length || 0 }}）
                </div>
                <div class="muted" v-if="!showCandidates">
                  默认只显示已启用指标；勾选上方“显示未启用候选”查看其他事实表候选。
                </div>
                <div v-for="group in metricGroups" :key="group.fact" class="metric-group">
                  <div class="group-title">{{ group.label }} · {{ group.metrics.length }}</div>
                  <label v-for="m in group.metrics" :key="m.id" class="check-row">
                    <input
                      type="checkbox"
                      :checked="selectedMetrics.includes(m.id)"
                      @change="toggleMetric(m.id)"
                    />
                    <span class="check-main">
                      <template v-if="editMode && metricEdits[m.id]">
                        <input v-model="metricEdits[m.id].name_zh" class="inline-input" />
                        <input
                          v-model="metricEdits[m.id].synonyms"
                          class="inline-input small"
                          placeholder="同义词，用、分隔"
                        />
                      </template>
                      <template v-else>
                        <b>{{ m.name_zh || m.name }}</b>
                        <small>{{ m.aggregation || '—' }} · {{ m.id }}</small>
                      </template>
                    </span>
                    <span class="conf">{{ m.confidence ? pct(m.confidence) : '—' }}</span>
                  </label>
                </div>
                <div v-if="!metrics.length" class="muted">没有指标候选</div>
              </div>

              <div class="draft-block">
                <div class="block-title">
                  维度（已启用 {{ selectedDims.length }} / 共 {{ draft.dimensions?.length || 0 }}）
                </div>
                <div class="muted" v-if="!showCandidates">默认只显示已启用维度。</div>
                <label v-for="d in dimensions" :key="d.id" class="check-row">
                  <input
                    type="checkbox"
                    :checked="selectedDims.includes(d.id)"
                    @change="toggleDim(d.id)"
                  />
                  <span class="check-main">
                    <template v-if="editMode && dimEdits[d.id]">
                      <input v-model="dimEdits[d.id].name_zh" class="inline-input" />
                      <input
                        v-model="dimEdits[d.id].synonyms"
                        class="inline-input small"
                        placeholder="同义词，用、分隔"
                      />
                    </template>
                    <template v-else>
                      <b>{{ d.name_zh || d.id }}</b>
                      <small>{{ d.kind || 'categorical' }} · {{ d.id }}</small>
                    </template>
                  </span>
                </label>
                <div v-if="!dimensions.length" class="muted">没有维度候选</div>
              </div>
            </div>

            <div v-if="relations.length" class="relations">
              <div class="block-title">已接受的关系（N:1）</div>
              <div v-for="r in relations" :key="`${r.child}-${r.child_col}-${r.parent}`" class="relation">
                <b>{{ r.child }}.{{ r.child_col }}</b>
                <span>→</span>
                <b>{{ r.parent }}.{{ r.parent_col }}</b>
                <span class="muted">覆盖 {{ pct(r.containment) }} · 评分 {{ r.score }}</span>
              </div>
            </div>

            <div class="publish-row">
              <div class="muted">
                发布只会启用勾选的指标与维度；未勾选项保留在草稿中。
              </div>
              <button
                class="primary"
                :disabled="!selectedMetrics.length || publishing"
                @click="publish"
              >
                {{ publishing ? '发布中…' : isDraft ? '确认并发布' : '重新发布所选' }}
              </button>
            </div>
          </section>

          <section class="panel" v-if="!draft">
            <div class="panel-head">
              <h3>语义层</h3>
              <span class="hint">内置示例数据源，无自动草稿</span>
            </div>
            <div class="metric-list">
              <div v-for="m in detail.metrics" :key="m.id" class="metric-row">
                <b>{{ m.name }}</b>
                <span>{{ m.definition }}</span>
              </div>
            </div>
          </section>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.manage {
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
  background: var(--paper); color: var(--ink);
}
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 18px 26px; border-bottom: 1px solid var(--rule);
  background: linear-gradient(180deg, #FBF9F3, var(--panel));
  flex: 0 0 auto;
}
.brand { display: flex; align-items: center; gap: 11px; }
.mark {
  width: 31px; height: 31px; border-radius: 2px; background: var(--accent-2);
  color: #FBF5EC; display: grid; place-items: center; font-weight: 600;
}
.bname { font-size: 17px; font-weight: 600; letter-spacing: .04em; }
.bsub { font: 8.5px var(--mono); letter-spacing: .18em; color: var(--ink-3); margin-top: 4px; }
.top-actions { display: flex; gap: 10px; }
.layout {
  flex: 1 1 auto; min-height: 0; display: flex; align-items: stretch;
  --rail-w: 300px;
}
.rail {
  width: var(--rail-w); flex: 0 0 auto; min-height: 0; overflow-y: auto;
  border-right: 1px solid var(--rule); padding: 18px 14px; background: var(--panel);
}
.splitter {
  width: 7px; flex: 0 0 auto; cursor: col-resize; background: transparent;
  position: relative; transition: background .15s ease;
}
.splitter::after {
  content: ""; position: absolute; top: 0; bottom: 0; left: 3px; width: 1px;
  background: var(--rule-soft);
}
.splitter:hover, .splitter.dragging { background: var(--accent-soft); }
.splitter:hover::after, .splitter.dragging::after { background: var(--accent); }
.rail::-webkit-scrollbar, .main::-webkit-scrollbar { width: 8px; }
.rail::-webkit-scrollbar-thumb, .main::-webkit-scrollbar-thumb {
  background: var(--rule); border-radius: 4px;
}
.rail::-webkit-scrollbar-track, .main::-webkit-scrollbar-track { background: transparent; }
.rail-head {
  display: flex; justify-content: space-between; align-items: center;
  font: 9px var(--mono); letter-spacing: .16em; color: var(--ink-3);
  margin: 0 4px 10px;
}
.rail-list { display: flex; flex-direction: column; gap: 5px; }
.ds-item {
  display: flex; gap: 9px; align-items: flex-start; text-align: left;
  border: 1px solid transparent; background: transparent; color: var(--ink-2);
  padding: 10px 9px; border-radius: 3px; cursor: pointer;
}
.ds-item:hover { background: var(--card); border-color: var(--rule-soft); }
.ds-item.active { background: var(--card); border-color: var(--rule); }
.ds-dot { width: 6px; height: 6px; border-radius: 50%; margin-top: 5px; flex: 0 0 auto; }
.ds-dot.live { background: var(--accent); }
.ds-dot.draft { background: var(--amber); }
.ds-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ds-main b { font-size: 12.5px; color: var(--ink); font-weight: 600; }
.ds-main small { font: 9px var(--mono); color: var(--ink-3); letter-spacing: .04em; }
.rail-empty { font-size: 12px; color: var(--ink-3); padding: 14px 6px; }
.upload-card {
  margin-top: 20px; padding: 14px; border: 1px solid var(--rule);
  border-radius: 3px; background: var(--card);
}
.card-title { font: 9px var(--mono); letter-spacing: .14em; color: var(--ink-3); margin-bottom: 10px; }
.file-pick {
  display: block; border: 1px dashed var(--rule); border-radius: 3px;
  padding: 14px 10px; text-align: center; font-size: 11.5px; color: var(--ink-3);
  cursor: pointer; margin-bottom: 9px;
}
.file-pick:hover { border-color: var(--accent); color: var(--accent); }
.file-pick input { display: none; }
.file-list { font: 9px var(--mono); color: var(--ink-3); margin-bottom: 8px; word-break: break-all; }
.text-input {
  width: 100%; border: 1px solid var(--rule); background: var(--panel);
  color: var(--ink); padding: 8px 9px; font-size: 12px; border-radius: 2px;
  margin-bottom: 8px;
}
.primary {
  width: 100%; border: 1px solid var(--accent); background: var(--accent);
  color: #F7F4EC; padding: 8px 12px; border-radius: 2px; cursor: pointer;
  font-size: 12px; letter-spacing: .03em;
}
.primary:disabled { opacity: .5; cursor: default; }
.ghost {
  border: 1px solid var(--rule); background: transparent; color: var(--ink-2);
  padding: 6px 10px; border-radius: 2px; cursor: pointer; font-size: 11px;
  text-decoration: none;
}
.ghost:hover { border-color: var(--accent); color: var(--accent); }
.ghost.sm { padding: 3px 7px; font-size: 10px; }
.ghost.danger:hover { border-color: var(--up); color: var(--up); }
.job { margin-top: 12px; }
.job-line { display: flex; justify-content: space-between; font: 9px var(--mono); color: var(--ink-3); }
.bar { height: 3px; background: var(--rule-soft); margin: 5px 0; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); transition: width .3s ease; }
.job-msg { font-size: 10.5px; color: var(--ink-3); margin-bottom: 6px; }
.main {
  flex: 1 1 auto; min-width: 0; min-height: 0; overflow-y: auto;
  padding: 24px 30px 50px;
}
.banner { padding: 9px 12px; border-radius: 2px; font-size: 12px; margin-bottom: 14px; }
.banner.error { background: rgba(178,59,43,.08); color: var(--up); border: 1px solid rgba(178,59,43,.25); }
.banner.ok { background: rgba(23,94,88,.08); color: var(--accent); border: 1px solid rgba(23,94,88,.25); }
.empty { padding: 80px 20px; text-align: center; color: var(--ink-3); }
.empty h2 { font-family: var(--serif); font-weight: 500; color: var(--ink-2); }
.hero { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; margin-bottom: 20px; }
.eyebrow { font: 8.5px var(--mono); letter-spacing: .2em; color: var(--ink-3); margin-bottom: 7px; }
.hero h1 { font-family: var(--serif); font-size: 30px; font-weight: 500; margin: 0 0 9px; letter-spacing: .01em; }
.hero-meta { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; font: 9.5px var(--mono); color: var(--ink-3); }
.pill { border: 1px solid var(--rule); padding: 2px 7px; border-radius: 2px; }
.pill.draft { color: var(--amber); border-color: rgba(156,108,24,.4); background: rgba(156,108,24,.08); }
.pill.live { color: var(--accent); border-color: rgba(23,94,88,.3); background: rgba(23,94,88,.06); }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 18px; }
.stat { border: 1px solid var(--rule); background: var(--card); border-radius: 3px; padding: 12px 14px; }
.stat small { display: block; font: 8.5px var(--mono); letter-spacing: .1em; color: var(--ink-3); margin-bottom: 6px; }
.stat b { font-family: var(--serif); font-size: 21px; font-weight: 500; }
.stat b.small { font-size: 12px; font-family: var(--mono); }
.panel { border: 1px solid var(--rule); background: var(--card); border-radius: 3px; padding: 18px 20px; margin-bottom: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.panel-head h3 { margin: 0; font-family: var(--serif); font-size: 18px; font-weight: 500; }
.hint { font: 9px var(--mono); color: var(--ink-3); letter-spacing: .04em; }
.summary { font-size: 13px; line-height: 1.8; color: var(--ink-2); margin: 0 0 14px; }
.table-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
.table-card { border: 1px solid var(--rule-soft); border-radius: 3px; padding: 11px 12px; background: var(--panel); }
.table-top { display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 5px; }
.table-sub { font: 9px var(--mono); color: var(--ink-3); margin-bottom: 8px; }
.cols { display: flex; flex-wrap: wrap; gap: 4px 5px; }
.col { font-size: 10px; color: var(--ink-2); border: 1px solid var(--rule-soft); padding: 1px 5px; border-radius: 2px; }
.col em { font: 8px var(--mono); font-style: normal; color: var(--ink-3); margin-left: 4px; }
.quality-list { display: flex; flex-direction: column; gap: 5px; }
.quality { display: flex; align-items: center; gap: 10px; font-size: 11.5px; padding: 6px 9px; border: 1px solid var(--rule-soft); border-radius: 2px; }
.quality .sev { font: 8px var(--mono); letter-spacing: .08em; padding: 1px 5px; border-radius: 2px; }
.quality.error .sev { color: var(--up); background: rgba(178,59,43,.1); }
.quality.warn .sev { color: var(--amber); background: rgba(156,108,24,.1); }
.quality.info .sev { color: var(--ink-3); background: var(--rule-soft); }
.q-title { flex: 1; }
.q-count { font: 10px var(--mono); color: var(--ink-3); }
.draft-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.block-title { font: 9px var(--mono); letter-spacing: .1em; color: var(--ink-3); margin-bottom: 7px; }
.check-row { display: flex; gap: 9px; align-items: center; padding: 6px 4px; border-bottom: 1px solid var(--rule-soft); cursor: pointer; }
.check-main { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.check-main b { font-size: 12px; font-weight: 500; }
.check-main small { font: 8.5px var(--mono); color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; }
.conf { font: 9px var(--mono); color: var(--ink-3); }
.relations { margin-top: 14px; }
.relation { display: flex; gap: 8px; align-items: center; font: 10px var(--mono); color: var(--ink-2); padding: 4px 0; }
.relation .muted { color: var(--ink-3); }
.publish-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--rule-soft); }
.publish-row .primary { width: auto; padding: 9px 18px; }
.muted { font-size: 11px; color: var(--ink-3); }
.metric-list { display: flex; flex-direction: column; gap: 6px; }
.metric-row { display: flex; gap: 14px; font-size: 11.5px; padding: 6px 0; border-bottom: 1px solid var(--rule-soft); }
.metric-row b { min-width: 90px; font-weight: 500; }
.metric-row span { color: var(--ink-3); }
.file-row {
  display: flex; align-items: center; gap: 6px; font-size: 10.5px;
  color: var(--ink-2); padding: 2px 0; cursor: pointer;
}
.file-row em {
  font-style: normal; font: 8px var(--mono); color: var(--amber);
  border: 1px solid rgba(156,108,24,.4); padding: 0 4px; border-radius: 2px;
}
.policy-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 8px; }
.policy-row label {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 10.5px; color: var(--ink-3);
}
.policy-row select {
  border: 1px solid var(--rule); background: var(--panel); color: var(--ink-2);
  font-size: 10px; padding: 3px 5px; border-radius: 2px;
}
.doc-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.doc-row input[type="file"] { font-size: 10.5px; color: var(--ink-3); max-width: 100%; }
.primary.slim { width: auto; padding: 6px 12px; font-size: 11px; }
.text-input.slim { width: auto; margin: 0; font-size: 10.5px; padding: 5px 7px; }
.doc-result { margin-top: 10px; }
.doc-meta { font: 9px var(--mono); color: var(--ink-3); margin-bottom: 7px; }
.doc-rules { display: flex; flex-direction: column; gap: 4px; }
.doc-rule {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  font-size: 11px; padding: 5px 8px; border: 1px solid var(--rule-soft);
  border-radius: 2px; background: var(--panel);
}
.doc-rule b { font-weight: 600; }
.doc-rule .agg { font: 8.5px var(--mono); color: var(--accent); }
.doc-rule .formula, .doc-rule .filter {
  font: 9px var(--mono); color: var(--amber);
}
.doc-warn { margin-top: 7px; font-size: 10.5px; color: var(--amber); }
.doc-note { margin-top: 4px; font-size: 10.5px; color: var(--ink-3); }
.draft-toolbar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-bottom: 10px;
}
.inline-input {
  width: 100%; border: 1px solid var(--rule); background: var(--panel);
  color: var(--ink); font-size: 11.5px; padding: 3px 5px; border-radius: 2px;
}
.inline-input.small { font-size: 10px; color: var(--ink-3); }
.metric-group { margin-top: 10px; }
.group-title {
  font: 9px var(--mono); letter-spacing: .08em; color: var(--accent);
  border-bottom: 1px solid var(--rule-soft); padding: 4px 2px; margin-bottom: 3px;
}
.check-inline {
  display: flex; align-items: center; gap: 5px;
  font-size: 10.5px; color: var(--ink-3); cursor: pointer;
}
.cap-row { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
.cap-chip {
  font: 8.5px var(--mono); color: var(--accent); background: var(--accent-soft);
  border: 1px solid rgba(23,94,88,.2); padding: 2px 6px; border-radius: 2px;
}

@media (max-width: 1000px) {
  .layout { flex-direction: column; overflow-y: auto; }
  .rail {
    width: 100%; max-height: 42vh; border-right: none;
    border-bottom: 1px solid var(--rule);
  }
  .splitter { display: none; }
  .main { flex: 0 0 auto; overflow: visible; min-height: auto; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .draft-cols { grid-template-columns: 1fr; }
}
</style>
