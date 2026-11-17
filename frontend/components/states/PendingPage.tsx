import { Card, Tag } from "antd";
import { EmptyState } from "./StatePanels";

interface PendingPageProps {
  title: string;
  description: string;
}

export function PendingPage({ title, description }: PendingPageProps) {
  return (
    <div className="page-container">
      <header className="page-header">
        <Tag color="gold">AG-05 待实现</Tag>
        <h1>{title}</h1>
        <p>{description}</p>
      </header>
      <Card className="pending-card">
        <EmptyState
          description="当前处于 M0 工程骨架阶段；此页面不会请求或展示模拟业务数据。"
          title="业务功能待实现"
        />
      </Card>
    </div>
  );
}
