import type { components } from "./schema";

export type Visibility = components["schemas"]["Visibility"];
export type FeedbackType = components["schemas"]["FeedbackType"];
export type EventCreateRequest = components["schemas"]["EventCreateRequest"];
export type EventCorrectionRequest = components["schemas"]["EventCorrectionRequest"];
export type EventDeleteRequest = components["schemas"]["EventDeleteRequest"];
export type EventRestoreRequest = components["schemas"]["EventRestoreRequest"];
export type SearchRequest = components["schemas"]["SearchRequest"];
export type SearchFiltersRequest = components["schemas"]["SearchFiltersRequest"];
export type FeedbackRequest = components["schemas"]["FeedbackRequest"];
export type FamiliarityUpdateRequest = components["schemas"]["FamiliarityUpdateRequest"];

export type EventStatus = "pending" | "active" | "failed" | "archived" | "purged";

export interface EventSummary {
  event_id: string;
  subject: string;
  event_type: string;
  title: string;
  event_time: string;
  importance: number;
  status: EventStatus | string;
  visibility: Visibility;
  created_at: string;
}

export interface EventDetail extends EventSummary {
  content: string | null;
  entity_id: string;
  decay_score: number;
  familiarity_score: number;
  source: string;
  supersedes_event_id: string | null;
  sequence_no: number | null;
  event_model_version: string | null;
  updated_at: string;
}

export interface EventAcceptedResponse {
  event_id: string;
  status: string;
  job_id: string;
}

export interface EventCorrectionResponse {
  new_event_id: string;
  status: string;
  rebuild_job_id: string | null;
}

export interface EventDeleteResponse {
  event_id: string;
  status: string;
  purge_after: string | null;
  rebuild_job_id: string | null;
}

export interface EventRestoreResponse {
  event_id: string;
  status: string;
  rebuild_job_id: string | null;
}

export interface ScoreBreakdown {
  cosine_similarity?: number;
  rerank_score?: number;
  relevance?: number;
  decay_score?: number;
  familiarity_score?: number;
  memory_weight?: number;
  importance?: number;
  intent_weight?: number;
  [key: string]: number | undefined;
}

export interface SearchResultBase {
  memory_id: string;
  final_score: number;
  feedback_state: string | null;
  score_breakdown: ScoreBreakdown | null;
  model_version: string | null;
}

export interface EventSearchResult extends SearchResultBase {
  memory_type: "event";
  title: string;
  content: string;
  event_time: string;
}

export interface WikiSourceRef {
  name: string;
  url: string;
  page_id: string;
  revision_id: string;
}

export interface WikiSearchResult extends SearchResultBase {
  memory_type: "wiki";
  article_id: string;
  title: string;
  content: string;
  chunk_index: number;
  heading_path: string[];
  source: WikiSourceRef;
}

export type SearchResult = EventSearchResult | WikiSearchResult;

export interface SearchResponse {
  query_id: string;
  intent: "event" | "wiki" | "mixed";
  results: SearchResult[];
}

export interface FeedbackResponse {
  accepted: boolean;
  counted: boolean;
  not_counted_reason: string | null;
  familiarity_score: number;
  replay_job_created: boolean;
}

export interface EntitySummary {
  entity_id: string;
  entity_name: string;
  entity_type: string;
  status: string;
  visibility: Visibility;
  readable_summary: string;
  state_version: number;
  model_version: string | null;
  last_sequence_no: number;
  last_update_time: string | null;
}

export interface EntityDetail extends EntitySummary {
  owner_id: string;
  source_event_ids: string[];
  summary_version: string | null;
  summary_updated_at: string | null;
  last_event_id: string | null;
  last_event_time: string | null;
}

export interface EntityState {
  entity_id: string;
  status: string;
  state_version: number;
  model_version: string | null;
  last_event_id: string | null;
  last_sequence_no: number;
  last_update_time: string | null;
  latest_snapshot_at: string | null;
  pending_replay_count: number;
  active_rebuild_job_id: string | null;
}

export interface ReplayLog {
  replay_id: string;
  event_id: string;
  reason: string;
  before_state_version: number;
  after_state_version: number | null;
  model_version: string;
  status: string;
  attempt_count: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface EntityRebuildResponse {
  entity_id: string;
  job_id: string;
  status: string;
}

export interface JobResponse {
  job_id: string;
  job_type: string;
  resource_type: string;
  resource_id: string;
  status: string;
  progress: number;
  attempt_count: number;
  last_error: string | null;
  requested_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface DashboardSummary {
  events: Record<string, number>;
  wiki: Record<string, number | string | null>;
  jobs: Record<string, number>;
  outbox: Record<string, number>;
  alerts: Record<string, number>;
}

export interface FamiliarityUpdateResponse {
  memory_type: "event" | "wiki";
  memory_id: string;
  familiarity_score: number;
  updated_at: string;
}

export type WikiArticleStatus = "active" | "redirected" | "deleted";
export type WikiChunkStatus = "pending" | "active" | "failed" | "superseded" | "deleted";

/** 设计 6.7：词条列表项。 */
export interface WikiArticleSummary {
  article_id: string;
  title: string;
  summary: string;
  language: string;
  categories: string[];
  domain: string | null;
  source: string;
  source_page_id: string;
  revision_id: string;
  source_url: string;
  source_updated_at: string | null;
  sync_time: string | null;
  status: WikiArticleStatus | string;
  chunk_count: number;
}

export interface WikiChunk {
  chunk_id: string;
  chunk_index: number;
  heading_path: string[];
  raw_text: string;
  token_count: number;
  status: WikiChunkStatus | string;
  model_version: string | null;
}

export interface WikiTocEntry {
  heading_path: string[];
  chunk_index: number;
}

/** 设计 6.7：词条详情必须包含正文、目录、按 chunk_index 排序的知识块与来源修订信息。 */
export interface WikiArticleDetail extends WikiArticleSummary {
  content: string;
  toc: WikiTocEntry[];
  chunks: WikiChunk[];
}

export interface WikiSyncErrorItem {
  code: string;
  message: string;
  source_item_id: string | null;
}

export type WikiSyncMode = "full" | "incremental" | "reembed";

export interface WikiSyncTask {
  task_id: string;
  job_id: string | null;
  source: string;
  mode: WikiSyncMode | string;
  package_version: string;
  package_uri: string | null;
  status: string;
  total_count: number | null;
  processed_count: number;
  success_count: number;
  failed_count: number;
  error_sample: WikiSyncErrorItem[];
  requested_by: string;
  retryable: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface WikiSyncTaskCreateRequest {
  source: string;
  mode: WikiSyncMode;
  /** full / incremental 必填；reembed 不需要。 */
  package_uri: string | null;
  package_version: string;
}
