import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryState } from "@/components/common/QueryState";
import { ApiError } from "@/lib/api/client";

function errorOf(status: number, message = "请求失败") {
  return new ApiError(message, { status, code: "TEST", requestId: "req-1" });
}

describe("QueryState", () => {
  it("根据 401/403 渲染登录与权限状态", () => {
    const { rerender } = render(
      <QueryState error={errorOf(401)} isLoading={false}>
        <div>ok</div>
      </QueryState>,
    );
    expect(screen.getByText("需要登录")).toBeInTheDocument();

    rerender(
      <QueryState error={errorOf(403)} isLoading={false}>
        <div>ok</div>
      </QueryState>,
    );
    expect(screen.getByText("无权访问")).toBeInTheDocument();
  });

  it("默认把 404 当作内容不存在，Wiki 可切换为接口未就绪", () => {
    const { rerender } = render(
      <QueryState error={errorOf(404)} isLoading={false}>
        <div>ok</div>
      </QueryState>,
    );
    expect(screen.getByText("内容不存在")).toBeInTheDocument();

    rerender(
      <QueryState error={errorOf(404)} isLoading={false} notFoundAsUnavailable>
        <div>ok</div>
      </QueryState>,
    );
    expect(screen.getByText("接口尚未就绪")).toBeInTheDocument();
  });

  it("普通错误展示后端消息与请求 ID", () => {
    render(
      <QueryState error={errorOf(500, "内部错误")} isLoading={false} onRetry={vi.fn()}>
        <div>ok</div>
      </QueryState>,
    );
    expect(screen.getByText("内部错误")).toBeInTheDocument();
    expect(screen.getByText(/req-1/)).toBeInTheDocument();
  });

  it("有数据时渲染子节点", () => {
    render(
      <QueryState error={null} isLoading={false}>
        <div>业务内容</div>
      </QueryState>,
    );
    expect(screen.getByText("业务内容")).toBeInTheDocument();
  });
});
