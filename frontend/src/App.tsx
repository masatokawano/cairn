import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

type SearchMode = 'keyword' | 'semantic' | 'hybrid'

type MatchReason = 'keyword' | 'semantic' | 'both'

type SearchHit = {
  conversation_id: number
  source: string
  title: string
  updated_at: string | null
  snippet: string
  role: string
  hit_count: number
  meta: { cwd?: string }
  match_reason: MatchReason
  matched_keywords: string[]
  semantic_score: number | null
}

type ConvSummary = {
  id: number
  source: string
  title: string
  updated_at: string | null
  message_count: number
  meta: { cwd?: string }
}

type Message = { id: number; idx: number; role: string; text: string; created_at: string | null }

type Conversation = {
  id: number
  source: string
  title: string
  created_at: string | null
  updated_at: string | null
  meta: { cwd?: string; path?: string }
  messages: Message[]
}

type SourceStat = { source: string; conversations: number; messages: number }

type ImportRun = {
  id: number
  source: string
  input_name: string | null
  started_at: string
  completed_at: string | null
  parser_version: string | null
  inserted: number
  updated: number
  skipped: number
  failed: number
  conversations: number
  warnings: number
  warning_summary: string | null
  content_hash: string | null
  status: 'ok' | 'error'
  error: string | null
}

const SOURCES: { key: string; label: string }[] = [
  { key: 'chatgpt', label: 'ChatGPT' },
  { key: 'claude', label: 'Claude' },
  { key: 'gemini', label: 'Gemini' },
  { key: 'claude_cli', label: 'claude CLI' },
  { key: 'codex_cli', label: 'codex CLI' },
]

const sourceLabel = (key: string) => SOURCES.find((s) => s.key === key)?.label ?? key

/** fetch wrapper: parses JSON and turns HTTP errors into Error(detail). */
async function api<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new Error('サーバーに接続できません')
  }
  let data: unknown = null
  try {
    data = await res.json()
  } catch {
    /* non-JSON body (e.g. proxy error page) */
  }
  if (!res.ok) {
    const detail = (data as { detail?: string } | null)?.detail
    throw new Error(detail ?? `HTTP ${res.status}`)
  }
  return data as T
}

const fmtWarnings = (warnings: string[] | undefined) =>
  warnings && warnings.length
    ? ` ⚠ warning ${warnings.length}件: ${warnings.slice(0, 3).join(' / ')}${warnings.length > 3 ? ' …' : ''}`
    : ''

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleString('ja-JP', { dateStyle: 'medium', timeStyle: 'short' })
}

/** Render snippet text where [[...]] marks highlights. */
function Snippet({ text }: { text: string }) {
  const parts = text.split(/\[\[(.*?)\]\]/g)
  return (
    <span>
      {parts.map((p, i) => (i % 2 === 1 ? <mark key={i}>{p}</mark> : <span key={i}>{p}</span>))}
    </span>
  )
}

const SEARCH_MODES: { key: SearchMode; label: string; hint: string }[] = [
  { key: 'hybrid', label: 'Hybrid', hint: 'キーワード + 意味の両方（RRF）' },
  { key: 'keyword', label: 'Keyword', hint: '完全一致・部分一致（FTS5）' },
  { key: 'semantic', label: 'Semantic', hint: '意味の近さ（embedding cosine）' },
]

// Persisted in localStorage so a returning user keeps their choice. Default
// is hybrid because the backend default (`keyword`) is for API back-compat,
// not the recommended UX.
function loadMode(): SearchMode {
  const v = localStorage.getItem('cairn.searchMode')
  return v === 'keyword' || v === 'semantic' || v === 'hybrid' ? v : 'hybrid'
}

const REASON_BADGE: Record<MatchReason, string> = {
  keyword: 'K',
  semantic: 'S',
  both: 'K+S',
}

export default function App() {
  const [query, setQuery] = useState('')
  const [source, setSource] = useState<string | null>(null)
  const [mode, setMode] = useState<SearchMode>(loadMode)
  // YYYY-MM-DD from <input type="date">; empty string = no filter. The
  // backend compares against ISO8601 updated_at, so a date-only string
  // works as both a lower and upper bound (lexicographic compare).
  const [after, setAfter] = useState('')
  const [before, setBefore] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [recent, setRecent] = useState<ConvSummary[]>([])
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [stats, setStats] = useState<SourceStat[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [importRuns, setImportRuns] = useState<ImportRun[] | null>(null)
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const debounce = useRef<number>(0)

  const refreshStats = useCallback(() => {
    api<{ sources: SourceStat[] }>('/api/stats')
      .then((d) => setStats(d.sources))
      .catch((e: Error) => setNotice(e.message))
  }, [])

  const loadRecent = useCallback((src: string | null) => {
    const p = new URLSearchParams({ limit: '50' })
    if (src) p.set('source', src)
    api<{ results: ConvSummary[] }>(`/api/conversations?${p}`)
      .then((d) => setRecent(d.results))
      .catch((e: Error) => setNotice(e.message))
  }, [])

  useEffect(() => {
    refreshStats()
    loadRecent(null)
  }, [refreshStats, loadRecent])

  const runSearch = useCallback(
    (q: string, src: string | null, m: SearchMode, a: string, b: string) => {
      if (!q.trim()) {
        setHits(null)
        loadRecent(src)
        return
      }
      const p = new URLSearchParams({ q, mode: m })
      if (src) p.set('source', src)
      if (a) p.set('after', a)
      // `before` from <input type="date"> is the start of that day. Pad to
      // end-of-day so a user picking "2026-06-25" includes everything on
      // that date, matching the MCP server's same convention.
      if (b) p.set('before', `${b}T23:59:59Z`)
      api<{ results: SearchHit[] }>(`/api/search?${p}`)
        .then((d) => setHits(d.results))
        .catch((e: Error) => setNotice(`検索に失敗しました: ${e.message}`))
    },
    [loadRecent],
  )

  // Debounced live search
  useEffect(() => {
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => runSearch(query, source, mode, after, before), 250)
    return () => window.clearTimeout(debounce.current)
  }, [query, source, mode, after, before, runSearch])

  // Persist mode so a refresh doesn't reset to the default.
  useEffect(() => {
    localStorage.setItem('cairn.searchMode', mode)
  }, [mode])

  const openConversation = (id: number) => {
    api<Conversation>(`/api/conversations/${id}`)
      .then(setSelected)
      .catch((e: Error) => setNotice(`会話の取得に失敗しました: ${e.message}`))
  }

  const openImportRuns = () => {
    setExpandedRunId(null)
    setImportRuns([])  // open the overlay immediately; results stream in below
    api<{ results: ImportRun[] }>('/api/import-runs?limit=50')
      .then((d) => setImportRuns(d.results))
      .catch((e: Error) => {
        setImportRuns(null)
        setNotice(`取り込み履歴の取得に失敗しました: ${e.message}`)
      })
  }

  type ImportResult = {
    conversations: number
    inserted: number
    updated: number
    skipped: number
    warnings?: string[]
  }

  const importFiles = async (files: FileList | File[]) => {
    setBusy(true)
    for (const file of Array.from(files)) {
      const form = new FormData()
      form.append('file', file)
      try {
        const d = await api<ImportResult>('/api/import', { method: 'POST', body: form })
        setNotice(
          `${file.name}: ${d.conversations}件中 追加${d.inserted} / 更新${d.updated} / 変更なし${d.skipped}` +
            fmtWarnings(d.warnings),
        )
      } catch (e) {
        setNotice(`${file.name}: ${(e as Error).message}`)
      }
    }
    setBusy(false)
    refreshStats()
    runSearch(query, source, mode, after, before)
  }

  type SyncResult = {
    files_imported: number
    inserted: number
    updated: number
    warnings?: string[]
  }

  const syncNow = async () => {
    setBusy(true)
    try {
      const d = await api<SyncResult>('/api/sync', { method: 'POST' })
      setNotice(
        `CLI同期: ${d.files_imported}ファイル更新 (追加${d.inserted} / 更新${d.updated})` +
          fmtWarnings(d.warnings),
      )
    } catch (e) {
      setNotice(`同期に失敗しました: ${(e as Error).message}`)
    }
    setBusy(false)
    refreshStats()
    runSearch(query, source, mode, after, before)
  }

  const totalConvs = stats.reduce((a, s) => a + s.conversations, 0)

  return (
    <div
      className={`app ${dragOver ? 'drag-over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        if (e.dataTransfer.files.length) importFiles(e.dataTransfer.files)
      }}
    >
      <header>
        <h1>Cairn</h1>
        <span className="tagline">AI会話アーカイブ・横断検索</span>
        <div className="header-actions">
          <button onClick={() => fileInput.current?.click()} disabled={busy}>
            エクスポートを取り込む
          </button>
          <button onClick={syncNow} disabled={busy}>
            CLIログ同期
          </button>
          <button onClick={openImportRuns} disabled={busy}>
            取り込み履歴
          </button>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".json,.zip"
            hidden
            onChange={(e) => e.target.files && importFiles(e.target.files)}
          />
        </div>
      </header>

      {notice && (
        <div className="notice" onClick={() => setNotice(null)}>
          {notice} ✕
        </div>
      )}

      <div className="search-row">
        <input
          className="search-box"
          type="search"
          placeholder={`${totalConvs} 件の会話を検索…`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
        />
        <div className="mode-toggle" role="radiogroup" aria-label="検索モード">
          {SEARCH_MODES.map((m) => (
            <button
              key={m.key}
              role="radio"
              aria-checked={mode === m.key}
              title={m.hint}
              className={`mode-btn ${mode === m.key ? 'active' : ''}`}
              onClick={() => setMode(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      <div className="filters">
        <button className={`chip ${source === null ? 'active' : ''}`} onClick={() => setSource(null)}>
          すべて
        </button>
        {SOURCES.map((s) => {
          const st = stats.find((x) => x.source === s.key)
          return (
            <button
              key={s.key}
              className={`chip ${source === s.key ? 'active' : ''}`}
              onClick={() => setSource(source === s.key ? null : s.key)}
            >
              {s.label}
              {st ? ` (${st.conversations})` : ''}
            </button>
          )
        })}
      </div>

      <div className="date-row">
        <label className="date-label">
          期間
          <input
            type="date"
            value={after}
            onChange={(e) => setAfter(e.target.value)}
            aria-label="開始日"
          />
          <span className="date-sep">〜</span>
          <input
            type="date"
            value={before}
            onChange={(e) => setBefore(e.target.value)}
            aria-label="終了日"
          />
        </label>
        {(after || before) && (
          <button
            className="date-clear"
            onClick={() => {
              setAfter('')
              setBefore('')
            }}
          >
            クリア
          </button>
        )}
      </div>

      <main>
        <div className="results">
          {hits !== null ? (
            hits.length === 0 ? (
              <p className="empty">ヒットなし</p>
            ) : (
              hits.map((h) => (
                <div key={h.conversation_id} className="result" onClick={() => openConversation(h.conversation_id)}>
                  <div className="result-head">
                    <span className={`badge badge-${h.source}`}>{sourceLabel(h.source)}</span>
                    <span
                      className={`reason reason-${h.match_reason}`}
                      title={
                        h.match_reason === 'both'
                          ? 'キーワードと意味の両方でヒット'
                          : h.match_reason === 'semantic'
                          ? '意味の近さでヒット'
                          : 'キーワード一致でヒット'
                      }
                    >
                      {REASON_BADGE[h.match_reason]}
                    </span>
                    {h.semantic_score !== null && (
                      <span className="sem-score" title="cosine 類似度">
                        {h.semantic_score.toFixed(2)}
                      </span>
                    )}
                    <span className="result-title">{h.title}</span>
                    <span className="result-date">{fmtDate(h.updated_at)}</span>
                  </div>
                  {h.matched_keywords.length > 0 && (
                    <div className="kw-chips">
                      {h.matched_keywords.slice(0, 8).map((kw) => (
                        <span key={kw} className="kw-chip">
                          {kw}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="result-snippet">
                    <Snippet text={h.snippet} />
                    {h.hit_count > 1 && <span className="hit-count">+{h.hit_count - 1}件ヒット</span>}
                  </div>
                </div>
              ))
            )
          ) : recent.length === 0 ? (
            <p className="empty">
              まだ会話がありません。エクスポートファイルをこのウィンドウにドロップするか、CLIログ同期を実行してください。
            </p>
          ) : (
            <>
              <p className="section-label">最近の会話</p>
              {recent.map((c) => (
                <div key={c.id} className="result" onClick={() => openConversation(c.id)}>
                  <div className="result-head">
                    <span className={`badge badge-${c.source}`}>{sourceLabel(c.source)}</span>
                    <span className="result-title">{c.title}</span>
                    <span className="result-date">{fmtDate(c.updated_at)}</span>
                  </div>
                  <div className="result-snippet muted">
                    {c.message_count} メッセージ {c.meta.cwd ? `· ${c.meta.cwd}` : ''}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      </main>

      {importRuns !== null && (
        <div className="overlay" onClick={() => setImportRuns(null)}>
          <div className="thread" onClick={(e) => e.stopPropagation()}>
            <div className="thread-head">
              <h2>取り込み履歴</h2>
              <button className="close" onClick={() => setImportRuns(null)}>
                ✕
              </button>
            </div>
            <div className="thread-meta">
              最新 {importRuns.length} 件（古い run から FIFO で 0600 の DB に追記）
            </div>
            <div className="messages">
              {importRuns.length === 0 ? (
                <p className="empty">取り込み履歴はまだありません。</p>
              ) : (
                importRuns.map((r) => {
                  const hasDetail = r.warnings > 0 || r.status === 'error'
                  const expanded = expandedRunId === r.id
                  return (
                    <div key={r.id} className={`run run-${r.status}`}>
                      <div
                        className="run-head"
                        onClick={() => hasDetail && setExpandedRunId(expanded ? null : r.id)}
                        style={{ cursor: hasDetail ? 'pointer' : 'default' }}
                      >
                        <span className={`badge badge-${r.source}`}>{sourceLabel(r.source)}</span>
                        <span className="run-name" title={r.input_name ?? ''}>
                          {r.input_name ?? '(no name)'}
                        </span>
                        <span className="run-date">{fmtDate(r.started_at)}</span>
                      </div>
                      <div className="run-counts">
                        <span className={`run-status run-status-${r.status}`}>
                          {r.status === 'ok' ? 'ok' : 'error'}
                        </span>
                        <span>会話 {r.conversations}</span>
                        <span>+{r.inserted}</span>
                        <span>~{r.updated}</span>
                        <span>={r.skipped}</span>
                        {r.failed > 0 && <span className="run-failed">×{r.failed}</span>}
                        {r.warnings > 0 && <span className="run-warn">⚠ {r.warnings}</span>}
                        {hasDetail && <span className="run-toggle">{expanded ? '▼' : '▶'}</span>}
                      </div>
                      {expanded && (
                        <pre className="run-detail">
                          {r.status === 'error' ? r.error ?? '(no detail)' : r.warning_summary ?? ''}
                        </pre>
                      )}
                    </div>
                  )
                })
              )}
            </div>
          </div>
        </div>
      )}

      {selected && (
        <div className="overlay" onClick={() => setSelected(null)}>
          <div className="thread" onClick={(e) => e.stopPropagation()}>
            <div className="thread-head">
              <span className={`badge badge-${selected.source}`}>{sourceLabel(selected.source)}</span>
              <h2>{selected.title}</h2>
              <button className="close" onClick={() => setSelected(null)}>
                ✕
              </button>
            </div>
            <div className="thread-meta">
              {fmtDate(selected.created_at)}
              {selected.meta.cwd ? ` · ${selected.meta.cwd}` : ''}
              {` · ${selected.messages.length} メッセージ`}
            </div>
            <div className="messages">
              {selected.messages.map((m) => (
                <div key={m.id} className={`msg msg-${m.role}`}>
                  <div className="msg-role">
                    {m.role === 'user' ? 'You' : m.role === 'assistant' ? 'AI' : m.role}
                    <span className="msg-date">{fmtDate(m.created_at)}</span>
                  </div>
                  <pre className="msg-text">{m.text}</pre>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
