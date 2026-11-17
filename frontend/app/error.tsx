"use client";

import { ErrorState } from "@/components/states/StatePanels";

export default function ErrorBoundary({ reset }: { reset: () => void }) {
  return (
    <ErrorState
      description="页面渲染时发生错误，请重试。"
      onRetry={reset}
      title="页面暂时不可用"
    />
  );
}
