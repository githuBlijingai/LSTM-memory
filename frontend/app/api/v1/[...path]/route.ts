import { NextRequest, NextResponse } from "next/server";
import { getServerSession } from "@/lib/auth";

export const runtime = "nodejs";

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

function errorResponse(status: number, code: string, message: string, requestId: string) {
  return NextResponse.json(
    {
      error: { code, message, details: {} },
      meta: { request_id: requestId, next_cursor: null },
    },
    {
      status,
      headers: { "x-request-id": requestId },
    },
  );
}

function getRequestId(request: NextRequest) {
  const incoming = request.headers.get("x-request-id");
  return incoming && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(incoming)
    ? incoming
    : globalThis.crypto.randomUUID();
}

function backendUrl(pathSegments: string[], search: string) {
  const base = new URL(process.env.BACKEND_URL ?? "");
  if (!["http:", "https:"].includes(base.protocol) || base.search || base.hash) {
    throw new TypeError("BACKEND_URL 仅支持不含查询参数的 HTTP(S) 地址。");
  }

  const target = new URL(base.toString());
  const encodedPath = pathSegments.map((segment) => encodeURIComponent(segment)).join("/");
  target.pathname = `${base.pathname.replace(/\/+$/, "")}/api/v1/${encodedPath}`;
  target.search = search;
  return target;
}

async function proxy(request: NextRequest, context: RouteContext) {
  const requestId = getRequestId(request);
  const session = await getServerSession();
  if (!session) {
    return errorResponse(
      401,
      "AUTH_REQUIRED",
      "未建立登录会话；本地开发可显式启用 Dev Auth 并提供令牌。",
      requestId,
    );
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin) {
      return errorResponse(403, "FORBIDDEN", "跨源变更请求已被拒绝。", requestId);
    }
  }

  if (!process.env.BACKEND_URL) {
    return errorResponse(503, "BACKEND_URL_MISSING", "尚未配置后端服务地址。", requestId);
  }

  let target: URL;
  try {
    const { path } = await context.params;
    target = backendUrl(path, new URL(request.url).search);
  } catch {
    return errorResponse(503, "BACKEND_URL_INVALID", "后端服务地址配置无效。", requestId);
  }

  const authorization = session.mode === "dev"
    ? `Dev ${session.user.id}`
    : `Bearer ${session.accessToken}`;

  const headers = new Headers({
    accept: request.headers.get("accept") ?? "application/json",
    authorization,
    "x-request-id": requestId,
  });
  if (session.mode === "dev") {
    headers.set("x-dev-roles", session.user.roles.join(","));
  }
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey) {
    headers.set("idempotency-key", idempotencyKey);
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      body: request.method === "GET" || request.method === "HEAD"
        ? undefined
        : await request.arrayBuffer(),
      cache: "no-store",
      headers,
      method: request.method,
      redirect: "manual",
      signal: request.signal,
    });
  } catch {
    return errorResponse(502, "UPSTREAM_UNAVAILABLE", "暂时无法连接后端服务。", requestId);
  }

  const responseHeaders = new Headers({
    "cache-control": "no-store",
    "x-request-id": requestId,
  });
  const upstreamContentType = upstream.headers.get("content-type");
  if (upstreamContentType) {
    responseHeaders.set("content-type", upstreamContentType);
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export {
  proxy as DELETE,
  proxy as GET,
  proxy as PATCH,
  proxy as POST,
  proxy as PUT,
};
