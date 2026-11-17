"use client";

import { CopyOutlined } from "@ant-design/icons";
import { App, Button, Tooltip } from "antd";

export function CopyButton({
  text,
  label = "复制",
  size = "small",
}: {
  text: string;
  label?: string;
  size?: "small" | "middle";
}) {
  const { message } = App.useApp();

  return (
    <Tooltip title={label}>
      <Button
        aria-label={label}
        icon={<CopyOutlined />}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            message.success("已复制");
          } catch {
            message.error("复制失败，请手动选择文本。");
          }
        }}
        size={size}
        type="text"
      />
    </Tooltip>
  );
}
