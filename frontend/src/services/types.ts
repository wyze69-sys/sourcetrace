export interface TokenResponse {
  access_token: string
  token_type: 'Bearer'
  expires_in: number
}

export interface ServerCapabilities {
  allowed_index_modes: string[]
  default_index_mode: string
  lexical_search_available: boolean
  semantic_search_available: boolean
  generation_available: boolean
}

export interface HealthResponse {
  status: string
  version: string
  mongodb: string
  storage: string
}

export interface ErrorDetail {
  code: string
  message: string
  request_id?: string
}

export interface ErrorEnvelope {
  error: ErrorDetail
}

export type RepositorySourceType = 'github' | 'zip'

export type RepositoryStatus = 'pending' | 'indexing' | 'ready' | 'failed'

export type IndexMode = 'static' | 'cloud_ai'

export interface Repository {
  repository_id: string
  name: string
  source_type: RepositorySourceType
  github_url?: string | null
  status: RepositoryStatus
  file_count: number
  chunk_count: number
  created_at: string
  updated_at: string
  index_mode?: IndexMode
  /** ISO-8601 UTC timestamp of last successful index. Null before first index. */
  last_indexed_at?: string | null
  /** Git commit SHA that was indexed. Null if SHA could not be resolved. */
  indexed_commit_sha?: string | null
  /** Branch that was indexed (e.g. "main"). Null if unknown. */
  indexed_branch?: string | null
  /** True when the backend detects the repo may be out of date. Null if unknown/unchecked. */
  is_stale?: boolean | null
  /** ISO-8601 UTC timestamp of last freshness check against remote GitHub. */
  stale_checked_at?: string | null
  /** True when all indexed chunks support flow-evidence extraction. */
  flow_evidence_complete?: boolean
}

export type IndexingJobStatus =
  | 'queued'
  | 'acquiring'
  | 'scanning'
  | 'parsing'
  | 'embedding'
  | 'storing'
  | 'ready'
  | 'failed'

export interface IndexingJob {
  job_id: string
  repository_id: string
  status: IndexingJobStatus
  /** "initial" for first import; "refresh" for subsequent re-index. */
  job_type?: 'initial' | 'refresh'
  progress_percentage: number
  current_step: string
  error_message?: string | null
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface CreateRepositoryResponse {
  repository: Repository
  indexing_job: IndexingJob
}

export interface RepositoryListResponse {
  repositories: Repository[]
}

export interface DeleteRepositoryResponse {
  message: string
  repository_id: string
}

export interface Citation {
  relative_path: string
  start_line: number
  end_line: number
  symbol_name: string
  symbol_type: string
}

export interface EvidenceSnippet {
  snippet: string
  relative_path: string
  start_line: number
  end_line: number
  symbol_name: string
  symbol_type: string
}

export type MessageRole = 'user' | 'assistant'

export interface Message {
  message_id: string
  role: MessageRole
  content: string
  insufficient_evidence?: boolean
  citations?: Citation[]
  evidence?: EvidenceSnippet[]
  created_at: string
}

export interface RequestMetadata {
  latency_ms: number
  chunks_retrieved: number
}

export interface CreateConversationRequest {
  question: string
}

export interface CreateConversationResponse {
  conversation_id: string
  repository_id: string
  user_message: Message
  assistant_message: Message
  request_metadata: RequestMetadata
}

export interface ConversationDetailResponse {
  conversation_id: string
  repository_id: string
  title: string
  created_at: string
  updated_at: string
  messages: Message[]
}

export interface SendMessageRequest {
  question: string
}

export interface SendMessageResponse {
  conversation_id: string
  repository_id: string
  user_message: Message
  assistant_message: Message
  request_metadata: RequestMetadata
}

export interface EvidenceSearchItem {
  chunk_id: string
  score: number
  relative_path: string
  symbol_name: string
  symbol_type: string
  start_line: number
  end_line: number
  snippet: string
}

export interface EvidenceSearchResponse {
  repository_id: string
  total: number
  items: EvidenceSearchItem[]
}

export type TraceEdgeKind = 'call' | 'import' | 'http'

export type TraceConfidence = 'high' | 'medium' | 'low'

export interface TraceEntry {
  query: string
  resolved_node_id: string | null
  candidates: string[]
}

export interface TraceNode {
  node_id: string
  relative_path: string
  symbol_name: string
  symbol_type: string
  start_line: number
  end_line: number
  snippet: string
}

export interface TraceEdge {
  from_node_id: string
  to_node_id: string
  kind: TraceEdgeKind
  confidence: TraceConfidence
  evidence_label: string
  evidence_line_start: number
  evidence_line_end: number
  alternatives: string[]
}

export interface TraceGap {
  kind: string
  detail: string
  node_id: string | null
}

export type TraceMode = 'static' | 'explain'

export interface TraceExplanation {
  text: string
  cited_steps: number[]
}

export interface FlowTraceResponse {
  repository_id: string
  entry: TraceEntry
  nodes: TraceNode[]
  edges: TraceEdge[]
  steps: string[]
  gaps: TraceGap[]
  explanation: TraceExplanation | null
}

export type ImpactEdgeKind = 'call' | 'http'

export type RiskSeverity = 'low' | 'medium' | 'high'

export type RiskLevel = 'low' | 'medium' | 'high' | 'unknown'

export interface ImpactTarget {
  query: string
  resolved_node_id: string | null
  candidates: string[]
}

export interface ImpactItem {
  node_id: string
  relative_path: string
  symbol_name: string
  symbol_type: string
  start_line: number
  end_line: number
  distance: number
  confidence: TraceConfidence
  edge_kind: ImpactEdgeKind
  via_node_id: string
  evidence_node_id: string
  evidence_label: string
  evidence_line_start: number
  evidence_line_end: number
}

export interface AffectedEndpoint {
  http_method: string
  normalized_path: string
  node_id: string
}

export interface RiskFactor {
  kind: string
  severity: RiskSeverity
  detail: string
}

export interface ImpactGap {
  kind: string
  detail: string
  node_id: string | null
}

export type ImpactMode = 'static' | 'explain'

export interface ImpactExplanation {
  text: string
  cited_steps: number[]
}

export interface ChangeImpactResponse {
  repository_id: string
  target: ImpactTarget
  upstream: ImpactItem[]
  downstream: ImpactItem[]
  affected_endpoints: AffectedEndpoint[]
  affected_components: string[]
  affected_tests: string[]
  risk_level: RiskLevel
  risk_factors: RiskFactor[]
  gaps: ImpactGap[]
  explanation: ImpactExplanation | null
}

export interface DiffTarget {
  node_id: string
  relative_path: string
  symbol_name: string
  symbol_type: string
  start_line: number
  end_line: number
  changed_lines: number[]
}

export interface DiffImpactResponse {
  repository_id: string
  targets: DiffTarget[]
  upstream: ImpactItem[]
  downstream: ImpactItem[]
  affected_endpoints: AffectedEndpoint[]
  affected_components: string[]
  affected_tests: string[]
  risk_level: RiskLevel
  risk_factors: RiskFactor[]
  gaps: ImpactGap[]
  explanation: ImpactExplanation | null
}
