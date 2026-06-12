import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { RunSummary } from '../types'

function statusBadgeClass(status: string) {
  if (status === 'complete') return 'badge-green'
  if (status === 'failed')   return 'badge-red'
  return 'badge-yellow'
}

function fmtDuration(secs: number | undefined): string {
  if (!secs) return '—'
  const m = Math.floor(secs / 60)
  const s = Math.round(secs % 60)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

export default function History() {
  const navigate = useNavigate()
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/runs?limit=50')
      .then(r => r.json())
      .then((data: RunSummary[]) => { setRuns(data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  return (
    <div className="page">
      <nav className="navbar">
        <div className="navbar-logo" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
          <div className="logo-icon">🏭</div>
          <span>AI Software Factory</span>
        </div>
        <div className="navbar-links">
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/')}>← Home</button>
          <button className="btn btn-primary btn-sm" onClick={() => navigate('/build')}>+ New Build</button>
        </div>
      </nav>

      <div className="section" style={{ maxWidth: 900 }}>
        <h1 className="fade-up" style={{ fontSize: 32, fontWeight: 800, marginBottom: 8 }}>
          Run <span className="grad-text">History</span>
        </h1>
        <p className="fade-up-1" style={{ color: 'var(--text-muted)', marginBottom: 32 }}>
          All pipeline runs — click any row to view the generated project.
        </p>

        {loading && (
          <div style={{ textAlign: 'center', padding: 60 }}>
            <div className="spinner" style={{ width: 36, height: 36, margin: '0 auto 12px' }} />
            <p style={{ color: 'var(--text-muted)' }}>Loading runs…</p>
          </div>
        )}

        {!loading && runs.length === 0 && (
          <div className="card" style={{ textAlign: 'center', padding: '48px 24px' }}>
            <div style={{ fontSize: 48, marginBottom: 16 }}>🏗️</div>
            <h2 style={{ fontWeight: 700, marginBottom: 8 }}>No runs yet</h2>
            <p style={{ color: 'var(--text-muted)', marginBottom: 24 }}>
              Start your first build and the history will appear here.
            </p>
            <button className="btn btn-primary" onClick={() => navigate('/build')}>
              🚀 Build Something
            </button>
          </div>
        )}

        {!loading && runs.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {/* Header row */}
            <div style={{
              display: 'grid',
              gridTemplateColumns: '1fr 140px 160px 90px 80px 80px',
              gap: 12, padding: '8px 16px',
              fontSize: 11, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: 1, fontWeight: 600,
            }}>
              <span>Prompt</span>
              <span>Model</span>
              <span>Created</span>
              <span>Status</span>
              <span>Files</span>
              <span>Time</span>
            </div>

            {runs.map(run => (
              <div
                key={run.run_id}
                className="card"
                onClick={() => run.status === 'complete' && navigate(`/result/${run.run_id}`)}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr 140px 160px 90px 80px 80px',
                  gap: 12, alignItems: 'center',
                  cursor: run.status === 'complete' ? 'pointer' : 'default',
                  padding: '12px 16px',
                  transition: 'border-color 0.2s',
                }}
              >
                {/* Prompt */}
                <div style={{ overflow: 'hidden' }}>
                  <div style={{
                    fontWeight: 600, fontSize: 13,
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                  }}>
                    {run.prompt}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 2 }}>
                    {run.run_id}
                  </div>
                </div>

                {/* Model */}
                <div style={{
                  fontSize: 11, color: 'var(--text-dim)',
                  fontFamily: 'var(--font-mono)',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {run.model}
                </div>

                {/* Created */}
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  {new Date(run.created_at).toLocaleString('en-IN', {
                    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
                  })}
                </div>

                {/* Status */}
                <div>
                  <span className={`badge ${statusBadgeClass(run.status)}`} style={{ fontSize: 10 }}>
                    {run.status}
                  </span>
                </div>

                {/* Files */}
                <div style={{ fontSize: 13, fontWeight: 600 }}>
                  {run.metrics?.files_generated ?? '—'}
                </div>

                {/* Duration */}
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  {fmtDuration(run.metrics?.duration_seconds)}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Stats summary */}
        {!loading && runs.length > 0 && (
          <div className="stats-bar fade-up-1" style={{ marginTop: 32 }}>
            <div className="stat-item">
              <span className="stat-value grad-text">{runs.length}</span>
              <div className="stat-label">Total Runs</div>
            </div>
            <div className="stat-item">
              <span className="stat-value grad-text">
                {runs.filter(r => r.status === 'complete').length}
              </span>
              <div className="stat-label">Successful</div>
            </div>
            <div className="stat-item">
              <span className="stat-value grad-text">
                {runs.length > 0
                  ? `${Math.round((runs.filter(r => r.status === 'complete').length / runs.length) * 100)}%`
                  : '—'}
              </span>
              <div className="stat-label">Success Rate</div>
            </div>
            <div className="stat-item">
              <span className="stat-value grad-text">
                {(() => {
                  const completed = runs.filter(r => r.status === 'complete' && r.metrics)
                  if (!completed.length) return '—'
                  const avg = completed.reduce((s, r) => s + (r.metrics!.files_generated || 0), 0) / completed.length
                  return Math.round(avg)
                })()}
              </span>
              <div className="stat-label">Avg Files</div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
