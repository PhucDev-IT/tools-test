import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from 'react'
import { call, CoreError, coreProcess, isEvent, onCoreEvent, onCoreState } from '../services/core'
import type {
  AgentConfigView, CommandEntry, CoreEvents, CoreProcessState, LogEntry, SystemStatus,
} from '../types/ipc'

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'info'
  title: string
  detail?: string
}

interface State {
  core: CoreProcessState
  status: SystemStatus | null
  config: AgentConfigView | null
  logs: LogEntry[]
  commands: CommandEntry[]
  captcha: CoreEvents['monitor.verification_detected'] | CoreEvents['monitor.captcha_detected'] | null
  toasts: Toast[]
}

type Action =
  | { type: 'core'; core: CoreProcessState }
  | { type: 'status'; status: SystemStatus }
  | { type: 'patch'; patch: Partial<SystemStatus> }
  | { type: 'config'; config: AgentConfigView }
  | { type: 'logs'; logs: LogEntry[]; replace?: boolean }
  | { type: 'command'; entry: CommandEntry }
  | { type: 'captcha'; alert: CoreEvents['monitor.verification_detected'] | CoreEvents['monitor.captcha_detected'] | null }
  | { type: 'toast'; toast: Toast }
  | { type: 'dismiss'; id: number }

const initial: State = {
  core: { phase: 'starting', pid: null, generation: 0, version: null, last_error: null, exit_code: null, restarts: 0 },
  status: null,
  config: null,
  logs: [],
  commands: [],
  captcha: null,
  toasts: [],
}

const MAX_LOGS = 800

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'core':
      return { ...state, core: action.core }
    case 'status':
      return { ...state, status: action.status, commands: mergeCommands(state.commands, action.status.cloud.recent_commands ?? []) }
    case 'patch':
      return state.status ? { ...state, status: { ...state.status, ...action.patch } } : state
    case 'config':
      return { ...state, config: action.config }
    case 'logs': {
      const logs = action.replace ? action.logs : [...state.logs, ...action.logs]
      return { ...state, logs: logs.slice(-MAX_LOGS) }
    }
    case 'command':
      return { ...state, commands: mergeCommands(state.commands, [action.entry]) }
    case 'captcha':
      return { ...state, captcha: action.alert }
    case 'toast':
      return { ...state, toasts: [...state.toasts.slice(-3), action.toast] }
    case 'dismiss':
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) }
  }
}

function mergeCommands(current: CommandEntry[], incoming: CommandEntry[]): CommandEntry[] {
  const byId = new Map(current.map((c) => [c.command_id, c]))
  for (const entry of incoming) byId.set(entry.command_id, { ...byId.get(entry.command_id), ...entry })
  return [...byId.values()].sort((a, b) => b.received_at.localeCompare(a.received_at)).slice(0, 50)
}

interface AppApi {
  state: State
  ready: boolean
  refresh: () => Promise<void>
  refreshConfig: () => Promise<void>
  notify: (kind: Toast['kind'], title: string, detail?: string) => void
  dismiss: (id: number) => void
  /** Run a Core action with busy-safe error reporting. Returns undefined on failure. */
  run: <T>(action: () => Promise<T>, success?: string) => Promise<T | undefined>
  clearCaptcha: () => void
}

const AppContext = createContext<AppApi | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initial)
  const toastId = useRef(0)
  const ready = state.core.phase === 'ready'

  const notify = useCallback((kind: Toast['kind'], title: string, detail?: string) => {
    const id = ++toastId.current
    dispatch({ type: 'toast', toast: { id, kind, title, detail } })
    window.setTimeout(() => dispatch({ type: 'dismiss', id }), kind === 'error' ? 9000 : 4500)
  }, [])

  const refresh = useCallback(async () => {
    try {
      dispatch({ type: 'status', status: await call('system.status') })
    } catch {
      // Core not ready yet: core://state drives the banner.
    }
  }, [])

  const refreshConfig = useCallback(async () => {
    try {
      dispatch({ type: 'config', config: await call('config.get') })
    } catch { /* see refresh */ }
  }, [])

  const run = useCallback(async <T,>(action: () => Promise<T>, success?: string) => {
    try {
      const result = await action()
      if (success) notify('success', success)
      return result
    } catch (error) {
      const err = error instanceof CoreError ? error : new CoreError({ code: 'GUI_ERROR', message: String(error) })
      notify('error', err.message, err.code)
      return undefined
    }
  }, [notify])

  // Core process lifecycle + event stream.
  useEffect(() => {
    let disposed = false
    const unsubs: Array<() => void> = []
    coreProcess.state().then((core) => !disposed && dispatch({ type: 'core', core })).catch(() => undefined)
    onCoreState((core) => dispatch({ type: 'core', core })).then((u) => unsubs.push(u))
    onCoreEvent((frame) => {
      if (isEvent(frame, 'core.ready')) {
        dispatch({ type: 'status', status: frame.data.status })
      } else if (isEvent(frame, 'agent.status_changed')) {
        dispatch({ type: 'patch', patch: { agent: frame.data } })
      } else if (isEvent(frame, 'cloud.connection_changed')) {
        void refresh()
      } else if (isEvent(frame, 'obs.status_changed')) {
        dispatch({ type: 'patch', patch: { obs: frame.data } })
      } else if (isEvent(frame, 'playback.changed')) {
        dispatch({ type: 'patch', patch: { playback: frame.data } })
      } else if (isEvent(frame, 'media.cache_changed')) {
        dispatch({ type: 'patch', patch: { media: { cloud_cache: frame.data } } })
      } else if (isEvent(frame, 'monitor.status_changed')) {
        dispatch({ type: 'patch', patch: { monitor: frame.data } })
      } else if (isEvent(frame, 'command.received') || isEvent(frame, 'command.finished')) {
        dispatch({ type: 'command', entry: frame.data })
      } else if (isEvent(frame, 'monitor.verification_detected') || isEvent(frame, 'monitor.captcha_detected')) {
        dispatch({ type: 'captcha', alert: frame.data })
      } else if (isEvent(frame, 'monitor.verification_cleared') || isEvent(frame, 'monitor.captcha_cleared')) {
        dispatch({ type: 'captcha', alert: null })
      } else if (isEvent(frame, 'log.entry')) {
        dispatch({ type: 'logs', logs: [frame.data] })
      }
    }).then((u) => unsubs.push(u))
    return () => {
      disposed = true
      unsubs.forEach((u) => u())
    }
  }, [refresh])

  // Initial load once the Core is ready, plus a slow safety refresh (events do the real work).
  useEffect(() => {
    if (!ready) return
    void refresh()
    void refreshConfig()
    call('logs.recent', { limit: 300 }).then((r) => dispatch({ type: 'logs', logs: r.entries, replace: true })).catch(() => undefined)
    const timer = window.setInterval(() => void refresh(), 10_000)
    return () => window.clearInterval(timer)
  }, [ready, state.core.generation, refresh, refreshConfig])

  const api = useMemo<AppApi>(() => ({
    state, ready, refresh, refreshConfig, notify, run,
    dismiss: (id) => dispatch({ type: 'dismiss', id }),
    clearCaptcha: () => dispatch({ type: 'captcha', alert: null }),
  }), [state, ready, refresh, refreshConfig, notify, run])

  return <AppContext.Provider value={api}>{children}</AppContext.Provider>
}

export function useApp(): AppApi {
  const value = useContext(AppContext)
  if (!value) throw new Error('useApp outside AppProvider')
  return value
}
