import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import type { PipelineEvent, StageState, HITLPlan } from '../types'

interface ModelOption {
  provider: string
  model: string
  label: string
  desc: string
  badge: string
  badgeColor: string
  keyPlaceholder: string
  keyLink: string
}

const PROVIDER_MODELS: ModelOption[] = [
  {
    provider: 'demo',
    model: 'demo',
    label: 'Demo Mode',
    desc: 'No API key needed. Mock pipeline, full UI.',
    badge: 'FREE · No key',
    badgeColor: 'badge-cyan',
    keyPlaceholder: '',
    keyLink: '',
  },
  {
    provider: 'gemini',
    model: 'gemini/gemini-2.0-flash',
    label: 'Gemini 2.0 Flash',
    desc: 'Google\'s latest free model — fast & capable.',
    badge: 'FREE · Recommended',
    badgeColor: 'badge-green',
    keyPlaceholder: 'AIza... (from aistudio.google.com)',
    keyLink: 'https://aistudio.google.com/apikey',
  },
  {
    provider: 'gemini',
    model: 'gemini/gemini-2.0-flash-lite',
    label: 'Gemini 2.0 Flash Lite',
    desc: 'Lightest Gemini model — fastest responses.',
    badge: 'FREE · Fastest',
    badgeColor: 'badge-green',
    keyPlaceholder: 'AIza... (from aistudio.google.com)',
    keyLink: 'https://aistudio.google.com/apikey',
  },
  {
    provider: 'groq',
    model: 'groq/llama-3.1-8b-instant',
    label: 'Llama 3.1 8B (Groq)',
    desc: 'Best for free tier — 30K TPM limit, near-instant replies.',
    badge: 'FREE · Recommended',
    badgeColor: 'badge-green',
    keyPlaceholder: 'gsk_... (from console.groq.com)',
    keyLink: 'https://console.groq.com/keys',
  },
  {
    provider: 'groq',
    model: 'groq/llama-3.3-70b-versatile',
    label: 'Llama 3.3 70B (Groq)',
    desc: 'More capable but 12K TPM — may hit rate limits on large code.',
    badge: 'FREE · High TPM',
    badgeColor: 'badge-yellow',
    keyPlaceholder: 'gsk_... (from console.groq.com)',
    keyLink: 'https://console.groq.com/keys',
  },
  {
    provider: 'openai',
    model: 'gpt-4o-mini',
    label: 'GPT-4o Mini',
    desc: 'OpenAI — fastest & cheapest paid option.',
    badge: 'PAID',
    badgeColor: 'badge-yellow',
    keyPlaceholder: 'sk-... (from platform.openai.com)',
    keyLink: 'https://platform.openai.com/api-keys',
  },
]




const EXAMPLE_PROMPTS = [
  'Create a REST API for a todo app with FastAPI, SQLite, and JWT auth',
  'Build a CLI weather tool that fetches real-time data from OpenWeatherMap',
  'Create a URL shortener service with click analytics tracking',
  'Build a markdown-to-HTML converter with a web interface',
]

const INITIAL_STAGES: StageState[] = [
  { name: 'Planning',        icon: '🏗️', status: 'pending' },
  { name: 'Code Generation', icon: '💻', status: 'pending' },
  { name: 'Code Review',     icon: '🔍', status: 'pending' },
  { name: 'Code Improvement',icon: '✨', status: 'pending' },
  { name: 'Test Generation', icon: '🧪', status: 'pending' },
  { name: 'Test Execution',  icon: '▶️', status: 'pending' },
  { name: 'Deployment',      icon: '🚀', status: 'pending' },
]

export default function Build() {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<ModelOption>(PROVIDER_MODELS[0]) // Demo by default
  const [apiKey,   setApiKey]   = useState('')
  const [prompt,   setPrompt]   = useState('')
  const [maxRev,   setMaxRev]   = useState(1)   // 1 = fast
  const [maxTest,  setMaxTest]  = useState(1)   // 1 = fast

  const [phase,      setPhase]      = useState<'form' | 'running' | 'done'>('form')
  const [stages,     setStages]     = useState<StageState[]>(INITIAL_STAGES)
  const [logs,       setLogs]       = useState<string[]>([])
  const [runId,      setRunId]      = useState<string | null>(null)
  const [error,      setError]      = useState<string | null>(null)
  const [elapsed,    setElapsed]    = useState(0)       // total seconds running
  const [stageTimer, setStageTimer] = useState(0)      // seconds in current stage

  // HITL state
  const [hitlPlan,   setHitlPlan]   = useState<HITLPlan | null>(null)
  const [hitlRunId,  setHitlRunId]  = useState<string | null>(null)
  const [hitlLoading, setHitlLoading] = useState(false)

  // Stop/cancel state
  const [stopping, setStopping] = useState(false)

  const logRef     = useRef<HTMLDivElement>(null)
  const wsRef      = useRef<WebSocket | null>(null)
  const timerRef   = useRef<ReturnType<typeof setInterval> | null>(null)
  const stageTRef  = useRef<ReturnType<typeof setInterval> | null>(null)
  const isDemo   = selected.provider === 'demo'
  const needsKey = !isDemo && (!apiKey || apiKey.length < 8)

  // Auto-scroll logs
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs])

  // Total elapsed timer — ticks every second while running
  useEffect(() => {
    if (phase === 'running') {
      setElapsed(0)
      timerRef.current = setInterval(() => setElapsed(s => s + 1), 1000)
    } else {
      if (timerRef.current) clearInterval(timerRef.current)
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [phase])

  // Per-stage timer — resets when a new stage starts
  useEffect(() => {
    const running = stages.find(s => s.status === 'running')
    if (running) {
      setStageTimer(0)
      if (stageTRef.current) clearInterval(stageTRef.current)
      stageTRef.current = setInterval(() => setStageTimer(s => s + 1), 1000)
    } else {
      if (stageTRef.current) clearInterval(stageTRef.current)
    }
    return () => { if (stageTRef.current) clearInterval(stageTRef.current) }
  }, [stages.map(s => s.status).join(',')])

  const fmtTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  const addLog = (msg: string) => {
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setLogs(prev => [...prev, `[${ts}] ${msg}`])
  }

  const updateStage = (name: string, updates: Partial<StageState>) =>
    setStages(prev => prev.map(s => s.name === name ? { ...s, ...updates } : s))

  const handleEvent = (ev: PipelineEvent) => {
    if (ev.type === 'ping') return
    // HITL checkpoint — show approval modal
    if (ev.type === 'hitl_checkpoint') {
      const plan = ev.plan
      if (plan) {
        setHitlPlan(plan)
      }
      addLog('⏸️ Pipeline paused — waiting for your architecture approval')
      return
    }
    // HITL approved — dismiss modal and continue
    if (ev.type === 'hitl_approved') {
      setHitlPlan(null)
      setHitlLoading(false)
      addLog('✅ Architecture approved — continuing to code generation')
      return
    }
    // Pipeline was cancelled by user
    if (ev.type === 'cancelled') {
      setStopping(false)
      setError('Pipeline stopped by user.')
      setPhase('done')
      addLog('🛑 Pipeline stopped by user')
      return
    }
    if (ev.type === 'stage_start'    && ev.stage) { updateStage(ev.stage, { status: 'running', message: ev.meta }); addLog(`▶ ${ev.stage}${ev.meta ? ` (${ev.meta})` : ''}`) }
    if (ev.type === 'stage_complete' && ev.stage) {
      const dur = ev.duration ? ` [${ev.duration}s]` : ''
      const info = ev.data ? ` — ${Object.entries(ev.data).map(([k,v]) => `${k}: ${v}`).join(', ')}` : ''
      updateStage(ev.stage, { status: 'complete', duration: ev.duration, data: ev.data })
      addLog(`✅ ${ev.stage}${dur}${info}`)
    }
    if (ev.type === 'log'      && ev.message) addLog(`   ${ev.message}`)
    if (ev.type === 'error'    && ev.message) { setError(ev.message); addLog(`❌ ${ev.message}`); setPhase('done') }
  }

  const handleSubmit = async () => {
    if (!prompt.trim() || needsKey) return
    setPhase('running')
    setStages(INITIAL_STAGES.map(s => ({ ...s, status: 'pending' })))
    setLogs([]); setError(null)
    addLog(isDemo ? '🎭 Starting Demo Mode (mock pipeline)...' : `🚀 Starting with ${selected.label}...`)

    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: prompt.trim(),
          api_key: isDemo ? null : apiKey,
          provider: selected.provider,
          model: selected.model,
          max_review_iterations: maxRev,
          max_test_fix_iterations: maxTest,
        }),
      })
      const { run_id } = await res.json()
      setRunId(run_id)
      setHitlRunId(run_id)
      const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${wsProto}//${window.location.host}/ws/${run_id}`)
      wsRef.current = ws
      ws.onmessage = e => { 
        try { 
          const ev = JSON.parse(e.data) as PipelineEvent
          if (ev.type === 'complete') {
            addLog('🎉 Pipeline complete!')
            setPhase('done')
            setTimeout(() => navigate(`/result/${run_id}`), 1500)
          } else {
            handleEvent(ev)
          }
        } catch { /**/ } 
      }
      ws.onerror = () => { setError('WebSocket connection error'); setPhase('done') }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start pipeline')
      setPhase('done')
    }
  }

  useEffect(() => () => { wsRef.current?.close() }, [])

  // HITL handlers
  const handleHITLDecision = async (approved: boolean) => {
    if (!hitlRunId) return
    setHitlLoading(true)
    try {
      await fetch(`/api/runs/${hitlRunId}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved, reason: approved ? '' : 'User rejected the architecture plan.' }),
      })
      if (!approved) {
        setHitlPlan(null)
        setError('You rejected the architecture. Pipeline stopped.')
        setPhase('done')
      }
      // If approved, the WebSocket will receive hitl_approved event and close the modal
    } catch (e) {
      setHitlLoading(false)
    }
  }

  // HITL Approval Modal (shown during running phase when hitlPlan is set)
  const HITLModal = hitlPlan ? (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(0,0,0,0.75)', backdropFilter: 'blur(6px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
    }}>
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 16, padding: 32, maxWidth: 620, width: '100%',
        maxHeight: '85vh', overflowY: 'auto',
        boxShadow: '0 24px 80px rgba(0,0,0,0.5)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <span style={{ fontSize: 28 }}>👤</span>
          <div>
            <div style={{ fontWeight: 800, fontSize: 20 }}>Architecture Review</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Human-in-the-Loop Checkpoint — approve before code generation starts</div>
          </div>
        </div>

        <div style={{ borderTop: '1px solid var(--border)', margin: '16px 0' }} />

        <div style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 4 }}>{hitlPlan.project_name}</div>
          <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6 }}>{hitlPlan.description}</div>
        </div>

        {hitlPlan.tech_stack.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 8 }}>Tech Stack</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {hitlPlan.tech_stack.map(t => (
                <span key={t} className="badge badge-purple" style={{ fontSize: 11 }}>{t}</span>
              ))}
            </div>
          </div>
        )}

        {hitlPlan.file_structure.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 8 }}>
              Files to generate ({hitlPlan.file_structure.length})
            </div>
            <div style={{
              background: 'var(--bg3)', borderRadius: 8, padding: '10px 14px',
              fontFamily: 'var(--font-mono)', fontSize: 12, maxHeight: 160, overflowY: 'auto',
            }}>
              {hitlPlan.file_structure.map(f => (
                <div key={f} style={{ color: 'var(--green)', marginBottom: 2 }}>📄 {f}</div>
              ))}
            </div>
          </div>
        )}

        {hitlPlan.endpoints.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1, marginBottom: 8 }}>API Endpoints</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {hitlPlan.endpoints.map((e, i) => (
                <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: 12 }}>
                  <span className={`badge ${e.method === 'GET' ? 'badge-green' : e.method === 'POST' ? 'badge-yellow' : 'badge-purple'}`} style={{ fontSize: 10, minWidth: 44, textAlign: 'center' }}>{e.method}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-dim)' }}>{e.path}</span>
                  <span style={{ color: 'var(--text-muted)', flex: 1 }}>{e.description}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div style={{ display: 'flex', gap: 12, marginTop: 24 }}>
          <button
            id="hitl-approve-btn"
            className="btn btn-primary"
            style={{ flex: 1, justifyContent: 'center', fontSize: 15, padding: '13px' }}
            disabled={hitlLoading}
            onClick={() => handleHITLDecision(true)}
          >
            {hitlLoading ? '⌛ Processing...' : '✅ Approve — Start Coding'}
          </button>
          <button
            id="hitl-reject-btn"
            className="btn btn-ghost"
            style={{ padding: '13px 20px', color: 'var(--red)', borderColor: 'rgba(239,68,68,.3)' }}
            disabled={hitlLoading}
            onClick={() => handleHITLDecision(false)}
          >
            ✗ Reject
          </button>
        </div>
      </div>
    </div>
  ) : null



  if (phase === 'form') {
    return (
      <div className="page">
        <nav className="navbar">
          <div className="navbar-logo" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
            <div className="logo-icon">🏭</div>
            <span>AI Software Factory</span>
          </div>
          <div className="navbar-links">
            <span className="badge badge-purple">Multi-Agent Pipeline</span>
          </div>
        </nav>

        <div className="section" style={{ maxWidth: 860 }}>
          <h1 className="fade-up" style={{ fontSize: 36, fontWeight: 800, marginBottom: 8 }}>
            Build with <span className="grad-text">AI Agents</span>
          </h1>
          <p className="fade-up-1" style={{ color: 'var(--text-muted)', marginBottom: 40 }}>
            Pick a provider — two are completely free. No credit card needed.
          </p>

          {/* Provider + Model selector */}
          <div className="form-group fade-up-1">
            <label className="form-label">Choose Provider & Model</label>
            <div className="model-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))' }}>
              {PROVIDER_MODELS.map(m => (
                <div
                  key={m.model}
                  className={`model-option ${selected.model === m.model ? 'selected' : ''}`}
                  onClick={() => { setSelected(m); setApiKey('') }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
                    <div className="model-name">{m.label}</div>
                    <span className={`badge ${m.badgeColor}`} style={{ fontSize: 10, padding: '2px 6px' }}>{m.badge}</span>
                  </div>
                  <div className="model-desc">{m.desc}</div>
                </div>
              ))}
            </div>
          </div>

          {/* API Key (hidden for Demo mode) */}
          {!isDemo && (
            <div className="form-group fade-up-1">
              <label className="form-label">
                API Key
                {selected.keyLink && (
                  <a href={selected.keyLink} target="_blank" rel="noreferrer"
                    style={{ marginLeft: 8, fontSize: 11, color: 'var(--purple-light)' }}>
                    Get free key →
                  </a>
                )}
              </label>
              <input
                type="password"
                placeholder={selected.keyPlaceholder}
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
              />
              <p className="form-hint">
                {selected.provider === 'gemini' && '✅ Google AI Studio gives a free API key instantly — no credit card.'}
                {selected.provider === 'groq'   && '✅ Groq Console gives a free API key instantly — no credit card.'}
                {selected.provider === 'openai' && '💳 OpenAI requires a paid account. Consider Gemini or Groq instead.'}
              </p>
            </div>
          )}

          {/* Prompt */}
          <div className="form-group fade-up-2">
            <label className="form-label">What do you want to build?</label>
            <textarea
              rows={5}
              placeholder="Describe your project in as much detail as you like..."
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              style={{ resize: 'vertical' }}
            />
            <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {EXAMPLE_PROMPTS.map(ex => (
                <button key={ex} className="btn btn-ghost btn-sm"
                  style={{ fontSize: 11, padding: '5px 10px' }}
                  onClick={() => setPrompt(ex)}>
                  {ex.substring(0, 42)}…
                </button>
              ))}
            </div>
          </div>

          {/* Advanced */}
          <details className="fade-up-3" style={{ marginBottom: 32 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--text-muted)', fontSize: 13, marginBottom: 16 }}>
              Advanced Settings
            </summary>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 }}>
              <div>
                <label className="form-label">Max Review Cycles: {maxRev}</label>
                <input type="range" min={1} max={5} value={maxRev} onChange={e => setMaxRev(+e.target.value)} style={{ padding: 0, height: 6 }} />
              </div>
              <div>
                <label className="form-label">Max Test-Fix Cycles: {maxTest}</label>
                <input type="range" min={1} max={5} value={maxTest} onChange={e => setMaxTest(+e.target.value)} style={{ padding: 0, height: 6 }} />
              </div>
            </div>
          </details>

          <button
            className="btn btn-primary"
            style={{ width: '100%', justifyContent: 'center', fontSize: 16, padding: '16px' }}
            disabled={!prompt.trim() || needsKey}
            onClick={handleSubmit}
          >
            🚀 Launch the AI Team
          </button>
          {needsKey && (
            <p style={{ textAlign: 'center', fontSize: 12, color: 'var(--red)', marginTop: 8 }}>
              Please enter your {selected.label} API key above
            </p>
          )}
        </div>
      </div>
    )
  }


  // ── Running / Done phase — live pipeline view ──────────────
  const completedCount = stages.filter(s => s.status === 'complete').length
  const currentStage   = stages.find(s => s.status === 'running')

  return (
    <div className="page">
      {HITLModal}
      <nav className="navbar">
        <div className="navbar-logo" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
          <div className="logo-icon">🏭</div>
          <span>AI Software Factory</span>
        </div>
        <div className="navbar-links">
          {phase === 'running' && (
            <>
              <span className="spinner" />
              <span style={{ fontSize: 13, color: 'var(--text-muted)', marginLeft: 8 }}>
                {fmtTime(elapsed)} elapsed
              </span>
              <span style={{ fontSize: 13, color: 'var(--text-muted)', marginLeft: 12 }}>
                {completedCount}/7 stages done
              </span>
              <button
                id="stop-pipeline-btn"
                onClick={async () => {
                  if (!runId || stopping) return
                  setStopping(true)
                  addLog('🛑 Stop requested — finishing current stage...')
                  try {
                    await fetch(`/api/runs/${runId}/cancel`, { method: 'POST' })
                  } catch { setStopping(false) }
                }}
                disabled={stopping}
                style={{
                  marginLeft: 16,
                  padding: '5px 14px',
                  borderRadius: 8,
                  border: '1px solid rgba(239,68,68,0.4)',
                  background: stopping ? 'rgba(239,68,68,0.08)' : 'rgba(239,68,68,0.12)',
                  color: stopping ? 'var(--text-muted)' : '#f87171',
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: stopping ? 'not-allowed' : 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  transition: 'all 0.2s',
                }}
              >
                {stopping ? '⌛ Stopping…' : '⏹ Stop'}
              </button>
            </>
          )}
          {phase === 'done' && !error && <span className="badge badge-green">✓ Complete in {fmtTime(elapsed)}</span>}
          {phase === 'done' && error  && <span className="badge badge-red">✗ Failed</span>}
        </div>
      </nav>

      <div className="section" style={{ maxWidth: 760 }}>
        <div className="pipeline-view">

          {/* Header */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
            <h2 style={{ fontSize: 24, fontWeight: 800 }}>
              {phase === 'running' ? '⚙️ Agents are working...' : error ? '❌ Pipeline Failed' : '🎉 Pipeline Complete!'}
            </h2>
            {phase === 'running' && (
              <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 16 }}>
                <div style={{ fontSize: 32, fontWeight: 800, fontFamily: 'var(--font-mono)', background: 'var(--grad)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
                  {fmtTime(elapsed)}
                </div>
                <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1 }}>elapsed</div>
              </div>
            )}
          </div>

          <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 8, fontFamily: 'var(--font-mono)' }}>
            "{prompt.substring(0, 80)}{prompt.length > 80 ? '…' : ''}"
          </p>

          {/* Duration hint — only show after 30s */}
          {phase === 'running' && elapsed >= 30 && (
            <div style={{
              background: 'rgba(124,111,247,.08)', border: '1px solid rgba(124,111,247,.2)',
              borderRadius: 10, padding: '10px 16px', marginBottom: 20,
              fontSize: 13, color: 'var(--purple-light)', display: 'flex', gap: 10, alignItems: 'center'
            }}>
              <span>⏳</span>
              <span>
                <strong>Still working — this is normal!</strong> Real LLM pipelines take <strong>3–8 minutes</strong>.
                {' '}Each of the 7 agents makes its own API call. Stage {completedCount + 1}/7 is running now.
              </span>
            </div>
          )}

          {/* Progress bar */}
          {phase === 'running' && (
            <div style={{ marginBottom: 20 }}>
              <div style={{ height: 4, background: 'var(--bg3)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  height: '100%', borderRadius: 2,
                  background: 'var(--grad)',
                  width: `${Math.max(5, (completedCount / 7) * 100)}%`,
                  transition: 'width 0.8s ease',
                }} />
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                {completedCount} of 7 stages complete
              </div>
            </div>
          )}

          {/* Stage cards */}
          <div className="stages-list">
            {stages.map((s, i) => (
              <div key={s.name} className={`stage-card ${s.status}`} style={{ animationDelay: `${i * 0.05}s` }}>
                <span className="stage-icon">{s.icon}</span>
                <div className="stage-info">
                  <div className="stage-name">{s.name}</div>
                  {s.status === 'running' && (
                    <div className="stage-meta" style={{ color: 'var(--purple-light)' }}>
                      Working… {stageTimer > 5 ? `(${stageTimer}s)` : ''}
                    </div>
                  )}
                  {s.status === 'complete' && (
                    <div className="stage-meta">
                      {s.data ? Object.entries(s.data).map(([k, v]) => `${k}: ${v}`).join(' · ') : ''}
                      {s.duration ? `${s.data ? ' · ' : ''}✓ ${s.duration}s` : ''}
                    </div>
                  )}
                  {s.status === 'pending' && (
                    <div className="stage-meta" style={{ opacity: 0.35 }}>Queued</div>
                  )}
                </div>
                <div className="stage-status">
                  {s.status === 'running'  && <span className="spinner" />}
                  {s.status === 'complete' && <span style={{ color: 'var(--green)',       fontSize: 18 }}>✓</span>}
                  {s.status === 'failed'   && <span style={{ color: 'var(--red)',         fontSize: 18 }}>✗</span>}
                  {s.status === 'pending'  && <span style={{ color: 'var(--text-muted)', fontSize: 18 }}>○</span>}
                </div>
              </div>
            ))}
          </div>

          {/* Current stage detail */}
          {currentStage && (
            <div style={{
              marginTop: 16, padding: '12px 16px',
              background: 'rgba(124,111,247,.06)',
              border: '1px solid rgba(124,111,247,.15)',
              borderRadius: 10, fontSize: 13,
              display: 'flex', alignItems: 'center', gap: 12,
            }}>
              <span className="spinner" />
              <span style={{ color: 'var(--purple-light)' }}>
                <strong>{currentStage.name}</strong> agent is calling the LLM API
                {stageTimer > 10 ? ` — ${stageTimer}s and counting` : ''}...
              </span>
            </div>
          )}

          {/* Log stream */}
          <div style={{ marginTop: 20 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
              Live Agent Log
            </div>
            <div className="log-stream" ref={logRef}>
              {logs.map((l, i) => <div key={i} className="log-entry">{l}</div>)}
              {phase === 'running' && (
                <div className="log-entry" style={{ opacity: 0.4 }}>
                  <span style={{ animation: 'blink 1s infinite' }}>▋</span>
                </div>
              )}
            </div>
          </div>

          {/* Error card */}
          {error && (
            <div className="card" style={{ marginTop: 24, borderColor: 'rgba(239,68,68,.3)', background: 'rgba(239,68,68,.05)' }}>
              <div style={{ color: 'var(--red)', fontWeight: 700, marginBottom: 8 }}>❌ Pipeline Error</div>
              <code style={{ fontSize: 12, color: 'var(--text-dim)', whiteSpace: 'pre-wrap' }}>{error}</code>
              <div style={{ marginTop: 16 }}>
                <button className="btn btn-ghost btn-sm" onClick={() => setPhase('form')}>
                  ← Try Again
                </button>
              </div>
            </div>
          )}

          {/* Complete */}
          {phase === 'done' && !error && runId && (
            <div style={{ textAlign: 'center', marginTop: 32 }}>
              <div style={{ fontSize: 48, marginBottom: 8 }}>🎉</div>
              <div style={{ fontWeight: 700, fontSize: 20, marginBottom: 4 }}>Done in {fmtTime(elapsed)}!</div>
              <p style={{ color: 'var(--text-muted)', marginBottom: 20, fontSize: 14 }}>Redirecting to your generated project…</p>
              <button className="btn btn-primary" style={{ fontSize: 16, padding: '12px 28px' }} onClick={() => navigate(`/result/${runId}`)}>
                View Generated Project →
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
