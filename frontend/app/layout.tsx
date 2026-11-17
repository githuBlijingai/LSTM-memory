import { AntdRegistry } from "@ant-design/nextjs-registry";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { QueryProvider } from "@/components/providers/QueryProvider";
import { getSessionView, isDevAuthEnabled } from "@/lib/auth";
import "./globals.css";

export const metadata: Metadata = {
  title: "分层记忆系统",
  description: "基于 LSTM 的分层记忆系统控制台",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const environment = process.env.NEXT_PUBLIC_APP_ENV ?? "development";
  const session = await getSessionView();

  return (
    <html lang="zh-CN">
      <body>
        <AntdRegistry>
          <ConfigProvider
            locale={zhCN}
            theme={{
              token: {
                borderRadius: 8,
                colorBgLayout: "#f4f6fa",
                colorPrimary: "#315efb",
              },
            }}
          >
            <AntdApp>
              <QueryProvider>
                <AppShell
                  devAuthEnabled={isDevAuthEnabled()}
                  environment={environment}
                  session={session}
                >
                  {children}
                </AppShell>
              </QueryProvider>
            </AntdApp>
          </ConfigProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
