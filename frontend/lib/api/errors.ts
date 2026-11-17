import { ApiError } from "./client";

export function getErrorStatus(error: unknown): number | undefined {
  return error instanceof ApiError ? error.status : undefined;
}

export function getRequestId(error: unknown): string | undefined {
  return error instanceof ApiError ? error.requestId : undefined;
}

export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "请求失败，请稍后重试。";
}

export function isUnauthorized(error: unknown) {
  return getErrorStatus(error) === 401;
}

export function isForbidden(error: unknown) {
  return getErrorStatus(error) === 403;
}

/**
 * 后端尚未实现的路由会返回 404，网关/代理未配置时返回 503。
 * 页面据此展示“接口未就绪”降级态，而不是伪造数据。
 */
export function isInterfaceUnavailable(error: unknown) {
  const status = getErrorStatus(error);
  return status === 404 || status === 405 || status === 501 || status === 503;
}
