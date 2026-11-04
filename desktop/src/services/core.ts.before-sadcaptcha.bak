// Typed bridge to the Agent Core. The GUI never talks to OBS, the Cloud or the filesystem
// itself: every action is a whitelisted Core method executed by Python.
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import type {
  AgentErrorInfo, CoreEventFrame, CoreProcessState, EventName, MethodName, MethodParams, MethodResult,
} from '../types/ipc'

export class CoreError extends Error {
  readonly code: string
  readonly details?: Record<string, unknown>

  constructor(info: AgentErrorInfo) {
    super(info.message)
    this.code = info.code
    this.details = info.details
  }
}

function toCoreError(raw: unknown): CoreError {
  if (raw && typeof raw === 'object' && 'code' in raw && 'message' in raw) {
    return new CoreError(raw as AgentErrorInfo)
  }
  return new CoreError({ code: 'GUI_ERROR', message: String(raw) })
}

// Long-running work (cloud media cache downloads) is async on the Core side and reported by events, so the
// default timeout stays short; network-bound calls get more time.
const SLOW_METHODS: Partial<Record<MethodName, number>> = {
  'autopin.open': 60_000,
  'autopin.login_done': 75_000,
  'autopin.start': 75_000,
  'autopin.scan': 60_000,
  'tiktok.products.sync': 150_000,
  'activation.activate': 45_000,
  'obs.connect': 20_000,
  'voice.speak': 60_000,
  'diagnostics.export': 60_000,
  'agent.stop': 30_000,
  'agent.restart': 40_000,
  'monitor.stop': 15_000,
}

type CallArgs<M extends MethodName> = Record<never, never> extends MethodParams<M> ? [params?: MethodParams<M>] : [params: MethodParams<M>]

export async function call<M extends MethodName>(method: M, ...args: CallArgs<M>): Promise<MethodResult<M>> {
  try {
    return await invoke<MethodResult<M>>('core_request', {
      method,
      params: args[0] ?? {},
      timeoutMs: SLOW_METHODS[method] ?? 30_000,
    })
  } catch (raw) {
    throw toCoreError(raw)
  }
}

export const coreProcess = {
  state: () => invoke<CoreProcessState>('core_state'),
  start: () => invoke<CoreProcessState>('core_start'),
  stop: () => invoke<CoreProcessState>('core_stop'),
  restart: () => invoke<CoreProcessState>('core_restart'),
  stderr: () => invoke<string[]>('core_stderr'),
}

export function onCoreEvent(handler: (frame: CoreEventFrame) => void): Promise<UnlistenFn> {
  return listen<CoreEventFrame>('core://event', (e) => handler(e.payload))
}

export function onCoreState(handler: (state: CoreProcessState) => void): Promise<UnlistenFn> {
  return listen<CoreProcessState>('core://state', (e) => handler(e.payload))
}

export function isEvent<E extends EventName>(frame: CoreEventFrame, name: E): frame is CoreEventFrame<E> {
  return frame.event === name
}
