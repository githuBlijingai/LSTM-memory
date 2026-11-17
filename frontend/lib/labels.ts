export type TagColor =
  | "blue"
  | "cyan"
  | "default"
  | "gold"
  | "green"
  | "magenta"
  | "orange"
  | "purple"
  | "red"
  | "volcano";

const eventStatus: Record<string, { label: string; color: TagColor }> = {
  pending: { label: "待处理", color: "gold" },
  active: { label: "生效", color: "green" },
  failed: { label: "失败", color: "red" },
  archived: { label: "已归档", color: "default" },
  purged: { label: "已清除", color: "default" },
};

const jobStatus: Record<string, { label: string; color: TagColor }> = {
  queued: { label: "排队中", color: "blue" },
  running: { label: "运行中", color: "cyan" },
  retry_wait: { label: "等待重试", color: "orange" },
  succeeded: { label: "已成功", color: "green" },
  failed: { label: "失败", color: "red" },
  dead_letter: { label: "死信", color: "volcano" },
  cancelled: { label: "已取消", color: "default" },
};

const wikiArticleStatus: Record<string, { label: string; color: TagColor }> = {
  active: { label: "生效", color: "green" },
  redirected: { label: "重定向", color: "gold" },
  deleted: { label: "已删除", color: "default" },
};

const wikiChunkStatus: Record<string, { label: string; color: TagColor }> = {
  pending: { label: "待处理", color: "gold" },
  active: { label: "生效", color: "green" },
  failed: { label: "失败", color: "red" },
  superseded: { label: "已替换", color: "default" },
  deleted: { label: "已删除", color: "default" },
};

const entityStatus: Record<string, { label: string; color: TagColor }> = {
  active: { label: "生效", color: "green" },
  rebuilding: { label: "重建中", color: "orange" },
  archived: { label: "已归档", color: "default" },
};

const visibility: Record<string, { label: string; color: TagColor }> = {
  private: { label: "私有", color: "purple" },
  shared: { label: "共享", color: "blue" },
};

const wikiSyncMode: Record<string, string> = {
  full: "全量",
  incremental: "增量",
  reembed: "重新向量化",
};

const feedbackReason: Record<string, string> = {
  duplicate_feedback: "重复反馈，未重复计入",
  recent_duplicate_query: "近期重复查询，本次不重复计入",
  daily_cap_reached: "已达每日反馈上限",
  not_consolidating_feedback: "该反馈类型不计入巩固",
};

const feedbackTypeLabel: Record<string, string> = {
  exposure: "曝光",
  clicked: "点击",
  adopted: "采用",
  helpful: "有用",
  not_helpful: "无帮助",
};

const scoreLabels: Record<string, string> = {
  cosine_similarity: "余弦相似度",
  rerank_score: "精排分",
  relevance: "相关性",
  decay_score: "衰减分",
  familiarity_score: "熟悉度",
  memory_weight: "记忆权重",
  importance: "重要性",
  intent_weight: "意图权重",
};

const intentLabel: Record<string, string> = {
  event: "情景记忆",
  wiki: "Wiki 知识",
  mixed: "混合意图",
};

export function eventStatusMeta(status: string) {
  return eventStatus[status] ?? { label: status || "未知", color: "default" as TagColor };
}

export function jobStatusMeta(status: string) {
  return jobStatus[status] ?? { label: status || "未知", color: "default" as TagColor };
}

export function wikiArticleStatusMeta(status: string) {
  return wikiArticleStatus[status] ?? { label: status || "未知", color: "default" as TagColor };
}

export function wikiChunkStatusMeta(status: string) {
  return wikiChunkStatus[status] ?? { label: status || "未知", color: "default" as TagColor };
}

export function entityStatusMeta(status: string) {
  return entityStatus[status] ?? { label: status || "未知", color: "default" as TagColor };
}

export function visibilityMeta(value: string) {
  return visibility[value] ?? { label: value || "未知", color: "default" as TagColor };
}

export function wikiSyncModeLabel(mode: string) {
  return wikiSyncMode[mode] ?? mode;
}

export function feedbackReasonLabel(reason: string | null) {
  if (!reason) {
    return null;
  }
  return feedbackReason[reason] ?? reason;
}

export function feedbackTypeLabelOf(type: string) {
  return feedbackTypeLabel[type] ?? type;
}

export function scoreLabel(key: string) {
  return scoreLabels[key] ?? key;
}

export function intentLabelOf(intent: string) {
  return intentLabel[intent] ?? intent;
}

export const EVENT_STATUS_OPTIONS = Object.entries(eventStatus).map(([value, meta]) => ({
  label: meta.label,
  value,
}));

export const JOB_FAILURE_STATUSES = ["failed", "dead_letter"];
