export interface ApiEvent {
  seq: number
  session_id: string
  ts: string
  type: string
  payload: any
  caused_by?: string | null
  trace_id?: string | null
}

export interface SessionSummary {
  session_id: string
  title: string
  turns: number
  updated_at: string
}

export interface HistoryItem {
  result_id: string
  question?: string
  ts: string
  row_count: number
  columns?: string[]
  seq?: number
}

export interface CheckIssue {
  id: string
  severity: 'error' | 'warn' | 'info'
  title: string
  detail?: string
}

export interface ResultArtifact {
  result_id: string
  columns: string[]
  column_types: string[]
  rows: any[][]
  row_count: number
  truncated: boolean
  sql: string
  params: string[]
  fingerprint: string
  duration_ms: number
  question?: string
  meta?: Record<string, any>
}

export interface PlanSummary {
  metric: string
  dimensions: string[]
  time?: { start: string; end: string }
  filters?: Record<string, any>
  compare?: { type: string } | null
  comparison?: { label?: string; current?: any; baseline?: any } | null
  having?: any[]
  limit?: number
  visualization?: { type?: string; x?: string; y?: string; reason?: string } | null
}

export interface MetricMeta {
  id: string
  name: string
  definition?: string
  unit?: string
  type?: string
  supported_grain?: string[]
}

export interface Meta {
  dataset: string
  semantic_version: number
  compiler_version: number
  time_range: [string, string]
  tables: Record<string, number>
  metrics: MetricMeta[]
  dimensions: { id: string; name: string }[]
  quality: { id: string; severity: string; title: string; count: number }[]
  llm: { enabled: boolean; model?: string | null; base_url?: string | null }
  limits: { max_rows: number }
}

export interface InterpretationEntry {
  id: string
  label?: string
  preferred: boolean
  result: ResultArtifact
  checks: CheckIssue[]
  facts: Record<string, any>
  plan: PlanSummary | null
}

export interface NavSnapshot {
  label: string
  question?: string
  result: ResultArtifact
  checks: CheckIssue[]
  facts: Record<string, any>
  sql: string
  plan: PlanSummary | null
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  text: string
  kind?: 'normal' | 'error' | 'clarify'
  options?: string[]
  trace?: string[]
}
