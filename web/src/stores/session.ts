import { defineStore } from 'pinia'
import {
  createSession,
  deleteSession,
  exportUrl,
  getAnalysisHints,
  getAnalysisModules,
  getHistory,
  getMeta,
  getResult,
  getSessionEvents,
  listDatasets,
  listSessions,
  streamAnalysis,
  streamTurn,
  verifyRecord,
} from '../api'
import type {
  AnalysisHints,
  AnalysisModule,
  AnalysisOptions,
  AnalysisReport,
  ApiEvent,
  ChatMessage,
  CheckIssue,
  DatasetSummary,
  HistoryItem,
  HistoryResponse,
  InterpretationEntry,
  Meta,
  NavSnapshot,
  PlanSummary,
  ResultArtifact,
  SessionSummary,
  VerifyResult,
} from '../types'

const GREETING =
  '你好，我是明账。已连接只读电商数仓，可以问订单、销售额、品类、商家、履约和评价类问题。'
const NO_DATA_GREETING =
  '还没有可用的数据源。请先在「数据管理」页导入 CSV/Excel，确认口径并发布，然后回来提问。'

const SUGGESTIONS = [
  '2024年1月至2025年12月的商品销售额',
  '4月商品销售额最高的品类是什么？',
  '5月已送达订单不少于10笔的商家中，延迟率最高的是谁？',
  '延迟订单与准时订单的平均评分分别是多少？',
  '有多少自然客户完成了至少2笔已送达订单？',
  '商品销售额的口径是什么？',
]

const STAGE_LABELS: Record<string, string> = {
  'turn.started': '理解问题',
  'user.message': '理解问题',
  'query.normalized': '理解问题',
  'intent.classified': '解析意图',
  'intent.parsed': '解析意图',
  'values.grounded': '校准实体值',
  'plan.compiled': '生成查询计划',
  'sql.compiled': '编译 SQL',
  'sql.executed': '执行完成',
  'checks.completed': '业务校验',
  'analysis.requested': '生成经营分析',
  'analysis.section': '分析板块',
  'analysis.completed': '分析完成',
  'answer.composed': '生成回答',
  'llm.retry': '模型重试',
}

interface State {
  sessionId: string
  datasetId: string
  datasets: DatasetSummary[]
  switching: boolean
  switchSeq: number
  status: string
  busy: boolean
  busySince: number
  stage: string
  messages: ChatMessage[]
  assistantBaseline: number
  trace: string[]
  lastTrace: string[]
  result: ResultArtifact | null
  results: InterpretationEntry[]
  hasSubquestions: boolean
  analysisMode: boolean
  analysis: AnalysisReport | null
  analysisModules: AnalysisModule[]
  analysisHints: AnalysisHints | null
  analysisDialogOpen: boolean
  activeResultId: string
  navStack: NavSnapshot[]
  chartType: 'auto' | 'line' | 'bar' | 'pie'
  plan: PlanSummary | null
  checks: CheckIssue[]
  facts: Record<string, any>
  sql: string
  pending: any | null
  history: HistoryItem[]
  historyMeta: HistoryResponse | null
  historyOpen: boolean
  recordOpen: boolean
  activeRecord: HistoryItem | null
  verify: VerifyResult | null
  verifying: boolean
  meta: Meta | null
  tab: 'table' | 'chart' | 'sql' | 'analysis'
  toast: string
  messageSeq: number
  sessions: SessionSummary[]
  drawerOpen: boolean
}

export const useSessionStore = defineStore('session', {
  state: (): State => ({
    sessionId: '',
    datasetId: '',
    datasets: [],
    switching: false,
    switchSeq: 0,
    status: 'IDLE',
    busy: false,
    busySince: 0,
    stage: '',
    messages: [],
    assistantBaseline: 0,
    trace: [],
    lastTrace: [],
    result: null,
    results: [],
    hasSubquestions: false,
    analysisMode: false,
    analysis: null,
    analysisModules: [],
    analysisHints: null,
    analysisDialogOpen: false,
    activeResultId: '',
    navStack: [],
    chartType: 'auto',
    plan: null,
    checks: [],
    facts: {},
    sql: '',
    pending: null,
    history: [],
    historyMeta: null,
    historyOpen: false,
    recordOpen: false,
    activeRecord: null,
    verify: null,
    verifying: false,
    meta: null,
    tab: 'table',
    toast: '',
    messageSeq: 0,
    sessions: [],
    drawerOpen: false,
  }),
  getters: {
    suggestions: (s) => {
      if (!s.datasetId || !s.meta) return []
      const demo = s.meta.dataset_id === 'ds_ecommerce_demo'
        || s.meta.dataset === 'ecommerce_agent_demo'
      if (demo) return SUGGESTIONS
      return (s.meta.metrics || []).slice(0, 4).map((m) => `${m.name}是多少？`)
    },
    greeting: (s) => {
      if (!s.datasetId || !s.meta) return NO_DATA_GREETING
      const demo = s.meta.dataset_id === 'ds_ecommerce_demo'
        || s.meta.dataset === 'ecommerce_agent_demo'
      if (demo) return GREETING
      const mode = s.meta.mode === 'draft' ? '自动推断口径，尚未经业务确认' : '口径已确认'
      return `你好，我是明账。当前数据源「${s.meta.dataset || s.datasetId}」，包含 ${s.meta.metrics.length} 个指标（${mode}）。可以问计数、合计、平均、按维度分组等基础问题。`
    },
    hasResult: (s) => !!s.result,
    blocks: (s) => s.checks.filter((c) => c.severity === 'error'),
    activePlan: (s): PlanSummary | null =>
      s.results.find((r) => r.id === s.activeResultId)?.plan || s.plan,
  },
  actions: {
    async init() {
      try {
        await this.refreshDatasets()
        if (!this.datasetId) {
          this.meta = null
          this.sessionId = ''
          this.messages = []
          this.addAssistant(NO_DATA_GREETING)
          return
        }
        this.meta = await getMeta(this.datasetId)
        this.sessionId = await createSession(this.datasetId)
        await this.refreshHistory()
        await this.loadSessions()
        this.addAssistant(this.greeting)
      } catch (err: any) {
        this.toast = `初始化失败：${err.message || err}`
      }
    },
    async refreshDatasets() {
      try {
        const data = await listDatasets()
        this.datasets = data.datasets
        if (!this.datasetId) {
          this.datasetId = data.default || data.datasets[0]?.dataset_id || ''
        }
      } catch {
        /* non-critical */
      }
    },
    async switchDataset(datasetId: string) {
      if (this.busy || this.switching || !datasetId || datasetId === this.datasetId) return
      const sequence = ++this.switchSeq
      this.switching = true
      this.setToast('正在切换数据源…')
      try {
        // Resolve a fully usable target context before changing any active UI
        // state. A failed switch leaves the previous dataset/session intact.
        const nextMeta = await getMeta(datasetId)
        const nextSessionId = await createSession(datasetId)
        if (sequence !== this.switchSeq) return
        this.datasetId = datasetId
        this.meta = nextMeta
        this.resetConversation()
        this.sessionId = nextSessionId
        this.addAssistant(this.greeting)
        await this.refreshHistory()
        await this.loadSessions()
        this.setToast(`已切换数据源：${this.meta.dataset}`)
      } catch (err: any) {
        this.setToast(`切换数据源失败：${err.message || err}`)
      } finally {
        if (sequence === this.switchSeq) this.switching = false
      }
    },
    resetConversation() {
      this.status = 'IDLE'
      this.busy = false
      this.busySince = 0
      this.stage = ''
      this.messages = []
      this.assistantBaseline = 0
      this.trace = []
      this.lastTrace = []
      this.result = null
      this.results = []
      this.hasSubquestions = false
      this.analysisMode = false
      this.analysis = null
      this.analysisDialogOpen = false
      this.analysisModules = []
      this.analysisHints = null
      this.activeResultId = ''
      this.navStack = []
      this.chartType = 'auto'
      this.plan = null
      this.checks = []
      this.facts = {}
      this.sql = ''
      this.pending = null
      this.history = []
      this.historyMeta = null
      this.historyOpen = false
      this.recordOpen = false
      this.activeRecord = null
      this.verify = null
      this.verifying = false
      this.tab = 'table'
      this.messageSeq = 0
    },
    async loadSessions() {
      try {
        this.sessions = await listSessions(this.datasetId || undefined)
      } catch {
        /* the conversation list is non-critical */
      }
    },
    toggleDrawer() {
      this.drawerOpen = !this.drawerOpen
      if (this.drawerOpen) this.loadSessions()
    },
    async newChat() {
      if (this.busy || this.switching) return
      if (!this.datasetId) {
        this.setToast('请先在数据管理页导入数据源')
        return
      }
      try {
        const sid = await createSession(this.datasetId)
        this.resetConversation()
        this.sessionId = sid
        this.addAssistant(this.greeting)
        this.drawerOpen = false
        await this.loadSessions()
        await this.refreshHistory()
      } catch (err: any) {
        this.setToast(`新建对话失败：${err.message || err}`)
      }
    },
    async loadSession(sessionId: string) {
      if (this.busy || this.switching || sessionId === this.sessionId) {
        this.drawerOpen = false
        return
      }
      try {
        const events = await getSessionEvents(sessionId)
        const created = events.find((ev) => ev.type === 'session.created')
        const datasetId = created?.payload?.dataset_id
        let nextMeta = this.meta
        if (datasetId) {
          try {
            nextMeta = await getMeta(datasetId)
          } catch (err: any) {
            this.setToast(`历史会话的数据源不可用：${err.message || err}`)
            return
          }
        }
        if (datasetId) this.datasetId = datasetId
        this.meta = nextMeta
        this.resetConversation()
        this.sessionId = sessionId
        for (const ev of events) {
          if (ev.type === 'user.message') {
            this.addUser((ev.payload || {}).text || '')
          } else {
            if (ev.type === 'turn.started') {
              this.assistantBaseline = this.messages.filter(
                (m) => m.role === 'assistant',
              ).length
            }
            this.applyEvent(ev)
          }
        }
        this.busy = false
        this.stage = ''
        this.drawerOpen = false
        await this.refreshHistory()
        this.setToast('已载入历史对话')
      } catch (err: any) {
        this.setToast(`载入对话失败：${err.message || err}`)
      }
    },
    async deleteChat(sessionId: string) {
      if (this.busy) return
      try {
        await deleteSession(sessionId)
        await this.loadSessions()
        if (sessionId === this.sessionId) await this.newChat()
      } catch (err: any) {
        this.setToast(`删除失败：${err.message || err}`)
      }
    },
    addUser(text: string) {
      this.messages.push({ id: ++this.messageSeq, role: 'user', text })
    },
    addAssistant(text: string, kind: ChatMessage['kind'] = 'normal', options?: string[]) {
      this.messages.push({ id: ++this.messageSeq, role: 'assistant', text, kind, options })
      return this.messages[this.messages.length - 1]
    },
    setToast(text: string) {
      this.toast = text
      window.setTimeout(() => {
        if (this.toast === text) this.toast = ''
      }, 2600)
    },
    async ask(text: string) {
      const content = text.trim()
      if (!this.datasetId) {
        this.setToast('请先在数据管理页导入数据源')
        return
      }
      if (!content || this.busy || this.switching || !this.sessionId) return
      this.addUser(content)
      this.busy = true
      this.busySince = Date.now()
      this.stage = '理解问题'
      this.assistantBaseline = this.messages.filter((m) => m.role === 'assistant').length
      this.trace = []
      this.pending = null
      try {
        for await (const ev of streamTurn(this.sessionId, content)) {
          this.applyEvent(ev)
        }
      } catch (err: any) {
        this.addAssistant(`请求失败：${err.message || err}`, 'error')
      } finally {
        this.busy = false
        this.stage = ''
        this.attachTrace()
        this.loadSessions()
      }
    },
    async openAnalysisDialog() {
      if (this.busy || !this.sessionId) return
      if (!this.analysisModules.length) {
        try {
          this.analysisModules = await getAnalysisModules(this.datasetId)
        } catch (err: any) {
          this.setToast(`加载分析模块失败：${err.message || err}`)
          return
        }
      }
      if (!this.analysisHints) {
        try {
          this.analysisHints = await getAnalysisHints(this.datasetId)
        } catch {
          /* hints are optional */
        }
      }
      this.analysisDialogOpen = true
    },
    closeAnalysisDialog() {
      this.analysisDialogOpen = false
    },
    async runAnalysis(options: AnalysisOptions) {
      if (this.busy || !this.sessionId) return
      const labels = (options.sections || []).map(
        (id) => this.analysisModules.find((item) => item.id === id)?.label || id,
      )
      let content = '生成经营分析'
      if (labels.length) content += `：${labels.join('、')}`
      const custom = (options.customText || '').trim()
      if (custom) content += `\n自定义分析：${custom}`

      this.addUser(content)
      this.busy = true
      this.busySince = Date.now()
      this.stage = '生成经营分析'
      this.assistantBaseline = this.messages.filter(
        (message) => message.role === 'assistant',
      ).length
      this.trace = []
      this.pending = null
      this.analysisDialogOpen = false
      try {
        for await (const event of streamAnalysis(this.sessionId, options)) {
          this.applyEvent(event)
        }
      } catch (err: any) {
        this.addAssistant(`请求失败：${err.message || err}`, 'error')
      } finally {
        this.busy = false
        this.stage = ''
        this.attachTrace()
        this.loadSessions()
      }
    },
    applyEvent(ev: ApiEvent) {
      const p = ev.payload || {}
      if (ev.type !== 'open' && ev.type !== 'done') {
        this.trace.push(ev.type)
        if (STAGE_LABELS[ev.type]) this.stage = STAGE_LABELS[ev.type]
      }
      switch (ev.type) {
        case 'turn.started':
          this.plan = null
          this.result = null
          this.results = []
          this.hasSubquestions = false
          this.analysisMode = false
          this.activeResultId = ''
          this.sql = ''
          this.checks = []
          this.facts = {}
          break
        case 'subquestions.planned':
          this.hasSubquestions = (p.items || p.questions || []).length > 1
          break
        case 'analysis.requested':
          this.analysisMode = true
          break
        case 'analysis.completed':
          this.analysis = { ...(p as AnalysisReport), status: 'done' }
          this.tab = 'analysis'
          break
        case 'plan.compiled':
          this.plan = p.plan as PlanSummary
          break
        case 'sql.compiled':
          this.sql = p.sql || this.sql
          break
        case 'sql.executed': {
          const artifact = p as ResultArtifact
          const entry: InterpretationEntry = {
            id: p.interpretation_id || 'default',
            label: p.interpretation_label || undefined,
            preferred: !!p.preferred,
            result: artifact,
            checks: [],
            facts: {},
            plan: (p.plan as PlanSummary) || null,
          }
          const idx = this.results.findIndex((r) => r.id === entry.id)
          if (idx >= 0) this.results[idx] = entry
          else this.results.push(entry)
          this.sql = p.sql || this.sql
          if (!this.activeResultId || entry.preferred || this.results.length === 1) {
            this.selectResult(entry.id)
          }
          if (!p.analysis_section) this.tab = 'table'
          break
        }
        case 'checks.completed': {
          const iid = p.interpretation_id || 'default'
          const entry = this.results.find((r) => r.id === iid)
          if (entry) {
            entry.checks = (p.issues || []) as CheckIssue[]
            entry.facts = p.facts || {}
          }
          if (!p.interpretation_id || iid === this.activeResultId) {
            this.checks = (p.issues || []) as CheckIssue[]
            this.facts = p.facts || {}
          }
          break
        }
        case 'answer.composed': {
          const message = this.addAssistant(p.text || '', 'normal', p.options || undefined)
          if (p.semantic_status === 'draft') message.semanticStatus = 'draft'
          break
        }
        case 'ambiguity.detected':
          this.pending = p
          this.addAssistant(p.message || '需要确认一下。', 'clarify', p.candidates || [])
          break
        case 'error.raised':
          this.addAssistant(
            [p.message, p.hint].filter(Boolean).join('\n') || '查询失败。',
            'error',
          )
          break
        case 'stream.error':
          this.addAssistant(p.message || '流式连接中断。', 'error')
          break
        case 'turn.completed': {
          this.status = p.status || this.status
          this.lastTrace = [...this.trace]
          if (this.result) {
            const lastUser = this.messages.filter((m) => m.role === 'user').at(-1)?.text
            const snap: NavSnapshot = {
              label: this.result.question || lastUser || '结果',
              question: this.result.question,
              result: this.result,
              checks: [...this.checks],
              facts: { ...this.facts },
              sql: this.sql,
              plan: this.activePlan,
            }
            const last = this.navStack[this.navStack.length - 1]
            if (!last || last.result.result_id !== snap.result.result_id) {
              this.navStack.push(snap)
              if (this.navStack.length > 8) this.navStack.shift()
            }
          }
          const assistantCount = this.messages.filter((m) => m.role === 'assistant').length
          if (this.status === 'FAILED' && assistantCount === this.assistantBaseline) {
            this.addAssistant('这一轮没有完成：没有找到可执行的查询。换一种问法或补充指标/时间后重试。', 'error')
          }
          break
        }
        default:
          break
      }
    },
    attachTrace() {
      const list = this.lastTrace.length ? this.lastTrace : this.trace
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        if (this.messages[i].role === 'assistant') {
          this.messages[i].trace = list
          break
        }
      }
    },
    selectResult(id: string) {
      const entry = this.results.find((r) => r.id === id)
      if (!entry) return
      this.activeResultId = id
      this.result = entry.result
      this.checks = entry.checks
      this.facts = entry.facts
      this.sql = entry.result.sql || this.sql
      if (entry.plan) this.plan = entry.plan
    },
    restoreNav(index: number) {
      const snap = this.navStack[index]
      if (!snap) return
      this.result = snap.result
      this.checks = snap.checks
      this.facts = snap.facts
      this.sql = snap.sql
      this.plan = snap.plan
      this.results = []
      this.activeResultId = ''
    },
    setChartType(t: 'auto' | 'line' | 'bar' | 'pie') {
      this.chartType = t
    },
    submitClarification(value: string) {
      if (!value) return
      this.pending = null
      this.ask(value)
    },
    async refreshHistory() {
      if (!this.sessionId) return
      try {
        const data = await getHistory(this.sessionId)
        this.history = data.history
        this.historyMeta = data
      } catch {
        /* history is non-critical */
      }
    },
    async toggleHistory() {
      this.historyOpen = !this.historyOpen
      if (this.historyOpen) await this.refreshHistory()
    },
    openRecord(item: HistoryItem) {
      this.activeRecord = item
      this.recordOpen = true
      this.verify = null
      this.historyOpen = false
    },
    closeRecord() {
      this.recordOpen = false
      this.verify = null
      this.verifying = false
    },
    async runVerify() {
      const item = this.activeRecord
      if (!item || this.verifying) return
      this.verifying = true
      this.verify = null
      try {
        this.verify = await verifyRecord(this.sessionId, item.result_id)
      } catch (err: any) {
        this.setToast(`重跑验证失败：${err.message || err}`)
      } finally {
        this.verifying = false
      }
    },
    async loadHistoryItem(item: HistoryItem) {
      try {
        const artifact = await getResult(this.sessionId, item.result_id)
        this.result = artifact
        this.results = []
        this.activeResultId = ''
        this.sql = artifact.sql || ''
        this.plan = item.plan || null
        this.checks = (item.checks || []) as CheckIssue[]
        this.facts = item.facts || {}
        this.historyOpen = false
        this.recordOpen = false
        this.tab = 'table'
        this.setToast('已载入历史结果')
      } catch (err: any) {
        this.setToast(`载入失败：${err.message || err}`)
      }
    },
    drillRow(row: any[]) {
      if (!this.result || !this.plan || !this.plan.dimensions?.length) return
      const dim = this.plan.dimensions[0]
      const next: Record<string, string> = {
        category: '商家',
        seller: '品类',
        product: '商家',
        state: '城市',
      }
      const target = next[dim]
      if (!target) return
      const name = String(row[0])
      this.ask(`只看${name}，按${target}拆`)
    },
    setTab(tab: 'table' | 'chart' | 'sql' | 'analysis') {
      this.tab = tab
    },
    exportCurrent() {
      if (!this.result?.result_id) {
        this.setToast('还没有可导出的结果')
        return
      }
      window.open(exportUrl(this.sessionId, this.result.result_id), '_blank')
    },
  },
})
