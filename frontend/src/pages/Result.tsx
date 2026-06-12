import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import type { RunResult, CodeFile } from '../types'

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
              <div className="code-viewer-header">
                <span className="code-viewer-path">{active.file_path}</span>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span className="badge badge-purple">{active.language}</span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    {active.content.split('\n').length} lines
                  </span>
                </div>
              </div>
              <pre style={{ margin: 0, borderRadius: 0, border: 'none', minHeight: '100%' }}>
                <code
                  className={`language-${LANG_MAP[active.language] || 'plaintext'}`}
                  dangerouslySetInnerHTML={{ __html: highlighted || active.content }}
                />
              </pre>
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
            <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', marginTop: 24 }} onClick={handleDownload}>
              ⬇ Download Project ZIP
            </button>
          )}
        </aside>
      </div>
    </div>
  )
}
