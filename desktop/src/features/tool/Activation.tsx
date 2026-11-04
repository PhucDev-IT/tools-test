import { useEffect, useState } from 'react'
import { KeyRound } from 'lucide-react'
import { useApp } from '../../app/store'
import { ActionButton, ErrorLine, Input } from '../../app/ui'
import { call } from '../../services/core'

const HINTS: Record<string, string> = {
  ACTIVATION_KEY_INVALID: 'Mã không đúng. Kiểm tra lại mã được cấp.',
  ACTIVATION_KEY_USED: 'Mã này đã dùng cho máy khác. Mỗi mã chỉ kích hoạt một lần.',
  LICENSE_EXPIRED: 'License đã hết hạn hoặc bị khóa.',
  AGENT_LIMIT_REACHED: 'Tài khoản đã đạt số máy tối đa của license.',
  CLOUD_UNREACHABLE: 'Không kết nối được Cloud. Kiểm tra Internet và Cloud URL.',
  CLOUD_TIMEOUT: 'Cloud phản hồi quá chậm. Thử lại sau.',
}

/** First run only: Cloud URL + one-time activation key. */
export function Activation() {
  const { state, refresh, refreshConfig, notify } = useApp()
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')
  const [error, setError] = useState<{ code: string; message: string } | null>(null)
  const savedUrl = state.config?.cloud.api_url

  useEffect(() => {
    if (savedUrl) setUrl(savedUrl)
  }, [savedUrl])

  const activate = async () => {
    setError(null)
    try {
      await call('activation.activate', { activation_key: key.trim(), cloud_url: url.trim() })
      setKey('')
      notify('success', 'Kích hoạt thành công')
      await Promise.all([refresh(), refreshConfig()])
      await call('agent.start').catch(() => undefined)  // a fresh activation should go online right away
      await refresh()
    } catch (err) {
      const e = err as { code: string; message: string }
      setError({ code: e.code, message: HINTS[e.code] ?? e.message })
    }
  }

  return (
    <section className="panel">
      <h2 className="panel-title"><KeyRound size={15} />Kích hoạt máy này</h2>
      <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Cloud URL, ví dụ https://app.sarae.vn/api" spellCheck={false} />
      <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Mã kích hoạt SARAE-ACT-…" autoComplete="off" spellCheck={false} />
      <ErrorLine error={error} />
      <ErrorLine error={state.status?.agent.credential_error} />
      <ActionButton variant="primary" disabled={!key.trim() || !url.trim()} onAction={activate}>Kích hoạt</ActionButton>
    </section>
  )
}
