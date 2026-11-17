import { Tooltip } from "antd";
import { formatDateTime, utcTooltip } from "@/lib/format";

export function DateTimeText({
  value,
  fallback = "—",
}: {
  value: string | null | undefined;
  fallback?: string;
}) {
  if (!value) {
    return <span className="muted">{fallback}</span>;
  }
  const utc = utcTooltip(value);
  return (
    <Tooltip title={utc ? `UTC ${utc}` : undefined}>
      <time dateTime={value}>{formatDateTime(value)}</time>
    </Tooltip>
  );
}
