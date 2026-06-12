import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

type SearchHit = {
  conversation_id: number
  source: string
  title: string
  updated_at: string | null
  snippet: string
  role: string
  hit_count: number
  meta: { cwd?: string }
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

const SOURCES: { key: string; label: string }[] = [
  { key: 'chatgpt', label: 'ChatGPT' },
  { key: 'claude', label: 'Claude' },
  { key: 'gemini', label: 'Gemini' },
  { key: 'claude_cli', label: 'claude CLI' },
  { key: 'codex_cli', label: 'codex CLI' },
]

const sourceLabel = (key: string) => SOURCES.find((s) => s.key === key)?.label ?? key

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

export default function App() {
  const [query, setQuery] = useState('')
  const [source, setSource] = useState<string | null>(null)
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [recent, setRecent] = useState<ConvSummary[]>([])
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [stats, setStats] = useState<SourceStat[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const debounce = useRef<number>(0)

  const refreshStats = useCallback(() => {
    fetch('/api/stats')
      .then((r) => r.json())
      .then((d) => setStats(d.sources))
      .catch(() => {})
  }, [])

  const loadRecent = useCallback((src: string | null) => {
    const p = new URLSearchParams({ limit: '50' })
    if (src) p.set('source', src)
    fetch(`/api/conversations?${p}`)
      .then((r) => r.json())
      .then((d) => setRecent(d.results))
      .catch(() => {})
  }, [])

  useEffect(() => {
    refreshStats()
    loadRecent(null)
  }, [refreshStats, loadRecent])

  const runSearch = useCallback(
    (q: string, src: string | null) => {
      if (!q.trim()) {
        setHits(null)
        loadRecent(src)
        return
      }
      const p = new URLSearchParams({ q })
      if (src) p.set('source', src)
      fetch(`/api/search?${p}`)
        .then((r) => r.json())
        .then((d) => setHits(d.results))
        .catch(() => setNotice('検索に失敗しました'))
    },
    [loadRecent],
  )

  // Debounced live search
  useEffect(() => {
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => runSearch(query, source), 250)
    return () => window.clearTimeout(debounce.current)
  }, [query, source, runSearch])

  const openConversation = (id: number) => {
    fetch(`/api/conversations/${id}`)
      .then((r) => r.json())
      .then(setSelected)
      .catch(() => setNotice('会話の取得に失敗しました'))
  }

  const importFiles = async (files: FileList | File[]) => {
    setBusy(true)
    for (const file of Array.from(files)) {
      const form = new FormData()
      form.append('file', file)
      try {
        const res = await fetch('/api/import', { method: 'POST', body: form })
        const d = await res.json()
        if (!res.ok) {
          setNotice(`${file.name}: ${d.detail ?? 'インポート失敗'}`)
        } else {
          setNotice(
            `${file.name}: ${d.conversations}件中 追加${d.inserted} / 更新${d.updated} / 変更なし${d.skipped}`,
          )
        }
      } catch {
        setNotice(`${file.name}: アップロードに失敗しました`)
      }
    }
    setBusy(false)
    refreshStats()
    runSearch(query, source)
  }

  const syncNow = async () => {
    setBusy(true)
    try {
      const res = await fetch('/api/sync', { method: 'POST' })
      const d = await res.json()
      setNotice(`CLI同期: ${d.files_imported}ファイル更新 (追加${d.inserted} / 更新${d.updated})`)
    } catch {
      setNotice('同期に失敗しました')
    }
    setBusy(false)
    refreshStats()
    runSearch(query, source)
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
                    <span className="result-title">{h.title}</span>
                    <span className="result-date">{fmtDate(h.updated_at)}</span>
                  </div>
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
