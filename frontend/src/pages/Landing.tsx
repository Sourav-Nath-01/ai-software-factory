import { useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import type { AppStats } from '../types'

const GITHUB_URL = 'https://github.com/Sourav-Nath-01/ai-software-factory'

const AGENTS = [
  { icon: '🏗️', role: 'Architect', name: 'Planner', desc: 'Designs system architecture, file structure, and API contracts.' },
  { icon: '💻', role: 'Engineer', name: 'Coder', desc: 'Writes production-ready code based on the architecture plan.' },
  { icon: '🔍', role: 'Reviewer', name: 'Code Reviewer', desc: 'Finds bugs, security issues, and anti-patterns in the code.' },
  { icon: '✨', role: 'Specialist', name: 'Improver', desc: 'Fixes every issue found in the review with targeted patches.' },
  { icon: '🧪', role: 'QA Engineer', name: 'Tester', desc: 'Writes comprehensive pytest suites for all generated code.' },
  { icon: '▶️', role: 'QA Analyst', name: 'Test Runner', desc: 'Executes tests, analyzes failures, and guides bug fixes.' },
  { icon: '🚀', role: 'DevOps', name: 'Deployer', desc: 'Generates Dockerfile, CI/CD pipelines, and deployment guides.' },
]

export default function Landing() {
  const navigate = useNavigate()
  const [stats, setStats] = useState<AppStats | null>(null)

  useEffect(() => {
    fetch('/api/stats').then(r => r.json()).then(setStats).catch(() => null)
  }, [])

  return (
    <div className="page">
      <nav className="navbar">
        <div className="navbar-logo">
          <div className="logo-icon">🏭</div>
          <span>AI Software Factory</span>
        </div>
        <div className="navbar-links">
          <a href={GITHUB_URL} target="_blank" rel="noreferrer" className="btn btn-ghost btn-sm">
            GitHub ↗
          </a>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/history')}
            style={{ marginRight: 4 }}>
            History
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => navigate('/build')}>
            Try Demo →
          </button>
        </div>
      </nav>

      {/* Hero */}
      <section className="hero fade-up">
        <div className="hero-eyebrow">🏭 Multi-Agent AI System</div>
        <h1>
          Your Personal<br />
          <span className="grad-text">AI Engineering Team</span>
        </h1>
        <p>
          7 specialized agents collaborate like a real team — planning, coding,
          reviewing, testing, and deploying your software end-to-end.
        </p>
        <div className="hero-actions">
          <button className="btn btn-primary" onClick={() => navigate('/build')}>
            🚀 Build Something
          </button>
          <a href={GITHUB_URL} target="_blank" rel="noreferrer" className="btn btn-ghost">
            View on GitHub
          </a>
        </div>
      </section>

      {/* Stats */}
      {stats && (
        <div className="stats-bar fade-up-1">
          <div className="stat-item">
            <span className="stat-value grad-text">{stats.total_runs}</span>
            <div className="stat-label">Total Runs</div>
          </div>
          <div className="stat-item">
            <span className="stat-value grad-text">{stats.success_rate}%</span>
            <div className="stat-label">Success Rate</div>
          </div>
          <div className="stat-item">
            <span className="stat-value grad-text">{stats.avg_files_generated || '12+'}</span>
            <div className="stat-label">Avg Files Generated</div>
          </div>
          <div className="stat-item">
            <span className="stat-value grad-text">7</span>
            <div className="stat-label">Specialized Agents</div>
          </div>
        </div>
      )}

      {/* Agent grid */}
      <section className="section fade-up-2">
        <h2 style={{ fontSize: 28, fontWeight: 800, textAlign: 'center', marginBottom: 8 }}>
          Meet the Team
        </h2>
        <p style={{ textAlign: 'center', color: 'var(--text-muted)', marginBottom: 40 }}>
          Each agent is a specialist — narrow role, deep expertise.
        </p>
        <div className="agent-grid">
          {AGENTS.map(a => (
            <div key={a.name} className="agent-card">
              <div className="agent-icon">{a.icon}</div>
              <div className="agent-role">{a.role}</div>
              <div className="agent-name">{a.name}</div>
              <div className="agent-desc">{a.desc}</div>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section className="section" style={{ borderTop: '1px solid var(--border)' }}>
        <h2 style={{ fontSize: 28, fontWeight: 800, textAlign: 'center', marginBottom: 48 }}>
          The Iterative Pipeline
        </h2>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 600, margin: '0 auto' }}>
          {[
            { step: '01', title: 'Describe what to build', desc: 'A sentence or full spec — the Planner agent handles the rest.' },
            { step: '02', title: 'Watch agents collaborate', desc: 'Code is generated, reviewed, improved, tested — all live in your browser.' },
            { step: '03', title: 'Download production code', desc: 'Get a complete project with Dockerfile, CI/CD, and deployment guide.' },
          ].map(({ step, title, desc }) => (
            <div key={step} className="card" style={{ display: 'flex', gap: 20, alignItems: 'flex-start' }}>
              <span style={{ fontSize: 11, fontWeight: 800, color: 'var(--purple-light)', letterSpacing: 1, minWidth: 28 }}>{step}</span>
              <div>
                <div style={{ fontWeight: 700, marginBottom: 4 }}>{title}</div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>{desc}</div>
              </div>
            </div>
          ))}
        </div>
        <div style={{ textAlign: 'center', marginTop: 48 }}>
          <button className="btn btn-primary" onClick={() => navigate('/build')} style={{ fontSize: 17, padding: '14px 32px' }}>
            🚀 Start Building — It's Free
          </button>
          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 12 }}>
            No API key needed — try Demo Mode instantly
          </p>
        </div>
      </section>
    </div>
  )
}
