import type { ApiEvent, HistoryItem, Meta, ResultArtifact, SessionSummary } from './types'

const BASE = (import.meta as any).env?.VITE_API_BASE || ''

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`${resp.status}: ${text}`)
  }
  return resp.json() as Promise<T>
}

export async function createSession(): Promise<string> {
  const resp = await fetch(`${BASE}/api/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  const data = await json<{ session_id: string }>(resp)
  return data.session_id
}

export async function getMeta(): Promise<Meta> {
  return json<Meta>(await fetch(`${BASE}/api/meta`))
}

export async function listSessions(): Promise<SessionSummary[]> {
  const data = await json<{ sessions: SessionSummary[] }>(
    await fetch(`${BASE}/api/sessions`),
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

export async function getHistory(sessionId: string): Promise<HistoryItem[]> {
  const data = await json<{ history: HistoryItem[] }>(
    await fetch(`${BASE}/api/sessions/${sessionId}/history`),
  )
  return data.history
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
