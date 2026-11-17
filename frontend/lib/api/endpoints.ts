import { requestEnvelope } from "./client";
import type {
  DashboardSummary,
  EntityDetail,
  EntityRebuildResponse,
  EntityState,
  EntitySummary,
  EventCorrectionRequest,
  EventCorrectionResponse,
  EventDeleteRequest,
  EventDeleteResponse,
  EventDetail,
  EventRestoreRequest,
  EventRestoreResponse,
  EventSummary,
  FamiliarityUpdateRequest,
  FamiliarityUpdateResponse,
  FeedbackRequest,
  FeedbackResponse,
  JobResponse,
  ReplayLog,
  SearchRequest,
  SearchResponse,
  WikiArticleDetail,
  WikiArticleSummary,
  WikiSyncTask,
  WikiSyncTaskCreateRequest,
} from "./contracts";

type QueryValue = string | number | boolean | null | undefined;

function toQuery(params: Record<string, QueryValue>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function idempotentHeaders() {
  return { "idempotency-key": globalThis.crypto.randomUUID() };
}

export interface EventListParams {
  subject?: string;
  entity_id?: string;
  event_type?: string;
  start_time?: string;
  end_time?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export function listEvents(params: EventListParams = {}) {
  return requestEnvelope<EventSummary[]>(`/memory/events${toQuery({ ...params })}`);
}

export function getEvent(eventId: string) {
  return requestEnvelope<EventDetail>(`/memory/events/${encodeURIComponent(eventId)}`);
}

export function correctEvent(eventId: string, body: EventCorrectionRequest) {
  return requestEnvelope<EventCorrectionResponse>(
    `/memory/events/${encodeURIComponent(eventId)}/corrections`,
    { body, headers: idempotentHeaders(), method: "POST" },
  );
}

export function archiveEvent(eventId: string, body: EventDeleteRequest) {
  return requestEnvelope<EventDeleteResponse>(
    `/memory/events/${encodeURIComponent(eventId)}`,
    { body, headers: idempotentHeaders(), method: "DELETE" },
  );
}

export function restoreEvent(eventId: string, body: EventRestoreRequest) {
  return requestEnvelope<EventRestoreResponse>(
    `/memory/events/${encodeURIComponent(eventId)}/restore`,
    { body, headers: idempotentHeaders(), method: "POST" },
  );
}

export function updateFamiliarity(
  memoryType: "event" | "wiki",
  memoryId: string,
  body: FamiliarityUpdateRequest,
) {
  return requestEnvelope<FamiliarityUpdateResponse>(
    `/admin/memories/${memoryType}/${encodeURIComponent(memoryId)}/familiarity`,
    { body, headers: idempotentHeaders(), method: "POST" },
  );
}

export function searchMemories(body: SearchRequest) {
  return requestEnvelope<SearchResponse>("/memory/search", { body, method: "POST" });
}

export function submitFeedback(body: FeedbackRequest) {
  return requestEnvelope<FeedbackResponse>("/memory/feedback", {
    body,
    headers: idempotentHeaders(),
    method: "POST",
  });
}

export interface EntityListParams {
  entity_type?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export function listEntities(params: EntityListParams = {}) {
  return requestEnvelope<EntitySummary[]>(`/entities${toQuery({ ...params })}`);
}

export function getEntity(entityId: string) {
  return requestEnvelope<EntityDetail>(`/entities/${encodeURIComponent(entityId)}`);
}

export function getEntityState(entityId: string) {
  return requestEnvelope<EntityState>(`/entities/${encodeURIComponent(entityId)}/state`);
}

export interface EntityEventListParams {
  event_type?: string;
  start_time?: string;
  end_time?: string;
  cursor?: string;
  limit?: number;
}

export function listEntityEvents(entityId: string, params: EntityEventListParams = {}) {
  return requestEnvelope<EventSummary[]>(
    `/entities/${encodeURIComponent(entityId)}/events${toQuery({ ...params })}`,
  );
}

export function listEntityReplays(entityId: string, limit = 20) {
  return requestEnvelope<ReplayLog[]>(
    `/entities/${encodeURIComponent(entityId)}/replays${toQuery({ limit })}`,
  );
}

export function rebuildEntity(entityId: string) {
  return requestEnvelope<EntityRebuildResponse>(
    `/entities/${encodeURIComponent(entityId)}/rebuild`,
    { headers: idempotentHeaders(), method: "POST" },
  );
}

export function getJob(jobId: string) {
  return requestEnvelope<JobResponse>(`/jobs/${encodeURIComponent(jobId)}`);
}

export function retryJob(jobId: string) {
  return requestEnvelope<JobResponse>(`/jobs/${encodeURIComponent(jobId)}/retry`, {
    headers: idempotentHeaders(),
    method: "POST",
  });
}

export function getDashboardSummary() {
  return requestEnvelope<DashboardSummary>("/dashboard/summary");
}

export interface WikiArticleListParams {
  q?: string;
  category?: string;
  language?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export function listWikiArticles(params: WikiArticleListParams = {}) {
  return requestEnvelope<WikiArticleSummary[]>(`/wiki/articles${toQuery({ ...params })}`);
}

export function getWikiArticle(articleId: string, includeInactive = false) {
  return requestEnvelope<WikiArticleDetail>(
    `/wiki/articles/${encodeURIComponent(articleId)}${toQuery({ include_inactive: includeInactive })}`,
  );
}

export interface WikiSyncTaskListParams {
  source?: string;
  mode?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export function listWikiSyncTasks(params: WikiSyncTaskListParams = {}) {
  return requestEnvelope<WikiSyncTask[]>(`/wiki/sync-tasks${toQuery({ ...params })}`);
}

export function getWikiSyncTask(taskId: string) {
  return requestEnvelope<WikiSyncTask>(
    `/wiki/sync-tasks/${encodeURIComponent(taskId)}`,
  );
}

export function createWikiSyncTask(body: WikiSyncTaskCreateRequest) {
  return requestEnvelope<WikiSyncTask>("/wiki/sync-tasks", {
    body,
    headers: idempotentHeaders(),
    method: "POST",
  });
}

export function retryWikiSyncTask(taskId: string) {
  return requestEnvelope<WikiSyncTask>(
    `/wiki/sync-tasks/${encodeURIComponent(taskId)}/retry`,
    { headers: idempotentHeaders(), method: "POST" },
  );
}
