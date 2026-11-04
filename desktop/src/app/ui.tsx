import { useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'muted' | 'accent'

export function Badge({ tone = 'muted', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}><i className="badge-dot" />{children}</span>
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  icon?: ReactNode
  busy?: boolean
}

export function Button({ variant = 'secondary', icon, busy, children, disabled, ...rest }: ButtonProps) {
  return (
    <button className={`btn btn-${variant}${children ? '' : ' btn-icon'}`} disabled={disabled || busy} {...rest}>
      {busy ? <Loader2 className="spin" size={14} /> : icon}
      {children && <span>{children}</span>}
    </button>
  )
}

/** Button whose async handler disables it until done (prevents double submission). */
export function ActionButton({ onAction, ...props }: Omit<ButtonProps, 'onClick' | 'busy'> & { onAction: () => Promise<unknown> }) {
  const [busy, setBusy] = useState(false)
  return (
    <Button
      {...props}
      busy={busy}
      onClick={async () => {
        setBusy(true)
        try {
          await onAction()
        } finally {
          setBusy(false)
        }
      }}
    />
  )
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="input" {...props} />
}

/** Labelled checkbox-style toggle for settings. */
export function Toggle({ checked, onChange, label, disabled }: {
  checked: boolean; onChange: (value: boolean) => void; label: ReactNode; disabled?: boolean
}) {
  return (
    <label className={`toggle ${disabled ? 'is-disabled' : ''}`}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track"><span className="toggle-thumb" /></span>
      <span>{label}</span>
    </label>
  )
}

/** The main on/off switch of a row. Stays disabled while its Core call runs, so a double click never sends two. */
export function Switch({ checked, onToggle, label, disabled }: {
  checked: boolean; onToggle: (value: boolean) => Promise<unknown>; label: string; disabled?: boolean
}) {
  const [busy, setBusy] = useState(false)
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`switch ${checked ? 'is-on' : ''} ${busy ? 'is-busy' : ''}`}
      disabled={disabled || busy}
      onClick={async () => {
        setBusy(true)
        try {
          await onToggle(!checked)
        } finally {
          setBusy(false)
        }
      }}
    >
      <span className="switch-thumb">{busy && <Loader2 className="spin" size={10} />}</span>
    </button>
  )
}

export function ErrorLine({ error }: { error?: { code: string; message: string } | null }) {
  if (!error) return null
  return (
    <div className="error-line">
      <span>{error.message}</span>
      <code>{error.code}</code>
    </div>
  )
}
