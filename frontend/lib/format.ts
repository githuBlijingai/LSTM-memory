const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "medium",
});

const dateFormatter = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" });

function parseDate(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | null | undefined) {
  const date = parseDate(value);
  return date ? dateTimeFormatter.format(date) : "—";
}

export function formatDate(value: string | null | undefined) {
  const date = parseDate(value);
  return date ? dateFormatter.format(date) : "—";
}

/** 保留原始 UTC 时间，供 Tooltip 展示。 */
export function utcTooltip(value: string | null | undefined) {
  const date = parseDate(value);
  return date ? date.toISOString() : "";
}

export function formatNumber(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatScore(value: number | null | undefined, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return value.toFixed(digits);
}

export function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${(value * 100).toFixed(1)}%`;
}

export function toIso(value: string | null | undefined) {
  const date = parseDate(value);
  return date ? date.toISOString() : undefined;
}
