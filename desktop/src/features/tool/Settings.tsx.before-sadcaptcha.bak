import { useEffect, useState } from 'react'
import { LogOut, Settings2 } from 'lucide-react'
import { useApp } from '../../app/store'
import { ActionButton, Input, Toggle } from '../../app/ui'
import { call } from '../../services/core'
import type { MethodParams, TemplateInfo } from '../../types/ipc'

/** Rarely changed, folded by default: startup behaviour, OBS connection, this device. */
export function Settings() {
  const { state, run, refresh, refreshConfig } = useApp()
  const config = state.config
  const agent = state.status?.agent
  const monitor = state.status?.monitor
  const [host, setHost] = useState('')
  const [port, setPort] = useState(4455)
  const [password, setPassword] = useState('')
  const [mediaInput, setMediaInput] = useState('Sarae Video')
  const [dualMode, setDualMode] = useState(false)
  const [sourceA, setSourceA] = useState('')
  const [sourceB, setSourceB] = useState('')
  const [cameraInput, setCameraInput] = useState('')
  const [templates, setTemplates] = useState<TemplateInfo[]>([])

  useEffect(() => {
    void call('monitor.templates.list').then(result => setTemplates(result.templates)).catch(() => undefined)
  }, [agent?.state])

  const uploadTemplate = async (name: string, file?: File) => {
    if (!file) return
    const image_base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onerror = () => reject(new Error('Không đọc được ảnh mẫu'))
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '')
      reader.readAsDataURL(file)
    })
    const result = await run(() => call('monitor.templates.set_custom', { name, image_base64 }))
    if (result) setTemplates(result.templates)
    await refresh()
  }

  useEffect(() => {
    if (!config) return
    setHost(config.obs.host)
    setPort(config.obs.port)
    setMediaInput(config.obs.media_input)
    setDualMode(config.obs.dual_mode)
    setSourceA(config.obs.source_a)
    setSourceB(config.obs.source_b)
    setCameraInput(config.obs.camera_input ?? '')
  }, [config])

  if (!config || !agent) return null
  const workerActive = agent.state !== 'stopped' && agent.state !== 'error'
  const setConfig = (patch: MethodParams<'config.set'>) =>
    run(() => call('config.set', patch)).then(refreshConfig).then(refresh)

  return (
    <details className="panel settings">
      <summary className="panel-title"><Settings2 size={15} />Cài đặt</summary>

      <div className="settings-group">
        <h3>Khi mở ứng dụng</h3>
        <Toggle checked={config.agent.autostart} label="Tự kết nối Cloud" onChange={(v) => void setConfig({ agent: { autostart: v } })} />
        <Toggle checked={config.obs.auto_connect} label="Tự kết nối OBS" onChange={(v) => void setConfig({ obs: { auto_connect: v } })} />
        <Toggle checked={config.monitor.autostart} label="Tự bật giám sát CAPTCHA" onChange={(v) => void setConfig({ monitor: { autostart: v } })} />
      </div>

      <div className="settings-group">
        <h3>Built-in Templates — {monitor?.builtins_ready ? 'Ready' : 'Missing'}</h3>
        <p className="muted small">Lần chụp gần nhất: {monitor?.state.last_scan ? new Date(monitor.state.last_scan).toLocaleString('vi-VN') : 'Chưa chụp'} · Số màn hình: {monitor?.state.monitors_detected ?? 'Chưa kiểm tra'} · Cửa sổ: {monitor?.state.window_title || 'Chưa tìm thấy'}</p>
        <p className="muted small">Chu kỳ kiểm tra: {config.monitor.interval_seconds} giây. Telegram được cấu hình theo từng OWNER trên Cloud.</p>
        <details>
          <summary>Lịch sử giao diện xác minh</summary>
          {monitor?.events.filter(event => event.kind === 'verification_detected' || event.kind === 'verification_cleared' || event.kind === 'captcha_detected' || event.kind === 'captcha_cleared').map(event => (
            <p className="muted small" key={event.id}>{new Date(event.at).toLocaleString('vi-VN')} · {event.kind} · {event.detail}</p>
          ))}
        </details>
        <p className="muted small">Ảnh mẫu tích hợp được nạp tự động. Trạng thái từng ảnh:</p>
        {templates.filter(t => t.source !== 'cloud').map(template => (
          <div className="inline" key={template.name}>
            <span>{template.name}: {template.status} ({template.source})</span>
            {template.source === 'custom' && <ActionButton onAction={async () => {
              const result = await run(() => call('monitor.templates.reset', { name: template.name }))
              if (result) setTemplates(result.templates)
              await refresh()
            }}>Reset to Default</ActionButton>}
          </div>
        ))}
        <details>
          <summary>Advanced Settings — Custom Templates</summary>
          {templates.filter(t => t.source !== 'cloud').map(template => (
            <label key={template.name} className="inline">{template.name}
              <input type="file" accept="image/png" onChange={e => void uploadTemplate(template.name, e.target.files?.[0])} />
            </label>
          ))}
        </details>
      </div>

      <div className="settings-group">
        <h3>OBS WebSocket</h3>
        <div className="inline">
          <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="127.0.0.1" spellCheck={false} />
          <Input type="number" min={1} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value))} className="input input-num" />
        </div>
        <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off"
          placeholder={config.obs.password_set ? 'Mật khẩu đã lưu (để trống = giữ nguyên)' : 'Mật khẩu (nếu OBS bật xác thực)'} />
        <h4>Media Source phát video</h4>
        <Input value={mediaInput} onChange={(e) => setMediaInput(e.target.value)} placeholder="Sarae Video" />
        <Toggle checked={dualMode} label="Phát A/B (hai Media Source luân phiên)" onChange={setDualMode} />
        {dualMode && <div className="inline">
          <Input value={sourceA} onChange={(e) => setSourceA(e.target.value)} placeholder={mediaInput + ' A'} />
          <Input value={sourceB} onChange={(e) => setSourceB(e.target.value)} placeholder={mediaInput + ' B'} />
        </div>}
        <h4>Camera dự phòng (Video Capture Device trong cùng scene)</h4>
        <Input value={cameraInput} onChange={(e) => setCameraInput(e.target.value)} placeholder="Tên nguồn camera trong OBS" />
        <ActionButton disabled={!host.trim() || !mediaInput.trim() || (dualMode && (sourceA.trim() || mediaInput.trim() + ' A') === (sourceB.trim() || mediaInput.trim() + ' B'))} onAction={async () => {
          await setConfig({ obs: { host: host.trim(), port, media_input: mediaInput.trim(), dual_mode: dualMode, source_a: sourceA.trim(), source_b: sourceB.trim(), camera_input: cameraInput.trim() }, ...(password ? { obs_password: password } : {}) })
          setPassword('')
          await run(() => call('obs.connect'), 'Đã kết nối OBS')
          await refresh()
        }}>Lưu &amp; kết nối</ActionButton>
      </div>

      {agent.activated && (
        <div className="settings-group">
          <h3>Thiết bị</h3>
          <p className="muted small selectable">{agent.activated_cloud_url ?? config.cloud.api_url}<br />{agent.agent_id}</p>
          <ActionButton variant="danger" icon={<LogOut size={14} />} disabled={workerActive} onAction={async () => {
            if (!window.confirm('Gỡ kích hoạt trên máy này? Cần mã kích hoạt mới để dùng lại.')) return
            await run(() => call('activation.deactivate'), 'Đã gỡ kích hoạt')
            await refresh()
          }}>Gỡ kích hoạt</ActionButton>
          {workerActive && <small className="muted">Tắt "Kết nối Cloud" trước khi gỡ kích hoạt.</small>}
        </div>
      )}
    </details>
  )
}
