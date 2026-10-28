import type {
  AnalysisHints,
  AnalysisModule,
  AnalysisOptions,
  AnalyzeStart,
  ApiEvent,
  DatasetSummary,
  HistoryItem,
  HistoryResponse,
  JobStatus,
  Meta,
  ResultArtifact,
  SessionSummary,
  UploadResult,
  VerifyResult,
} from './types'

const BASE = (import.meta as any).env?.VITE_API_BASE || ''

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`${resp.status}: ${text}`)
  }
  return resp.json() as Promise<T>
}

export async function createSession(datasetId?: string): Promise<string> {
  const resp = await fetch(`${BASE}/api/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dataset_id: datasetId || null }),
  })
  const data = await json<{ session_id: string }>(resp)
  return data.session_id
}

export async function getMeta(datasetId?: string): Promise<Meta> {
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ''
  return json<Meta>(await fetch(`${BASE}/api/meta${query}`))
}

export async function listDatasets(): Promise<{ datasets: DatasetSummary[]; default: string }> {
  return json<{ datasets: DatasetSummary[]; default: string }>(
    await fetch(`${BASE}/api/datasets`),
  )
}

export async function getDataset(datasetId: string): Promise<any> {
  return json<any>(await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}`))
}

export async function getDatasetReadiness(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/readiness`),
  )
}

export async function getDatasetProfile(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/profile`),
  )
}

export async function getDatasetDraft(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/draft`),
  )
}

export async function publishDataset(
  datasetId: string,
  metrics?: string[],
  dimensions?: string[],
): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/publish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ metrics: metrics ?? null, dimensions: dimensions ?? null }),
    }),
  )
}

export async function deleteDataset(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}`, {
      method: 'DELETE',
    }),
  )
}

export async function createUpload(files: File[]): Promise<UploadResult> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  return json<UploadResult>(
    await fetch(`${BASE}/api/uploads`, { method: 'POST', body: form }),
  )
}

export async function analyzeUpload(
  uploadId: string,
  name?: string,
  options: Record<string, any> = {},
): Promise<AnalyzeStart> {
  return json<AnalyzeStart>(
    await fetch(`${BASE}/api/uploads/${encodeURIComponent(uploadId)}/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name || null, ...options }),
    }),
  )
}

export async function rebuildDataset(
  datasetId: string,
  options: Record<string, any> = {},
): Promise<{ job_id: string; dataset_id: string; backup?: string }> {
  return json(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/rebuild`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options),
    }),
  )
}

export async function editDatasetDraft(
  datasetId: string,
  payload: Record<string, any>,
): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/draft`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  )
}

export async function uploadDocumentation(datasetId: string, file: File): Promise<any> {
  const form = new FormData()
  form.append('file', file)
  return json<any>(
    await fetch(
      `${BASE}/api/datasets/${encodeURIComponent(datasetId)}/documentation`,
      { method: 'POST', body: form },
    ),
  )
}

export async function getDocumentation(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/datasets/${encodeURIComponent(datasetId)}/documentation`),
  )
}

export async function applyDocumentation(datasetId: string): Promise<any> {
  return json<any>(
    await fetch(
      `${BASE}/api/datasets/${encodeURIComponent(datasetId)}/documentation/apply`,
      { method: 'POST' },
    ),
  )
}

export async function getJob(jobId: string): Promise<JobStatus> {
  return json<JobStatus>(
    await fetch(`${BASE}/api/jobs/${encodeURIComponent(jobId)}`),
  )
}

export async function cancelJob(jobId: string): Promise<any> {
  return json<any>(
    await fetch(`${BASE}/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST',
    }),
  )
}

export async function listSessions(datasetId?: string): Promise<SessionSummary[]> {
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ''
  const data = await json<{ sessions: SessionSummary[] }>(
    await fetch(`${BASE}/api/sessions${query}`),
  )
  return data.sessions
}

export async function deleteSession(sessionId: string): Promise<void> {
  const resp = await fetch(`${BASE}/api/sessions/${sessionId}`, { method: 'DELETE' })
  if (!resp.ok) {
    throw new Error(`${resp.status}: ${await resp.text()}`)
  }
}

export async function getSessionEvents(sessionId: string): Promise<ApiEvent[]> {
  const data = await json<{ events: ApiEvent[] }>(
    await fetch(`${BASE}/api/sessions/${sessionId}/events`),
  )
  return data.events
}

export async function getHistory(sessionId: string): Promise<HistoryResponse> {
  return json<HistoryResponse>(
    await fetch(`${BASE}/api/sessions/${sessionId}/history`),
  )
}

export async function verifyRecord(
  sessionId: string,
  resultId: string,
): Promise<VerifyResult> {
  return json<VerifyResult>(await fetch(
    `${BASE}/api/sessions/${sessionId}/records/${encodeURIComponent(resultId)}/verify`,
    { method: 'POST' },
  ))
}

export async function getResult(sessionId: string, resultId: string): Promise<ResultArtifact> {
  return json<ResultArtifact>(
    await fetch(`${BASE}/api/sessions/${sessionId}/results/${resultId}`),
  )
}

export function exportUrl(sessionId: string, resultId: string): string {
  return `${BASE}/api/sessions/${sessionId}/results/${resultId}/export.csv`
}

function parseFrame(frame: string): ApiEvent | null {
  let event = 'message'
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!data.length) return null
  try {
    const payload = JSON.parse(data.join('\n'))
    if (!payload.type) payload.type = event
    return payload as ApiEvent
  } catch {
    return null
  }
}

async function* readEvents(resp: Response): AsyncGenerator<ApiEvent> {
  if (!resp.body) return
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let index = buffer.indexOf('\n\n')
    while (index >= 0) {
      const ev = parseFrame(buffer.slice(0, index))
      buffer = buffer.slice(index + 2)
      if (ev) yield ev
      index = buffer.indexOf('\n\n')
    }
  }
  buffer += decoder.decode()
  const tail = parseFrame(buffer.trim())
  if (tail) yield tail
}

export async function* streamTurn(
  sessionId: string,
  message: string,
  signal?: AbortSignal,
): AsyncGenerator<ApiEvent> {
  const resp = await fetch(`${BASE}/api/sessions/${sessionId}/turns`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  })
  if (!resp.ok || !resp.body) {
    throw new Error(`${resp.status}: ${await resp.text()}`)
  }
  yield* readEvents(resp)
}

export async function* streamAnalysis(
  sessionId: string,
  options: AnalysisOptions,
  signal?: AbortSignal,
): AsyncGenerator<ApiEvent> {
  const resp = await fetch(`${BASE}/api/sessions/${sessionId}/analysis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sections: options.sections || [],
      custom_text: options.customText || null,
      custom_title: options.customTitle || null,
    }),
    signal,
  })
  if (!resp.ok || !resp.body) {
    throw new Error(`${resp.status}: ${await resp.text()}`)
  }
  yield* readEvents(resp)
}

export async function getAnalysisModules(datasetId?: string): Promise<AnalysisModule[]> {
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ''
  const data = await json<{ modules: AnalysisModule[] }>(
    await fetch(`${BASE}/api/analysis/modules${query}`),
  )
  return data.modules
}

export async function getAnalysisHints(datasetId?: string): Promise<AnalysisHints> {
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : ''
  return json<AnalysisHints>(await fetch(`${BASE}/api/analysis/hints${query}`))
}
