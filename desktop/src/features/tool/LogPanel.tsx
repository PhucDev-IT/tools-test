import { useMemo, useState } from 'react'
import { revealItemInDir } from '@tauri-apps/plugin-opener'
import { Download } from 'lucide-react'
import { useApp } from '../../app/store'
import { ActionButton } from '../../app/ui'
import { call } from '../../services/core'

/** Newest first; "Lỗi" keeps warnings and errors only. Export zips logs + masked config for support. */
export function LogPanel() {
  const { state, run } = useApp()
  const [errorsOnly, setErrorsOnly] = useState(false)
  const logs = useMemo(() => state.logs
    .filter((l) => !errorsOnly || l.level === 'WARNING' || l.level === 'ERROR' || l.level === 'CRITICAL')
    .slice(-300)
    .reverse(), [state.logs, errorsOnly])

  return (
    <section className="panel panel-log">
      <div className="panel-head">
        <h2 className="panel-title">Nhật ký</h2>
        <div className="segmented">
          <button className={!errorsOnly ? 'is-active' : ''} onClick={() => setErrorsOnly(false)}>Tất cả</button>
          <button className={errorsOnly ? 'is-active' : ''} onClick={() => setErrorsOnly(true)}>Lỗi</button>
        </div>
        <ActionButton variant="ghost" icon={<Download size={14} />} aria-label="Xuất gói chẩn đoán" title="Xuất gói chẩn đoán" onAction={async () => {
          const result = await run(() => call('diagnostics.export'), 'Đã tạo gói chẩn đoán')
          if (result) await run(() => revealItemInDir(result.path))
        }} />
      </div>
      <div className="log-list">
        {logs.length === 0 ? <div className="log-empty">Chưa có nhật ký</div> : logs.map((l, i) => (
          <div key={`${l.ts}-${i}`} className={`log-row log-${l.level.toLowerCase()}`}>
            <span className="log-ts">{new Date(l.ts).toLocaleTimeString('vi-VN')}</span>
            <span className="log-msg">{l.message}{l.code !== '-' && <code>{l.code}</code>}</span>
          </div>
        ))}
      </div>
    </section>
  )
}
