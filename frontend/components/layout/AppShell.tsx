"use client";

import {
  ApartmentOutlined,
  BookOutlined,
  DashboardOutlined,
  HistoryOutlined,
  MenuOutlined,
  SearchOutlined,
  SyncOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Badge, Button, Drawer, Input, Menu, Tag, Tooltip } from "antd";
import type { MenuProps } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { SessionProvider } from "@/components/providers/SessionProvider";
import { getDashboardSummary } from "@/lib/api/endpoints";
import type { SessionView } from "@/lib/auth";

const navigationItems = [
  { href: "/dashboard", label: "概览", icon: <DashboardOutlined /> },
  { href: "/search", label: "统一搜索", icon: <SearchOutlined /> },
  { href: "/memories/events", label: "情景记忆", icon: <HistoryOutlined /> },
  { href: "/entities", label: "实体状态", icon: <ApartmentOutlined /> },
  { href: "/wiki", label: "Wiki 知识", icon: <BookOutlined /> },
  { href: "/jobs/wiki-sync", label: "Wiki 同步", icon: <SyncOutlined /> },
];

function Brand() {
  return (
    <Link className="brand" href="/dashboard" aria-label="分层记忆系统首页">
      <span className="brand-mark" aria-hidden="true">M</span>
      <span className="brand-copy">
        <strong>分层记忆系统</strong>
        <span>MEMORY CONSOLE</span>
      </span>
    </Link>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const selectedKey = navigationItems.find(
    ({ href }) => pathname === href || pathname.startsWith(`${href}/`),
  )?.href;

  const items: MenuProps["items"] = navigationItems.map(({ href, label, icon }) => ({
    key: href,
    icon,
    label: <Link href={href}>{label}</Link>,
  }));

  return (
    <nav aria-label="主导航">
      <Menu
        className="app-navigation"
        items={items}
        mode="inline"
        onClick={onNavigate}
        selectedKeys={selectedKey ? [selectedKey] : []}
      />
    </nav>
  );
}

function TaskFailureNotice() {
  const { data } = useQuery({
    queryKey: ["dashboard", "summary", "topbar"],
    queryFn: getDashboardSummary,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  if (!data) {
    return null;
  }

  const alerts = data.data.alerts ?? {};
  const failed =
    (alerts.dead_letter_jobs ?? 0) +
    (alerts.dead_letter_outbox ?? 0) +
    (alerts.failed_events ?? 0);

  if (failed <= 0) {
    return <span className="task-notice">当前无失败告警</span>;
  }

  return (
    <Tooltip title="死信任务、死信 Outbox 与失败事件汇总">
      <Link className="task-notice task-notice-alert" href="/dashboard">
        <Badge count={failed} size="small" />
        <span>失败告警</span>
      </Link>
    </Tooltip>
  );
}

function useOnlineStatus() {
  const [online, setOnline] = useState(true);

  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  return online;
}

interface AppShellProps {
  children: ReactNode;
  environment: string;
  session?: SessionView | null;
  devAuthEnabled?: boolean;
}

export function AppShell({
  children,
  environment,
  session = null,
  devAuthEnabled = false,
}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false);
  const router = useRouter();
  const online = useOnlineStatus();
  const roles = session?.user.roles.join(" / ");

  return (
    <SessionProvider session={session}>
      <div className="app-shell">
        <a className="skip-link" href="#main-content">跳至主内容</a>
        <aside className="app-sidebar">
          <Brand />
          <Navigation />
          <div className="sidebar-footer">M3 · 控制台</div>
        </aside>

        <div className="app-workspace">
          <header className="app-topbar">
            <div className="topbar-group">
              <Button
                aria-label="打开导航"
                className="mobile-nav-button"
                icon={<MenuOutlined />}
                onClick={() => setNavigationOpen(true)}
                type="text"
              />
              <Input.Search
                aria-label="全局搜索"
                className="global-search-input"
                onSearch={(value) => {
                  const query = value.trim();
                  router.push(query ? `/search?q=${encodeURIComponent(query)}` : "/search");
                }}
                placeholder="搜索记忆与 Wiki，回车跳转"
              />
            </div>

            <div className="topbar-group topbar-actions">
              <Tag className="environment-tag" color={devAuthEnabled ? "orange" : "blue"}>
                {environment}
              </Tag>
              {devAuthEnabled ? <Tag color="warning">DEV AUTH</Tag> : null}
              {session ? <TaskFailureNotice /> : null}
              <div className="user-area">
                <UserOutlined aria-hidden="true" />
                <span className="user-copy">
                  <strong>{session?.user.name ?? "认证待接入"}</strong>
                  <span>{roles ?? "未建立会话"}</span>
                </span>
              </div>
            </div>
          </header>

          {online ? null : (
            <Alert
              banner
              title="网络已断开，恢复连接后请点击页面中的“重新加载”重试。"
              type="warning"
            />
          )}

          <main className="app-main" id="main-content">{children}</main>
        </div>

        <Drawer
          onClose={() => setNavigationOpen(false)}
          open={navigationOpen}
          placement="left"
          styles={{ body: { padding: 0 } }}
          size={280}
          title="分层记忆系统"
        >
          <Navigation onNavigate={() => setNavigationOpen(false)} />
        </Drawer>
      </div>
    </SessionProvider>
  );
}
