// Single-screen tool: on/off switches with one status line each, the live log, and settings folded away.
// Everything an OWNER manages (products, sessions, playlists, LIVE control) lives on the Sarae Web.
import { AlertTriangle, CheckCircle2, Info, RefreshCw, ScanEye, X, XCircle } from 'lucide-react'
import { useApp } from './store'
import { Badge, Button, type Tone } from './ui'
import { Activation } from '../features/tool/Activation'
import { Controls } from '../features/tool/Controls'
import { LogPanel } from '../features/tool/LogPanel'
import { Settings } from '../features/tool/Settings'
import { coreProcess } from '../services/core'
import type { AgentState } from '../types/ipc'

const AGENT_LABEL: Record<AgentState, [string, Tone]> = {
  stopped: ['Đã dừng', 'muted'],
  starting: ['Đang kết nối', 'info'],
  running: ['Đang chạy', 'ok'],
  disconnected: ['Mất kết nối', 'warn'],
  error: ['Lỗi', 'danger'],
  stopping: ['Đang dừng', 'info'],
}

export function App() {
  const { state, ready } = useApp()
  const status = state.status
  const agent = status?.agent

  return (
    <div className="tool">
      <header className="tool-head">
        <div className="brand-mark">S</div>
        <div className="tool-title">
          <strong>Sarae Agent</strong>
          <small>{status?.core.version ? `v${status.core.version}` : '—'}{agent?.hostname ? ` · ${agent.hostname}` : ''}</small>
        </div>
        {agent && <Badge tone={AGENT_LABEL[agent.state][1]}>{AGENT_LABEL[agent.state][0]}</Badge>}
      </header>

      <CoreBanner />
      <CaptchaBanner />

      {!ready && !status ? <Booting /> : (
        <main className="tool-body">
          {agent && !agent.activated ? <Activation /> : <Controls />}
          <LogPanel />
          <Settings />
        </main>
      )}
      <Toasts />
    </div>
  )
}

function CoreBanner() {
  const { state, run } = useApp()
  const { phase, last_error, restarts } = state.core
  if (phase === 'ready' || phase === 'starting') return null
  return (
    <div className={`banner ${phase === 'crashed' ? 'banner-danger' : 'banner-info'}`}>
      <AlertTriangle size={16} />
      <div>
        <strong>{phase === 'crashed' ? 'Agent Core dừng bất thường' : phase === 'stopping' ? 'Agent Core đang dừng…' : 'Agent Core chưa chạy'}</strong>
        {last_error && <p>{last_error.message} <code>{last_error.code}</code></p>}
        {restarts > 0 && <p>Đã tự khởi động lại {restarts} lần.</p>}
      </div>
      {phase !== 'stopping' && (
        <Button variant="primary" icon={<RefreshCw size={14} />} onClick={() => void run(() => coreProcess.restart(), 'Đã khởi động lại Agent Core')}>
          Khởi động lại
        </Button>
      )}
    </div>
  )
}

function CaptchaBanner() {
  const { state, clearCaptcha } = useApp()
  if (!state.captcha) return null
  return (
    <div className="banner banner-danger pulse">
      <ScanEye size={16} />
      <div>
        <strong>{'kind' in state.captcha ? ({ INACTIVITY_WARNING: 'LIVE yêu cầu xác minh do không hoạt động', CAPTCHA_CHALLENGE: 'Phát hiện hộp CAPTCHA', UNKNOWN_VERIFICATION: 'Giao diện xác minh chưa phân loại' }[state.captcha.kind]) : 'Phát hiện hộp CAPTCHA'} trên TikTok LIVE Studio</strong>
        <p>Lúc {new Date(state.captcha.at).toLocaleTimeString('vi-VN')}. Hãy xử lý trực tiếp trên máy.</p>
      </div>
      <Button variant="ghost" icon={<X size={14} />} onClick={clearCaptcha} aria-label="Ẩn" />
    </div>
  )
}

function Booting() {
  const { state } = useApp()
  return (
    <div className="booting">
      <div className="booting-ring" />
      <strong>{state.core.phase === 'crashed' ? 'Agent Core không khởi động được' : 'Đang khởi động…'}</strong>
    </div>
  )
}

function Toasts() {
  const { state, dismiss } = useApp()
  return (
    <div className="toasts">
      {state.toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          {t.kind === 'success' ? <CheckCircle2 size={16} /> : t.kind === 'error' ? <XCircle size={16} /> : <Info size={16} />}
          <div>
            <strong>{t.title}</strong>
            {t.detail && <code>{t.detail}</code>}
          </div>
          <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="Đóng"><X size={14} /></button>
        </div>
      ))}
    </div>
  )
}
