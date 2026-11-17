import type { ApiErrorOptions, ApiRequestOptions } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(message: string, options: ApiErrorOptions) {
    super(message, { cause: options.cause });
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.requestId = options.requestId;
    this.details = options.details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function apiUrl(path: string) {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new TypeError("API 路径必须是以单个 / 开头的同源相对路径。");
  }
  return `/api/v1${path}`;
}

async function readPayload(response: Response, requestId: string) {
  const text = await response.text();
  if (!text) {
    return undefined;
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("json")) {
    return text;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch (cause) {
    if (response.ok) {
      throw new ApiError("服务返回了无法解析的响应。", {
        status: response.status,
        code: "INVALID_RESPONSE",
        requestId,
        cause,
      });
    }
    return text;
  }
}

function errorDetails(payload: unknown, status: number) {
  if (typeof payload === "string") {
    return { code: "HTTP_ERROR", message: payload, details: undefined };
  }

  if (!isRecord(payload)) {
    return {
      code: "HTTP_ERROR",
      message: `请求失败（HTTP ${status}）。`,
      details: undefined,
    };
  }

  const nestedError = isRecord(payload.error) ? payload.error : undefined;
  const nestedDetail = isRecord(payload.detail) ? payload.detail : undefined;
  const source = nestedError ?? nestedDetail ?? payload;
  const detailMessage = typeof payload.detail === "string" ? payload.detail : undefined;
  const message = typeof source.message === "string"
    ? source.message
    : detailMessage ?? `请求失败（HTTP ${status}）。`;
  const code = typeof source.code === "string" ? source.code : "HTTP_ERROR";

  return {
    code,
    message,
    details: source.details,
  };
}

export async function request<TResponse, TBody = unknown>(
  path: string,
  options: ApiRequestOptions<TBody> = {},
): Promise<TResponse> {
  const {
    body,
    headers: initialHeaders,
    requestId = globalThis.crypto.randomUUID(),
    ...init
  } = options;
  const headers = new Headers(initialHeaders);
  headers.set("accept", "application/json");
  headers.set("x-request-id", requestId);

  let requestBody: BodyInit | undefined;
  if (body !== undefined) {
    if (
      typeof body === "string" ||
      body instanceof Blob ||
      body instanceof FormData ||
      body instanceof URLSearchParams ||
      body instanceof ArrayBuffer ||
      ArrayBuffer.isView(body)
    ) {
      requestBody = body as BodyInit;
    } else {
      headers.set("content-type", "application/json");
      requestBody = JSON.stringify(body);
    }
  }

  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      body: requestBody,
      credentials: "same-origin",
      headers,
    });
  } catch (cause) {
    throw new ApiError("无法连接服务，请检查网络后重试。", {
      status: 0,
      code: "NETWORK_ERROR",
      requestId,
      cause,
    });
  }

  const payload = await readPayload(response, requestId);
  const meta = isRecord(payload) && isRecord(payload.meta) ? payload.meta : undefined;
  const payloadRequestId = meta && typeof meta.request_id === "string"
    ? meta.request_id
    : undefined;
  const responseRequestId = response.headers.get("x-request-id") ?? payloadRequestId ?? requestId;

  if (!response.ok) {
    const details = errorDetails(payload, response.status);
    throw new ApiError(details.message, {
      status: response.status,
      code: details.code,
      requestId: responseRequestId,
      details: details.details,
    });
  }

  return payload as TResponse;
}

export interface ApiMeta {
  request_id: string;
  next_cursor: string | null;
}

export interface ApiEnvelope<T> {
  data: T;
  meta: ApiMeta;
}

/**
 * 统一读取设计第 6.1 节的 `{data, meta}` 响应包络。
 * 成功响应缺少 data 时按契约错误处理，避免页面误把错误响应当作数据渲染。
 */
export async function requestEnvelope<TResponse, TBody = unknown>(
  path: string,
  options: ApiRequestOptions<TBody> = {},
): Promise<ApiEnvelope<TResponse>> {
  const payload = await request<unknown, TBody>(path, options);
  if (!isRecord(payload) || !("data" in payload)) {
    throw new ApiError("服务响应缺少 data 字段。", {
      status: 200,
      code: "INVALID_RESPONSE",
      requestId: options.requestId,
      details: payload,
    });
  }

  const meta = isRecord(payload.meta) ? payload.meta : {};
  const nextCursor = typeof meta.next_cursor === "string" ? meta.next_cursor : null;

  return {
    data: payload.data as TResponse,
    meta: {
      request_id: typeof meta.request_id === "string" ? meta.request_id : "",
      next_cursor: nextCursor,
    },
  };
}

export const apiClient = { request, requestEnvelope };
