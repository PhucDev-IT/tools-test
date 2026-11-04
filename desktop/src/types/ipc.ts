// IPC contract v1 between the GUI and the Python Agent Core. Source of truth:
// docs/WINDOWS_AGENT_IPC.md and agent-core/src/sarae_agent/ipc/methods.py.

export const IPC_PROTOCOL_VERSION = 1

export interface AgentErrorInfo {
  code: string
  message: string
  details?: Record<string, unknown>
}

export type CorePhase = 'stopped' | 'starting' | 'ready' | 'stopping' | 'crashed'

export interface CoreProcessState {
  phase: CorePhase
  pid: number | null
  generation: number
  version: string | null
  last_error: AgentErrorInfo | null
  exit_code: number | null
  restarts: number
}

export type AgentState = 'stopped' | 'starting' | 'running' | 'disconnected' | 'error' | 'stopping'

export interface AgentStatus {
  state: AgentState
  error: AgentErrorInfo | null
  activated: boolean
  agent_id: string | null
  activated_cloud_url: string | null
  activated_at: string | null
  hostname: string
  credential_error: AgentErrorInfo | null
}

export type CommandStatus = 'accepted' | 'running' | 'succeeded' | 'failed'

export interface CommandEntry {
  command_id: string
  kind: string
  status: CommandStatus
  received_at: string
  finished_at?: string
  duration_ms?: number
  error?: AgentErrorInfo | null
}

export interface CloudStatus {
  connected: boolean
  state: 'idle' | 'connecting' | 'connected' | 'disconnected' | 'rejected' | 'stopped'
  api_url?: string
  agent_id?: string
  last_ok?: string | null
  last_error?: AgentErrorInfo | null
  next_retry_seconds?: number | null
  server_time?: string
  pending_acks?: number
  commands_in_flight?: number
  recent_commands?: CommandEntry[]
}

export interface ObsMediaStatus {
  state: string | null
  duration_ms: number | null
  cursor_ms: number | null
}

export interface ObsStatus {
  connected: boolean
  state: 'disconnected' | 'connecting' | 'connected' | 'error'
  error: AgentErrorInfo | null
  obs_version: string | null
  websocket_version: string | null
  host: string
  port: number
  media_input: string
  voice_input: string
  password_set: boolean
  auto_connect: boolean
  media_input_ok?: boolean | null
  voice_input_ok?: boolean | null
  media?: ObsMediaStatus | null
}

/** Cloud media cache job (media.cloud_cache.prepare): Media Library videos the Agent plays offline. */
export interface CloudCacheStatus {
  state: 'IDLE' | 'PREPARING' | 'READY' | 'FAILED'
  session_id?: number
  ready?: Array<{ media_id: string; size_bytes: number }>
  failed?: Array<{ media_id?: string; error: string }>
  products_without_video?: number[]
  total?: number
  bytes_total?: number
  bytes_done?: number
  current?: string | null
}

export interface MediaSummary {
  cloud_cache: CloudCacheStatus
}

export interface PlaybackStatus {
  source_mode?: 'video' | 'camera'
  active: boolean
  paused: boolean
  loop: boolean
  media_ids: string[]
  index: number
  count: number
  current_media_id: string | null
  current_filename: string | null
  media_state: string | null
  skipped: string[]
  last_error: AgentErrorInfo | null
  started_at: string | null
  needs_manual_resume: boolean
  source: string
}

export interface MonitorWindowInfo {
  hwnd: number
  pid: number
  exe_name: string
  exe_path: string
  title: string
  minimized: boolean
}

export interface MonitorState {
  running: boolean
  status: string
  window_found: boolean
  window_visible: boolean
  window_title: string
  window_info: MonitorWindowInfo | null
  process_running: boolean | null
  last_scan: string | null
  error: string | null
  events_count: number
  match_score: number | null
  matched_template: string | null
  monitors_detected: number | null
}

export interface MonitorConfig {
  interval_seconds: number
  threshold: number
  autostart: boolean
}

export interface MonitorEvent {
  id: number
  at: string
  kind: string
  detail: string
  image: string | null
}

export interface MonitorStatus {
  state: MonitorState
  config: MonitorConfig
  events: MonitorEvent[]
  templates: number
  builtins_ready: boolean
}

export interface CoreInfo {
  version: string
  ipc_protocol: number
  pid: number
  started_at: string
  data_root: string
  hostname: string
}

export interface AutoPinStatus {
  state: string
  enabled: boolean
  interval_seconds: number
  products: string[]
  current_index: number
  last_pin: string
  last_error: string
  /** The Playwright-controlled Chrome (step 2) is open. */
  browser_open: boolean
  /** The plain Chrome of step 1 (manual sign-in) is still running on the shared profile. */
  login_open: boolean
  /** Products read from the LIVE Console by the last scan. */
  found: number
}

export interface AutoPinProduct {
  name: string
  pinned: boolean
}

export interface SystemStatus {
  core: CoreInfo
  agent: AgentStatus
  cloud: CloudStatus
  obs: ObsStatus
  media: MediaSummary
  playback: PlaybackStatus
  monitor: MonitorStatus
  auto_pin?: AutoPinStatus
}

export interface AgentConfigView {
  version: number
  cloud: { api_url: string; heartbeat_seconds: number; poll_seconds: number; request_timeout_seconds: number }
  obs: { host: string; port: number; media_input: string; voice_input: string; dual_mode: boolean; source_a: string; source_b: string; camera_input: string; auto_connect: boolean; password_set: boolean }
  monitor: MonitorConfig
  agent: { autostart: boolean }
  paths: { data_root: string; logs: string; templates: string }
}

export interface LogEntry {
  ts: string
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
  logger: string
  code: string
  correlation_id: string
  message: string
}

export interface TemplateInfo {
  name: string
  bytes: number
  source: 'custom' | 'built-in' | 'cloud'
  status: 'Ready' | 'Missing' | 'Invalid'
}

export interface SadCaptchaStatus {
  key_configured: boolean
  attempts: number
  remaining_local_budget: number
  max_attempts: number
  last: SadCaptchaAnalysis | null
}

export interface SadCaptchaAnalysis {
  kind: 'shapes' | 'puzzle'
  state: 'REQUESTED' | 'FAILED' | 'ANALYSIS_READY'
  attempt: number
  duration_ms?: number
  result?: Record<string, number>
  error?: string
  remaining_local_budget?: number
}

export type Voice = 'vi-VN-HoaiMyNeural' | 'vi-VN-NamMinhNeural'

type NoParams = Record<never, never>

/** method -> [params, result] */
export interface CoreMethods {
  'system.status': [NoParams, SystemStatus]
  'autopin.status': [NoParams, AutoPinStatus]
  'autopin.open': [{url:string}, AutoPinStatus]
  'autopin.login': [{url?:string}, AutoPinStatus]
  'autopin.login_done': [NoParams, AutoPinStatus]
  'autopin.scan': [NoParams, AutoPinStatus & { products_found: AutoPinProduct[] }]
  'tiktok.products.sync': [NoParams, {agent_id:string;scanned_at:string;imported:{created:number;updated:number;shops:number};summary:{tiktok:number;sarae:number;matched:number;unmatched_tiktok:number;ambiguous:number;missing_in_tiktok:number}}]
  'autopin.configure': [{products:string[];interval_seconds:number}, AutoPinStatus]
  'autopin.start': [NoParams, AutoPinStatus]
  'autopin.stop': [NoParams, AutoPinStatus]
  'system.ping': [NoParams, { pong: boolean }]
  'system.methods': [NoParams, { methods: string[]; cloud_command_kinds: string[] }]
  'system.shutdown': [NoParams, { accepted: boolean }]
  'config.get': [NoParams, AgentConfigView]
  'config.set': [{
    cloud?: Partial<AgentConfigView['cloud']>
    obs?: Partial<Omit<AgentConfigView['obs'], 'password_set'>>
    monitor?: Partial<MonitorConfig>
    agent?: Partial<AgentConfigView['agent']>
    obs_password?: string
    clear_obs_password?: boolean
  }, AgentConfigView]
  'activation.status': [NoParams, Omit<AgentStatus, 'state' | 'error'>]
  'activation.activate': [{ activation_key: string; cloud_url?: string }, Omit<AgentStatus, 'state' | 'error'>]
  'activation.deactivate': [NoParams, Omit<AgentStatus, 'state' | 'error'>]
  'agent.start': [NoParams, AgentStatus]
  'agent.stop': [NoParams, AgentStatus]
  'agent.restart': [NoParams, AgentStatus]
  'agent.status': [NoParams, AgentStatus & { cloud: CloudStatus }]
  'media.cloud_cache.prepare': [{ session_id: number }, CloudCacheStatus]
  'media.cloud_cache.status': [NoParams, CloudCacheStatus]
  'obs.status': [NoParams, ObsStatus]
  'obs.connect': [{ host?: string; port?: number; password?: string }, ObsStatus]
  'obs.disconnect': [NoParams, ObsStatus]
  'playback.status': [NoParams, PlaybackStatus]
  'playback.play': [{ media_id: string }, PlaybackStatus]
  'playback.playlist': [{ media_ids: string[]; loop?: boolean }, PlaybackStatus]
  'playback.pause': [NoParams, PlaybackStatus]
  'playback.resume': [NoParams, PlaybackStatus]
  'playback.stop': [NoParams, PlaybackStatus]
  'playback.next': [NoParams, PlaybackStatus]
  'playback.previous': [NoParams, PlaybackStatus]
  'playback.loop': [{ loop: boolean }, PlaybackStatus]
  'playback.camera': [NoParams, PlaybackStatus]
  'playback.video': [NoParams, PlaybackStatus]
  'monitor.status': [NoParams, MonitorStatus]
  'monitor.start': [NoParams, MonitorStatus]
  'monitor.stop': [NoParams, MonitorStatus]
  'monitor.config': [Partial<MonitorConfig>, MonitorConfig]
  'monitor.templates.list': [NoParams, { templates: TemplateInfo[] }]
  'monitor.templates.set_custom': [{ name: string; image_base64: string }, { templates: TemplateInfo[] }]
  'monitor.templates.reset': [{ name: string }, { templates: TemplateInfo[] }]
  'monitor.sadcaptcha.status': [NoParams, SadCaptchaStatus]
  'monitor.sadcaptcha.set_key': [{ api_key: string }, SadCaptchaStatus]
  'monitor.sadcaptcha.clear_key': [NoParams, SadCaptchaStatus]
  'monitor.sadcaptcha.analyze': [{ kind: 'shapes' | 'puzzle'; image_base64: string; piece_image_base64?: string }, SadCaptchaAnalysis]
  'voice.speak': [{ text: string; voice?: Voice; rate?: number; duck?: boolean; intro?: string }, { status: string; id: string; position: number; source: string; voice: string; chars: number }]
  'voice.clear': [NoParams, { cleared: number; stopped: boolean }]
  'voice.status': [NoParams, { queue: number; prepare_pending: number; speaking: string | null; ducked: boolean; stats: Record<string, number>; last: Record<string, unknown> | null }]
  'diagnostics.export': [NoParams, { path: string; folder: string; bytes: number }]
  'logs.recent': [{ limit?: number }, { entries: LogEntry[] }]
}

export type MethodName = keyof CoreMethods
export type MethodParams<M extends MethodName> = CoreMethods[M][0]
export type MethodResult<M extends MethodName> = CoreMethods[M][1]

/** event -> data */
export interface CoreEvents {
  'core.ready': { version: string; ipc_protocol: number; pid: number; methods: string[]; status: SystemStatus }
  'core.error': AgentErrorInfo & { fatal?: boolean }
  'core.stopped': { reason: string }
  'agent.status_changed': AgentStatus
  'cloud.connection_changed': CloudStatus
  'media.cache_changed': CloudCacheStatus
  'obs.status_changed': ObsStatus
  'playback.changed': PlaybackStatus
  'monitor.status_changed': MonitorStatus
  'monitor.captcha_detected': { at: string; score: number; template: string; image_path: string; window_title: string }
  'monitor.captcha_cleared': { at: string }
  'monitor.verification_detected': { at: string; kind: 'INACTIVITY_WARNING' | 'CAPTCHA_CHALLENGE' | 'UNKNOWN_VERIFICATION'; challenge_type: string | null; score: number; template: string; image_path: string; window_title: string }
  'monitor.verification_cleared': { at: string; kind: string | null }
  'command.received': CommandEntry
  'command.finished': CommandEntry
  'log.entry': LogEntry
}

export type EventName = keyof CoreEvents

export interface CoreEventFrame<E extends EventName = EventName> {
  v: number
  type: 'event'
  event: E
  data: CoreEvents[E]
  ts: string
}
