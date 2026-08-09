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
