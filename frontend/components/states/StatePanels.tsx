"use client";

import { Button, Empty, Result, Spin } from "antd";
import type { ReactNode } from "react";

export function LoadingState({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="state-panel loading-state" role="status" aria-live="polite">
      <Spin />
      <span>{label}</span>
    </div>
  );
}

interface EmptyStateProps {
  title?: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({
  title = "暂无数据",
  description,
  action,
}: EmptyStateProps) {
  return (
    <div className="state-panel">
      <Empty description={<span>{title}</span>} image={Empty.PRESENTED_IMAGE_SIMPLE}>
        {description ? <p className="state-description">{description}</p> : null}
        {action}
      </Empty>
    </div>
  );
}

interface ErrorStateProps {
  title?: string;
  description?: string;
  requestId?: string;
  onRetry?: () => void;
}

export function ErrorState({
  title = "加载失败",
  description = "请求未完成，请稍后重试。",
  requestId,
  onRetry,
}: ErrorStateProps) {
  return (
    <Result
      extra={onRetry ? <Button onClick={onRetry} type="primary">重新加载</Button> : undefined}
      status="error"
      subTitle={
        <span>
          {description}
          {requestId ? <span className="request-id">请求 ID：{requestId}</span> : null}
        </span>
      }
      title={title}
    />
  );
}

export function ForbiddenState({
  description = "当前账号没有访问此内容的权限。",
}: {
  description?: string;
}) {
  return <Result status="403" subTitle={description} title="无权访问" />;
}

export function UnauthenticatedState({
  description = "会话不存在或已过期。请先登录，或按 docs 说明显式开启本地 Dev Auth。",
  onRetry,
}: {
  description?: string;
  onRetry?: () => void;
}) {
  return (
    <Result
      extra={onRetry ? <Button onClick={onRetry} type="primary">重新加载</Button> : undefined}
      status="warning"
      subTitle={description}
      title="需要登录"
    />
  );
}

export function UnavailableState({
  title = "接口尚未就绪",
  description = "后端对应接口尚未实现或当前环境未配置，页面不会展示模拟数据。",
  onRetry,
}: {
  title?: string;
  description?: string;
  onRetry?: () => void;
}) {
  return (
    <Result
      extra={onRetry ? <Button onClick={onRetry} type="primary">重新检查</Button> : undefined}
      status="info"
      subTitle={description}
      title={title}
    />
  );
}

export function NotFoundState({
  title = "内容不存在",
  description = "目标记录不存在、已被删除，或当前账号不可见。",
}: {
  title?: string;
  description?: string;
}) {
  return <Result status="404" subTitle={description} title={title} />;
}
