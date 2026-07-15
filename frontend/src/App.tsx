import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

type SearchMode = 'keyword' | 'semantic' | 'hybrid'

type MatchReason = 'keyword' | 'semantic' | 'both'

type ItemKind = 'conversation' | 'bookmark' | 'reference' | 'note' | 'social_post'

type SearchHit = {
  conversation_id: number | null // null for external items (M2)
  item_id: number
  kind: ItemKind
  source: string
  title: string
  url: string | null
  updated_at: string | null
  snippet: string
  role: string | null
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

type ItemStat = { kind: ItemKind; source: string; count: number }

type Assertion = {
  id: number
  segment_id: number
  text: string
  actor: 'user' | 'assistant' | 'shared'
  kind: 'claim' | 'hypothesis' | 'conclusion' | 'decision' | 'rejected_idea' | 'question' | 'todo'
  status: 'tentative' | 'accepted' | 'rejected' | 'superseded' | 'unresolved' | 'completed'
  confidence: number | null
  supporting_message_ids: number[]
  locked_by_user: number
  user_edited_at: string | null
}

type Segment = {
  id: number
  conversation_id: number
  idx: number
  title: string
  summary: string
  topics: string
  locked_by_user: number
  user_edited_at: string | null
  assertions: Assertion[]
}

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
  { key: 'karakeep', label: 'Karakeep' },
  { key: 'zotero', label: 'Zotero' },
  { key: 'obsidian', label: 'Obsidian' }, // M3
  { key: 'x', label: 'X' }, // ADR-0006
  { key: 'facebook', label: 'Facebook' }, // ADR-0006
]

// items.kind filter chips (M2). Every kind the registry can hold is listed
// so the UI can filter the whole data model (note: M3 / social_post: ADR-0006).
const KINDS: { key: ItemKind; label: string }[] = [
  { key: 'conversation', label: '会話' },
  { key: 'bookmark', label: 'ブックマーク' },
  { key: 'reference', label: '文献' },
  { key: 'note', label: 'ノート' }, // M3: Obsidian
  { key: 'social_post', label: '発信' }, // ADR-0006: 自作ソーシャル投稿
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
  const [kind, setKind] = useState<ItemKind | null>(null)
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
  const [itemStats, setItemStats] = useState<ItemStat[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [importRuns, setImportRuns] = useState<ImportRun[] | null>(null)
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null)
  const [convTab, setConvTab] = useState<'messages' | 'extractions'>('messages')
  const [extractions, setExtractions] = useState<Segment[] | null>(null)
  const [extractionsBusy, setExtractionsBusy] = useState(false)
  const [expandedSegId, setExpandedSegId] = useState<number | null>(null)
  const [editingSegId, setEditingSegId] = useState<number | null>(null)
  const [editSegDraft, setEditSegDraft] = useState<{ title: string; summary: string }>({ title: '', summary: '' })
  const [editingAssertionId, setEditingAssertionId] = useState<number | null>(null)
  const [editAssertionDraft, setEditAssertionDraft] = useState<{ text: string; actor: string; kind: string; status: string }>({ text: '', actor: '', kind: '', status: '' })
  const fileInput = useRef<HTMLInputElement>(null)
  const debounce = useRef<number>(0)

  const refreshStats = useCallback(() => {
    api<{ sources: SourceStat[]; items?: ItemStat[] }>('/api/stats')
      .then((d) => {
        setStats(d.sources)
        setItemStats(d.items ?? [])
      })
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
    (q: string, src: string | null, k: ItemKind | null, m: SearchMode, a: string, b: string) => {
      if (!q.trim()) {
        setHits(null)
        loadRecent(src)
        return
      }
      const p = new URLSearchParams({ q, mode: m })
      if (src) p.set('source', src)
      if (k) p.set('kinds', k)
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
    debounce.current = window.setTimeout(() => runSearch(query, source, kind, mode, after, before), 250)
    return () => window.clearTimeout(debounce.current)
  }, [query, source, kind, mode, after, before, runSearch])

  // Persist mode so a refresh doesn't reset to the default.
  useEffect(() => {
    localStorage.setItem('cairn.searchMode', mode)
  }, [mode])

  const openConversation = (id: number) => {
    setConvTab('messages')
    setExtractions(null)
    setExpandedSegId(null)
    setEditingSegId(null)
    setEditingAssertionId(null)
    api<Conversation>(`/api/conversations/${id}`)
      .then(setSelected)
      .catch((e: Error) => setNotice(`会話の取得に失敗しました: ${e.message}`))
  }

  const loadExtractions = (convId: number) => {
    setExtractionsBusy(true)
    api<{ segments: Segment[] }>(`/api/conversations/${convId}/extractions`)
      .then((d) => {
        setExtractions(d.segments)
        if (d.segments.length > 0) setExpandedSegId(d.segments[0].id)
      })
      .catch((e: Error) => setNotice(`抽出結果の取得に失敗しました: ${e.message}`))
      .finally(() => setExtractionsBusy(false))
  }

  const saveSegment = (segId: number, title: string, summary: string, convId: number) => {
    api<{ ok: boolean }>(`/api/segments/${segId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, summary }),
    })
      .then(() => { setEditingSegId(null); loadExtractions(convId) })
      .catch((e: Error) => setNotice(`保存に失敗しました: ${e.message}`))
  }

  const deleteSegment = (segId: number, convId: number) => {
    if (!confirm('このセグメントを削除しますか？')) return
    api<{ ok: boolean }>(`/api/segments/${segId}`, { method: 'DELETE' })
      .then(() => loadExtractions(convId))
      .catch((e: Error) => setNotice(`削除に失敗しました: ${e.message}`))
  }

  const saveAssertion = (
    assertionId: number, text: string, actor: string, kind: string, status: string, convId: number
  ) => {
    api<{ ok: boolean }>(`/api/assertions/${assertionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, actor, kind, status }),
    })
      .then(() => { setEditingAssertionId(null); loadExtractions(convId) })
      .catch((e: Error) => setNotice(`保存に失敗しました: ${e.message}`))
  }

  const deleteAssertion = (assertionId: number, convId: number) => {
    if (!confirm('この Assertion を削除しますか？')) return
    api<{ ok: boolean }>(`/api/assertions/${assertionId}`, { method: 'DELETE' })
      .then(() => loadExtractions(convId))
      .catch((e: Error) => setNotice(`削除に失敗しました: ${e.message}`))
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
    runSearch(query, source, kind, mode, after, before)
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
    runSearch(query, source, kind, mode, after, before)
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
          // external sources (karakeep/zotero) count via the items registry
          const it = st ? null : itemStats.filter((x) => x.source === s.key)
          const count = st
            ? st.conversations
            : it && it.length
            ? it.reduce((n, x) => n + x.count, 0)
            : null
          return (
            <button
              key={s.key}
              className={`chip ${source === s.key ? 'active' : ''}`}
              onClick={() => setSource(source === s.key ? null : s.key)}
            >
              {s.label}
              {count !== null ? ` (${count})` : ''}
            </button>
          )
        })}
      </div>

      <div className="filters">
        <button className={`chip ${kind === null ? 'active' : ''}`} onClick={() => setKind(null)}>
          全種類
        </button>
        {KINDS.map((k) => (
          <button
            key={k.key}
            className={`chip ${kind === k.key ? 'active' : ''}`}
            onClick={() => setKind(kind === k.key ? null : k.key)}
          >
            {k.label}
          </button>
        ))}
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
                <div
                  key={h.item_id}
                  className="result"
                  onClick={() => {
                    // conversations open in-app; external items open the
                    // original page (read-only: Cairn holds only the index).
                    // scheme guard duplicates the backend's _safe_external_url
                    // — defense in depth for an untrusted, external-origin URL
                    if (h.conversation_id !== null) openConversation(h.conversation_id)
                    else if (h.url && /^https?:\/\//i.test(h.url)) {
                      window.open(h.url, '_blank', 'noopener,noreferrer')
                    }
                  }}
                >
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
                    <span className="result-title">
                      {h.title}
                      {h.conversation_id === null && h.url && (
                        <span className="ext-mark" title={h.url}>
                          {' '}↗
                        </span>
                      )}
                    </span>
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

            <div className="conv-tabs">
              <button
                className={`conv-tab ${convTab === 'messages' ? 'active' : ''}`}
                onClick={() => setConvTab('messages')}
              >
                メッセージ
              </button>
              <button
                className={`conv-tab ${convTab === 'extractions' ? 'active' : ''}`}
                onClick={() => {
                  setConvTab('extractions')
                  if (extractions === null) loadExtractions(selected.id)
                }}
              >
                抽出結果
              </button>
            </div>

            {convTab === 'messages' && (
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
            )}

            {convTab === 'extractions' && (
              <div className="messages">
                {extractionsBusy && <p className="empty">読み込み中…</p>}
                {!extractionsBusy && extractions !== null && extractions.length === 0 && (
                  <p className="empty">セグメントがありません。<code>admin extract-segments</code> を実行してください。</p>
                )}
                {!extractionsBusy && extractions !== null && extractions.map((seg) => {
                  const isOpen = expandedSegId === seg.id
                  const isEditSeg = editingSegId === seg.id
                  let topicsArr: string[] = []
                  try { topicsArr = JSON.parse(seg.topics || '[]') } catch { /* ignore */ }
                  return (
                    <div key={seg.id} className={`seg-card ${seg.locked_by_user ? 'seg-locked' : ''}`}>
                      <div className="seg-head" onClick={() => setExpandedSegId(isOpen ? null : seg.id)}>
                        <span className="seg-idx">§{seg.idx + 1}</span>
                        {seg.locked_by_user === 1 && <span className="lock-badge" title="手動編集済み">🔒</span>}
                        <span className="seg-title">{seg.title}</span>
                        <span className="seg-count">{seg.assertions.length}件</span>
                        <span className="seg-toggle">{isOpen ? '▼' : '▶'}</span>
                      </div>

                      {isOpen && !isEditSeg && (
                        <div className="seg-body">
                          <p className="seg-summary">{seg.summary}</p>
                          {topicsArr.length > 0 && (
                            <div className="seg-topics">
                              {topicsArr.map((t) => <span key={t} className="kw-chip">{t}</span>)}
                            </div>
                          )}
                          <div className="seg-actions">
                            <button className="btn-edit" onClick={() => {
                              setEditingSegId(seg.id)
                              setEditSegDraft({ title: seg.title, summary: seg.summary })
                            }}>編集</button>
                            <button className="btn-delete" onClick={() => deleteSegment(seg.id, selected.id)}>削除</button>
                          </div>

                          {seg.assertions.map((a) => {
                            const isEditA = editingAssertionId === a.id
                            return (
                              <div key={a.id} className={`assertion-row ${a.locked_by_user ? 'assertion-locked' : ''}`}>
                                {isEditA ? (
                                  <div className="assertion-edit">
                                    <textarea
                                      className="assertion-text-input"
                                      value={editAssertionDraft.text}
                                      onChange={(e) => setEditAssertionDraft({ ...editAssertionDraft, text: e.target.value })}
                                    />
                                    <div className="assertion-edit-selects">
                                      <select value={editAssertionDraft.actor} onChange={(e) => setEditAssertionDraft({ ...editAssertionDraft, actor: e.target.value })}>
                                        {['user', 'assistant', 'shared'].map(v => <option key={v} value={v}>{v}</option>)}
                                      </select>
                                      <select value={editAssertionDraft.kind} onChange={(e) => setEditAssertionDraft({ ...editAssertionDraft, kind: e.target.value })}>
                                        {['claim', 'hypothesis', 'conclusion', 'decision', 'rejected_idea', 'question', 'todo'].map(v => <option key={v} value={v}>{v}</option>)}
                                      </select>
                                      <select value={editAssertionDraft.status} onChange={(e) => setEditAssertionDraft({ ...editAssertionDraft, status: e.target.value })}>
                                        {['tentative', 'accepted', 'rejected', 'superseded', 'unresolved', 'completed'].map(v => <option key={v} value={v}>{v}</option>)}
                                      </select>
                                    </div>
                                    <div className="assertion-edit-btns">
                                      <button className="btn-save" onClick={() => saveAssertion(a.id, editAssertionDraft.text, editAssertionDraft.actor, editAssertionDraft.kind, editAssertionDraft.status, selected.id)}>保存</button>
                                      <button className="btn-cancel" onClick={() => setEditingAssertionId(null)}>キャンセル</button>
                                    </div>
                                  </div>
                                ) : (
                                  <>
                                    <div className="assertion-meta">
                                      <span className={`a-badge a-actor-${a.actor}`}>{a.actor}</span>
                                      <span className={`a-badge a-kind-${a.kind}`}>{a.kind}</span>
                                      <span className={`a-badge a-status-${a.status}`}>{a.status}</span>
                                      {a.confidence !== null && <span className="a-conf">{(a.confidence * 100).toFixed(0)}%</span>}
                                      {a.locked_by_user === 1 && <span className="lock-badge" title="手動編集済み">🔒</span>}
                                    </div>
                                    <p className="assertion-text">{a.text}</p>
                                    <div className="assertion-actions">
                                      <button className="btn-edit" onClick={() => {
                                        setEditingAssertionId(a.id)
                                        setEditAssertionDraft({ text: a.text, actor: a.actor, kind: a.kind, status: a.status })
                                      }}>編集</button>
                                      <button className="btn-delete" onClick={() => deleteAssertion(a.id, selected.id)}>削除</button>
                                    </div>
                                  </>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}

                      {isOpen && isEditSeg && (
                        <div className="seg-body">
                          <input
                            className="seg-title-input"
                            value={editSegDraft.title}
                            onChange={(e) => setEditSegDraft({ ...editSegDraft, title: e.target.value })}
                          />
                          <textarea
                            className="seg-summary-input"
                            value={editSegDraft.summary}
                            onChange={(e) => setEditSegDraft({ ...editSegDraft, summary: e.target.value })}
                          />
                          <div className="seg-actions">
                            <button className="btn-save" onClick={() => saveSegment(seg.id, editSegDraft.title, editSegDraft.summary, selected.id)}>保存</button>
                            <button className="btn-cancel" onClick={() => setEditingSegId(null)}>キャンセル</button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
