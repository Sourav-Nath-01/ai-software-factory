import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import type { RunResult, CodeFile, DiffResponse, FileDiff } from '../types'

// Language → highlight.js alias
const LANG_MAP: Record<string, string> = {
  python: 'python', javascript: 'javascript', typescript: 'typescript',
  yaml: 'yaml', dockerfile: 'dockerfile', markdown: 'markdown',
  json: 'json', bash: 'bash', sh: 'bash', text: 'plaintext',
}

function extToLang(path: string): string {
  const ext = path.split('.').pop() || ''
  const map: Record<string, string> = { py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', yml: 'yaml', yaml: 'yaml', md: 'markdown', json: 'json', sh: 'bash', txt: 'plaintext' }
  return map[ext] || 'plaintext'
}

function buildTree(files: CodeFile[]): Record<string, CodeFile[]> {
  const dirs: Record<string, CodeFile[]> = { '': [] }
  for (const f of files) {
    const parts = f.file_path.split('/')
    if (parts.length === 1) {
      dirs[''].push(f)
    } else {
      const dir = parts.slice(0, -1).join('/')
      if (!dirs[dir]) dirs[dir] = []
      dirs[dir].push(f)
    }
  }
  return dirs
}

export default function Result() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [run, setRun] = useState<RunResult | null>(null)
  const [active, setActive] = useState<CodeFile | null>(null)
  const [loading, setLoading] = useState(true)
  const [hljs, setHljs] = useState<typeof import('highlight.js').default | null>(null)

  // Diff viewer state
  const [diffData, setDiffData] = useState<DiffResponse | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [showDiff, setShowDiff] = useState(false)

  // Load highlight.js + theme lazily
  useEffect(() => {
    import('highlight.js').then(m => setHljs(m.default))
    // inject hljs CSS
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css'
    document.head.appendChild(link)
    return () => { document.head.removeChild(link) }
  }, [])

  useEffect(() => {
    if (!runId) return
    const poll = () => {
      fetch(`/api/runs/${runId}`)
        .then(r => r.json())
        .then((data: RunResult) => {
          setRun(data)
          setLoading(false)
          if (!active && data.files?.length) setActive(data.files[0])
          if (data.status === 'running') setTimeout(poll, 2000)
        })
        .catch(() => setLoading(false))
    }
    poll()
  }, [runId])

  const highlighted = (() => {
    if (!hljs || !active) return ''
    const lang = LANG_MAP[active.language] || extToLang(active.file_path)
    try { return hljs.highlight(active.content, { language: lang }).value }
    catch { return hljs.highlightAuto(active.content).value }
  })()

  const handleDownload = () => { window.open(`/api/runs/${runId}/download`, '_blank') }

  const handleLoadDiff = async () => {
    if (!runId || diffData) { setShowDiff(true); return }
    setDiffLoading(true)
    try {
      const res = await fetch(`/api/runs/${runId}/diff`)
      if (res.ok) {
        const data: DiffResponse = await res.json()
        setDiffData(data)
        setShowDiff(true)
      }
    } finally {
      setDiffLoading(false)
    }
  }

  // Render a simple inline diff with + / - line highlighting
  const renderDiff = (diff: FileDiff) => {
    if (diff.is_new) {
      return (
        <div style={{ padding: '0 16px' }}>
          <div style={{ fontSize: 11, color: 'var(--green)', marginBottom: 8, fontWeight: 600 }}>✨ New file added by Improver agent</div>
          {diff.improved_content.split('\n').map((line, i) => (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--green)', background: 'rgba(34,197,94,.06)', padding: '0 4px' }}>
              + {line}
            </div>
          ))}
        </div>
      )
    }
    if (diff.is_unchanged) {
      return <div style={{ padding: '16px', fontSize: 13, color: 'var(--text-muted)', fontStyle: 'italic' }}>No changes — this file was not modified by the Improver agent.</div>
    }

    // Compute line-level diff
    const origLines = diff.original_content.split('\n')
    const impLines  = diff.improved_content.split('\n')
    const maxLen    = Math.max(origLines.length, impLines.length)

    return (
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', minHeight: 200 }}>
        <div style={{ borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px', background: 'rgba(239,68,68,.06)', borderBottom: '1px solid var(--border)' }}>Before (generated)</div>
          {origLines.map((line, i) => {
            const changed = line !== (impLines[i] ?? '')
            return (
              <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 11, padding: '0 8px', color: changed ? 'var(--red)' : 'var(--text-dim)', background: changed ? 'rgba(239,68,68,.06)' : 'transparent', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                <span style={{ color: 'var(--text-muted)', marginRight: 8, userSelect: 'none' }}>{i + 1}</span>{line}
              </div>
            )
          })}
        </div>
        <div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px', background: 'rgba(34,197,94,.06)', borderBottom: '1px solid var(--border)' }}>After (improved)</div>
          {impLines.map((line, i) => {
            const changed = line !== (origLines[i] ?? '')
            return (
              <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 11, padding: '0 8px', color: changed ? 'var(--green)' : 'var(--text-dim)', background: changed ? 'rgba(34,197,94,.06)' : 'transparent', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                <span style={{ color: 'var(--text-muted)', marginRight: 8, userSelect: 'none' }}>{i + 1}</span>{line}
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="page" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="spinner" style={{ width: 40, height: 40, margin: '0 auto 16px' }} />
          <p style={{ color: 'var(--text-muted)' }}>Loading results…</p>
        </div>
      </div>
    )
  }

  if (!run) {
    return (
      <div className="page" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <p style={{ color: 'var(--red)', marginBottom: 16 }}>Run not found</p>
          <button className="btn btn-ghost" onClick={() => navigate('/')}>← Go Home</button>
        </div>
      </div>
    )
  }

  const tree = buildTree(run.files || [])
  const dirs = Object.keys(tree).sort()
  const m = run.metrics

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* Top bar */}
      <nav className="navbar" style={{ position: 'relative', borderBottom: '1px solid var(--border)' }}>
        <div className="navbar-logo" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
          <div className="logo-icon">🏭</div>
          <span>AI Software Factory</span>
        </div>
        <div style={{ flex: 1, padding: '0 24px', fontSize: 13, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
          "{run.prompt.substring(0, 60)}{run.prompt.length > 60 ? '…' : ''}"
        </div>
        <div className="navbar-links">
          <span className={`badge ${run.status === 'complete' ? 'badge-green' : run.status === 'failed' ? 'badge-red' : 'badge-yellow'}`}>
            {run.status}
          </span>
          {run.status === 'complete' && (
            <button className="btn btn-primary btn-sm" onClick={handleDownload}>
              ⬇ Download ZIP
            </button>
          )}
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/build')}>
            + New Build
          </button>
        </div>
      </nav>

      {/* Main 3-column layout */}
      <div className="result-layout" style={{ flex: 1, marginTop: 0, paddingTop: 0 }}>
        {/* Sidebar: file tree */}
        <aside className="result-sidebar">
          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 12 }}>
            Generated Files ({run.files?.length ?? 0})
          </div>
          <div className="file-tree">
            {dirs.map(dir => (
              <div key={dir}>
                {dir && <div className="file-tree-item file-tree-dir">📁 {dir}/</div>}
                {tree[dir].map(f => (
                  <div
                    key={f.file_path}
                    className={`file-tree-item file-tree-file ${active?.file_path === f.file_path ? 'active' : ''}`}
                    style={{ paddingLeft: dir ? 20 : 8 }}
                    onClick={() => setActive(f)}
                  >
                    📄 {f.file_path.split('/').pop()}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </aside>

        {/* Code viewer */}
        <main className="code-viewer">
          {active ? (
            <>
              <div className="code-viewer-header" style={{ flexDirection: 'column', gap: 8, alignItems: 'stretch', padding: '10px 16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span className="code-viewer-path">{active.file_path}</span>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span className="badge badge-purple">{active.language}</span>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {active.content.split('\n').length} lines
                    </span>
                  </div>
                </div>
                {/* Code / Diff toggle tabs */}
                {run?.status === 'complete' && (
                  <div style={{ display: 'flex', gap: 4 }}>
                    <button
                      id="tab-code"
                      onClick={() => setShowDiff(false)}
                      style={{
                        fontSize: 11, padding: '4px 12px', borderRadius: 6, border: 'none',
                        cursor: 'pointer', fontWeight: 600,
                        background: !showDiff ? 'var(--purple)' : 'var(--bg3)',
                        color: !showDiff ? '#fff' : 'var(--text-muted)',
                      }}
                    >
                      {'</>  Code'}
                    </button>
                    <button
                      id="tab-diff"
                      onClick={handleLoadDiff}
                      style={{
                        fontSize: 11, padding: '4px 12px', borderRadius: 6, border: 'none',
                        cursor: 'pointer', fontWeight: 600,
                        background: showDiff ? 'var(--purple)' : 'var(--bg3)',
                        color: showDiff ? '#fff' : 'var(--text-muted)',
                        display: 'flex', alignItems: 'center', gap: 4,
                      }}
                    >
                      {diffLoading ? '⌛' : '± '} Diff View
                    </button>
                  </div>
                )}
              </div>

              {showDiff && diffData ? (() => {
                const fileDiff = diffData.diffs.find(d => d.file_path === active.file_path)
                return fileDiff ? (
                  <div style={{ overflow: 'auto', flex: 1 }}>
                    {renderDiff(fileDiff)}
                  </div>
                ) : (
                  <div style={{ padding: 16, color: 'var(--text-muted)', fontSize: 13 }}>
                    No diff data available for this file (it may have been added during deployment).
                  </div>
                )
              })() : (
                <pre style={{ margin: 0, borderRadius: 0, border: 'none', minHeight: '100%' }}>
                  <code
                    className={`language-${LANG_MAP[active.language] || 'plaintext'}`}
                    dangerouslySetInnerHTML={{ __html: highlighted || active.content }}
                  />
                </pre>
              )}
            </>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
              Select a file to view its contents
            </div>
          )}
        </main>


        {/* Right panel: metrics */}
        <aside className="result-panel">
          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 16 }}>
            Run Metrics
          </div>

          {m ? (
            <>
              <div className="metrics-grid" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 20 }}>
                <div className="metric-card">
                  <span className="metric-value">{m.files_generated}</span>
                  <div className="metric-label">Files</div>
                </div>
                <div className="metric-card">
                  <span className="metric-value">{m.lines_of_code}</span>
                  <div className="metric-label">Lines</div>
                </div>
                <div className="metric-card">
                  <span className="metric-value">{m.issues_found}</span>
                  <div className="metric-label">Issues Found</div>
                </div>
                <div className="metric-card">
                  <span className="metric-value">{m.issues_fixed}</span>
                  <div className="metric-label">Issues Fixed</div>
                </div>
                <div className="metric-card">
                  <span className="metric-value">{m.review_iterations}</span>
                  <div className="metric-label">Review Cycles</div>
                </div>
                <div className="metric-card">
                  <span className="metric-value">{m.duration_seconds}s</span>
                  <div className="metric-label">Duration</div>
                </div>
              </div>
              <div className="card" style={{ textAlign: 'center', marginBottom: 12 }}>
                <div style={{ fontSize: 24 }}>{m.tests_passed ? '✅' : '⚠️'}</div>
                <div style={{ fontWeight: 600, marginTop: 4 }}>{m.tests_passed ? 'Tests Passed' : 'Tests Skipped'}</div>
              </div>
            </>
          ) : (
            <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>
              {run.status === 'running' ? 'Pipeline still running…' : 'No metrics available'}
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, margin: '20px 0 12px' }}>
            Run Info
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[
              { label: 'Run ID', value: run.run_id },
              { label: 'Model', value: run.model },
              { label: 'Created', value: new Date(run.created_at).toLocaleString() },
            ].map(({ label, value }) => (
              <div key={label}>
                <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: .5, marginBottom: 2 }}>{label}</div>
                <div style={{ fontSize: 12, fontFamily: 'var(--font-mono)', color: 'var(--text-dim)', wordBreak: 'break-all' }}>{value}</div>
              </div>
            ))}
          </div>

          {run.status === 'complete' && (
            <>
              {/* Diff summary card */}
              {diffData && (
                <div className="card" style={{ marginTop: 20, padding: '12px 14px' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 10 }}>Improver Changes</div>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <span className="badge badge-yellow">{diffData.files_changed} modified</span>
                    <span className="badge badge-green">{diffData.files_added} added</span>
                    <span className="badge" style={{ background: 'var(--bg3)', color: 'var(--text-muted)' }}>{diffData.files_unchanged} unchanged</span>
                  </div>
                </div>
              )}
              <button
                className="btn btn-ghost"
                id="view-changes-btn"
                style={{ width: '100%', justifyContent: 'center', marginTop: 12 }}
                onClick={handleLoadDiff}
                disabled={diffLoading}
              >
                {diffLoading ? '⌛ Loading diff...' : showDiff ? '📄 Hide Diff' : '± View Changes'}
              </button>
              <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', marginTop: 8 }} onClick={handleDownload}>
                ⬇ Download Project ZIP
              </button>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
