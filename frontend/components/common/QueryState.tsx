"use client";

import type { ReactNode } from "react";
import {
  getErrorMessage,
  getErrorStatus,
  getRequestId,
  isForbidden,
  isInterfaceUnavailable,
  isUnauthorized,
} from "@/lib/api";
import {
  EmptyState,
  ErrorState,
  ForbiddenState,
  LoadingState,
  NotFoundState,
  UnauthenticatedState,
  UnavailableState,
} from "@/components/states/StatePanels";

interface QueryStateProps {
  isLoading: boolean;
  error: unknown;
  onRetry?: () => void;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  loadingLabel?: string;
  unavailableTitle?: string;
  unavailableDescription?: string;
  /** Wiki 等尚未实现的接口把 404 视为“接口未就绪”。 */
  notFoundAsUnavailable?: boolean;
  children: ReactNode;
}

/**
 * 统一渲染加载、401、403、404、接口未就绪、空和错误状态。
 * 页面只负责提供真实数据，不在此处生成任何占位业务数据。
 */
export function QueryState({
  isLoading,
  error,
  onRetry,
  isEmpty = false,
  emptyTitle,
  emptyDescription,
  emptyAction,
  loadingLabel,
  unavailableTitle,
  unavailableDescription,
  notFoundAsUnavailable = false,
  children,
}: QueryStateProps) {
  if (isLoading) {
    return <LoadingState label={loadingLabel} />;
  }

  if (error) {
    if (isUnauthorized(error)) {
      return <UnauthenticatedState onRetry={onRetry} />;
    }
    if (isForbidden(error)) {
      return (
        <ForbiddenState
          description={`当前账号权限不足。${getErrorMessage(error)}`}
        />
      );
    }
    if (isInterfaceUnavailable(error)) {
      if (getErrorStatus(error) === 404 && !notFoundAsUnavailable) {
        return <NotFoundState />;
      }
      return (
        <UnavailableState
          description={unavailableDescription}
          onRetry={onRetry}
          title={unavailableTitle}
        />
      );
    }
    return (
      <ErrorState
        description={getErrorMessage(error)}
        onRetry={onRetry}
        requestId={getRequestId(error)}
      />
    );
  }

  if (isEmpty) {
    return (
      <EmptyState
        action={emptyAction}
        description={emptyDescription}
        title={emptyTitle}
      />
    );
  }

  return <>{children}</>;
}
