import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/layout/AppShell";
import { ErrorState } from "@/components/states/StatePanels";

const navigation = vi.hoisted(() => ({
  usePathname: vi.fn(),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn() })),
}));

vi.mock("next/navigation", () => ({
  usePathname: navigation.usePathname,
  useRouter: navigation.useRouter,
}));

describe("基础布局与通用状态", () => {
  beforeEach(() => {
    navigation.usePathname.mockReturnValue("/memories/events/event-1");
  });

  it("呈现六项导航、当前路由、环境区域和主内容", () => {
    render(
      <AppShell devAuthEnabled={false} environment="test">
        <div>页面内容</div>
      </AppShell>,
    );

    const mainNavigation = screen.getByRole("navigation", { name: "主导航" });
    const links = within(mainNavigation).getAllByRole("link");
    expect(links).toHaveLength(6);
    expect(links.map((link) => link.textContent)).toEqual([
      "概览",
      "统一搜索",
      "情景记忆",
      "实体状态",
      "Wiki 知识",
      "Wiki 同步",
    ]);
    expect(within(mainNavigation).getByRole("menuitem", { name: /情景记忆$/ }))
      .toHaveClass("ant-menu-item-selected");
    expect(screen.getByText("test")).toBeVisible();
    expect(screen.getByText("认证待接入")).toBeVisible();
    expect(screen.getByText("页面内容")).toBeVisible();
  });

  it("错误状态展示请求 ID 并触发重试", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<ErrorState onRetry={onRetry} requestId="req-test-001" />);

    expect(screen.getByText("请求 ID：req-test-001")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
