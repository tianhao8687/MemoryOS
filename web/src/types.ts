export type ScopeType = 'user' | 'workspace' | 'repository' | 'branch' | 'task'
export type MemoryType =
  | 'semantic'
  | 'episodic'
  | 'procedural'
  | 'preference'
  | 'project'
  | 'working'
export type MemoryStatus =
  | 'candidate'
  | 'active'
  | 'superseded'
  | 'expired'
  | 'forgotten'
  | 'rejected'

export interface MemoryRecord {
  id: string
  scope_type: ScopeType
  scope_key: string
  memory_type: MemoryType
  category: string
  subject: string | null
  key: string | null
  title: string
  content: string
  status: MemoryStatus
  confidence: number
  importance: number
  valid_from: string | null
  valid_to: string | null
  ttl_seconds: number | null
  supersedes_id: string | null
  created_at: string
  updated_at: string
  created_by: string
  sensitivity: 'normal' | 'sensitive'
  metadata: Record<string, unknown>
}

export interface SearchItem {
  memory: MemoryRecord
  score: number
  lexical_score: number
  semantic_score?: number
}

export interface SearchResponse {
  items: SearchItem[]
  total: number
  mode: string
}

export type TruthState = 'resolved' | 'contested' | 'stale' | 'unknown'
export type FreshnessState = 'fresh' | 'moved' | 'suspect' | 'stale' | 'unknown'

export interface EntityRecord {
  id: string
  canonical_name: string
  normalized_name: string
  entity_type: string
  aliases: string[]
}

export interface ClaimRecord {
  id: string
  memory_id: string
  subject: EntityRecord | null
  predicate: string
  object_kind: string
  object_value: unknown
  polarity: string
  modality: string
  confidence: number
  status: string
  valid_from: string | null
  valid_to: string | null
  recorded_at: string
  stale_state: string
}

export interface TruthGroup {
  subject: EntityRecord
  predicate: string
  state: TruthState
  accepted_claims: ClaimRecord[]
  conflicting_claims: ClaimRecord[]
  evidence: Array<Record<string, unknown>>
  freshness: string[]
  resolution_history: Array<Record<string, unknown>>
}

export interface CurrentTruthResponse {
  state: TruthState
  truths: TruthGroup[]
  accepted_claims: ClaimRecord[]
  conflicting_claims: ClaimRecord[]
  evidence: Array<Record<string, unknown>>
  freshness: string[]
  resolution_history: Array<Record<string, unknown>>
  as_of_valid_time: string
  as_known_at: string
}

export interface ClaimGraphResponse {
  state: TruthState
  nodes: ClaimRecord[]
  edges: Array<{
    id: string
    from: string
    to: string
    type: string
    confidence: number
    method: string
    explanation: string
  }>
}

export interface FreshnessRecord {
  anchor_id: string
  memory_id: string
  memory_title: string
  claim_id: string
  path: string
  symbol_fqn: string | null
  freshness: FreshnessState
  commit_sha: string
  cached_head: string | null
  checked_at: string | null
}

export interface ConsolidationRecord {
  id: string
  scope_type: ScopeType
  scope_key: string
  subject_entity_id: string
  predicate: string
  proposal: Record<string, unknown>
  status: string
  source_memory_ids: string[]
  counterevidence: Array<Record<string, unknown>>
  created_at: string
}

export interface ConsolidationProposal {
  id: string | null
  status: string
  proposal: Record<string, unknown>
  source_memory_ids: string[]
  relations: Array<Record<string, unknown>>
  counterevidence: Array<Record<string, unknown>>
}

export interface RetrievalManifestItem {
  memory_id: string
  claim_ids: string[]
  included: boolean
  inclusion_reason: string | null
  exclusion_reason: string | null
  utility: number
  cost: number
  truth_state: TruthState
  freshness: FreshnessState
  retrieval_trace: Record<string, unknown>
}

export interface MemoryBenchSuite {
  suite: string
  sample_size: number
  evidence_type?: string
  baseline?: Record<string, unknown>
  v2?: Record<string, unknown>
  fixture?: Record<string, unknown>
  real_model?: Record<string, unknown>
  gate?: { passed: boolean; rule: string }
  truthfulness_gate?: { passed: boolean; reason: string }
}

export interface MemoryBenchReport {
  schema: string
  generated_at: string
  seed: number
  config_hash: string
  git: { commit: string; dirty: boolean | null }
  provider_policy: Record<string, unknown>
  suites: Record<string, MemoryBenchSuite>
  release_gates: {
    measured_all_passed: boolean
    real_model_agent_effect: string
    release_readiness: string
    note: string
  }
}

export interface StatusResponse {
  version: string
  database: string
  schema_version: string
  counts: Record<MemoryStatus, number>
  sources: number
  provenance_rate: number
  conflicts: number
  embedding_provider: string
  mode: string
}

export interface Repository {
  id: string
  stable_key: string
  name: string
  path: string
  remote_url: string | null
  default_branch: string | null
  branch?: string
  head?: string
}

export interface AuditEvent {
  id: string
  action: string
  entity_type: string
  entity_id: string
  actor: string
  timestamp: string
  details: Record<string, unknown>
}

export interface ExplainResponse {
  memory: MemoryRecord
  sources: Array<{
    id: string
    source_type: string
    source_ref: string
    captured_at: string
    excerpt: string
    content_hash: string
    metadata: Record<string, unknown>
  }>
  relations: Array<{
    id: string
    from_memory_id: string
    to_memory_id: string
    relation_type: string
    metadata: Record<string, unknown>
  }>
  audit: AuditEvent[]
  reason: string
}

export interface ConflictRecord {
  candidate: MemoryRecord
  current: MemoryRecord[]
  semantic_key: string
  status: 'needs_review'
}

export interface ContextResponse {
  task: string
  repository: string
  branch: string | null
  budget: number
  characters_used: number
  retrieval_mode: string
  retrieval_run_id?: string
  query_plan?: Record<string, unknown>
  truth_state?: TruthState
  manifest?: RetrievalManifestItem[]
  debug?: {
    config_hash: string
    reranker: string
    candidates: RetrievalManifestItem[]
  }
  sections: Record<string, MemoryRecord[]>
  text: string
}

export interface DoctorCheck {
  name: string
  status: 'PASS' | 'WARN' | 'FAIL'
  detail: string
}

export interface DoctorResponse {
  overall: 'PASS' | 'WARN' | 'FAIL'
  checks: DoctorCheck[]
}
