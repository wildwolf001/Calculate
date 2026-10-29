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
  dataset_id?: string | null
  updated_at: string
}

export interface DatasetSummary {
  dataset_id: string
  name: string
  status: string
  rows?: number | null
  tables?: number | null
  semantic_version?: number | null
  data_version?: string | null
  updated_at?: string | null
  readiness?: string | null
}

export interface UploadResult {
  upload_id: string
  files: { name: string; size: number; sha256: string }[]
}

export interface AnalyzeStart {
  job_id: string
  dataset_id: string
  name: string
}

export interface JobStatus {
  job_id: string
  kind: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  stage: string
  percent: number
  message: string
  error?: string | null
  result?: any
  params?: any
  created_at: string
  updated_at: string
}

export interface HistoryItem {
  result_id: string
  question?: string
  ts: string
  row_count: number
  columns?: string[]
  seq?: number
  turn?: number
  kind?: 'query' | 'subquestion' | 'interpretation' | 'analysis' | string
  label?: string
  analysis_section?: string
  metric?: string
  dimensions?: string[]
  time?: { start: string; end: string }
  comparison?: { label?: string; current?: any; baseline?: any } | null
  filters?: Record<string, any>
  limit?: number
  degraded_reason?: string | null
  plan?: PlanSummary
  sql?: string
  params?: string[]
  fingerprint?: string
  duration_ms?: number
  truncated?: boolean
  warehouse_version?: string | null
  preview?: any[][]
  checks?: CheckIssue[]
  facts?: Record<string, any>
  blocked?: boolean
  status?: string
  answer?: string
}

export interface VerifyResult {
  result_id?: string
  verdict: 'reproduced' | 'data_changed' | 'query_changed' | 'error' | 'unavailable'
  message?: string
  stored?: {
    fingerprint?: string
    sql?: string
    row_count?: number
    truncated?: boolean
    result_hash?: string
    warehouse_version?: string | null
  }
  current?: {
    fingerprint?: string
    sql?: string
    row_count?: number
    truncated?: boolean
    columns?: string[]
    result_hash?: string
    warehouse_version?: string | null
    duration_ms?: number
  }
  checks?: {
    sql_match?: boolean
    fingerprint_match?: boolean
    columns_match?: boolean
    rows_match?: boolean
  }
  warehouse_changed?: boolean
}

export interface HistoryResponse {
  history: HistoryItem[]
  dataset?: string
  semantic_version?: number
  compiler_version?: number
  warehouse_version?: string
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

export interface AnalysisModule {
  id: string
  label: string
  section: string
  description: string
  default: boolean
}

export interface AnalysisOptions {
  sections: string[]
  customText?: string
  customTitle?: string
}

export interface AnalysisHints {
  categories: string[]
  products: string[]
  sellers: string[]
  time_range: string[]
}

export interface AnalysisSection {
  id: string
  section: string
  title: string
  result_id: string
  insights: string[]
}

export interface AnalysisReport {
  report: string
  summary: string
  result_id: string | null
  sections: AnalysisSection[]
  failures?: { id: string; section: string; title: string; reason: string }[]
  status?: string
  writer?: 'llm' | 'template'
  core_conclusions?: string[]
  watchouts?: string[]
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
  dataset_id?: string
  dataset: string
  mode?: 'draft' | 'published'
  data_version?: string
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
  semanticStatus?: 'draft' | 'published'
  repairs?: { action: string; attempt?: number; detail?: any }[]
  refusals?: any[]
  diagnoses?: any[]
  dataWarnings?: string[]
}
