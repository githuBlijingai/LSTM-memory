"use client";

import {
  ApartmentOutlined,
  BookOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  HourglassOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Col, Progress, Row, Statistic, Tag } from "antd";
import Link from "next/link";
import type { ReactNode } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { getDashboardSummary } from "@/lib/api/endpoints";
import { formatNumber } from "@/lib/format";
import { eventStatusMeta, jobStatusMeta } from "@/lib/labels";

function sum(values: Record<string, number> | undefined) {
  return Object.values(values ?? {}).reduce((total, value) => total + (value ?? 0), 0);
}

function StatCard({
  title,
  value,
  icon,
  suffix,
  hint,
}: {
  title: string;
  value: number | string;
  icon: ReactNode;
  suffix?: string;
  hint?: ReactNode;
}) {
  return (
    <Card className="stat-card">
      <div className="stat-card-body">
        <span className="stat-icon" aria-hidden="true">{icon}</span>
        <Statistic title={title} value={value} suffix={suffix} />
      </div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </Card>
  );
}

function DistributionCard({
  title,
  data,
  metaOf,
}: {
  title: string;
  data: Record<string, number>;
  metaOf: (status: string) => { label: string; color: string };
}) {
  const entries = Object.entries(data).filter(([, count]) => count > 0);
  const total = entries.reduce((sumValue, [, count]) => sumValue + count, 0);

  return (
    <Card className="panel-card" title={title}>
      {entries.length === 0 ? (
        <p className="muted">暂无记录</p>
      ) : (
        <div className="distribution-list">
          {entries.map(([status, count]) => {
            const meta = metaOf(status);
            return (
              <div className="distribution-row" key={status}>
                <div className="distribution-label">
                  <Tag color={meta.color}>{meta.label}</Tag>
                  <span className="muted">{formatNumber(count)}</span>
                </div>
                <Progress
                  percent={total > 0 ? Math.round((count / total) * 1000) / 10 : 0}
                  showInfo={false}
                  size="small"
                  strokeColor="#315efb"
                />
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function DashboardView() {
  const query = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: getDashboardSummary,
    staleTime: 30_000,
  });

  const summary = query.data?.data;
  const eventTotal = sum(summary?.events);
  const jobTotal = sum(summary?.jobs);
  const failedJobs = (summary?.jobs?.failed ?? 0) + (summary?.jobs?.dead_letter ?? 0);
  const runningJobs = (summary?.jobs?.running ?? 0) + (summary?.jobs?.queued ?? 0);
  const alertTotal = sum(summary?.alerts);

  return (
    <div className="page-container">
      <PageHeader
        description="汇总记忆数量、任务状态、Wiki 新鲜度与失败告警；图表只读取汇总接口，不在浏览器计算全量事件。"
        title="概览"
      />

      <QueryState
        error={query.error}
        isLoading={query.isLoading}
        loadingLabel="正在读取概览汇总…"
        onRetry={() => void query.refetch()}
      >
        {summary ? (
          <>
            {alertTotal > 0 ? (
              <Alert
                action={<Link href="/jobs/wiki-sync">查看同步任务</Link>}
                description="死信任务会阻塞后续写链路；请按任务 ID 查询状态并重试，或检查 Worker 日志。"
                title={`当前存在 ${alertTotal} 项失败告警`}
                showIcon
                type="error"
              />
            ) : (
              <Alert showIcon title="当前没有失败告警" type="success" />
            )}

            <Row gutter={[16, 16]}>
              <Col lg={6} sm={12} xs={24}>
                <StatCard
                  hint={`生效 ${formatNumber(summary.events.active ?? 0)} · 待处理 ${formatNumber(summary.events.pending ?? 0)}`}
                  icon={<ApartmentOutlined />}
                  title="情景记忆总数"
                  value={eventTotal}
                />
              </Col>
              <Col lg={6} sm={12} xs={24}>
                <StatCard
                  hint={
                    <>
                      最近同步：<DateTimeText value={String(summary.wiki.last_sync_at ?? "")} />
                    </>
                  }
                  icon={<BookOutlined />}
                  title="Wiki 生效词条"
                  value={formatNumber(Number(summary.wiki.active_articles ?? 0))}
                />
              </Col>
              <Col lg={6} sm={12} xs={24}>
                <StatCard
                  hint={`运行/排队 ${formatNumber(runningJobs)}`}
                  icon={<SyncOutlined />}
                  title="后台任务总数"
                  value={jobTotal}
                />
              </Col>
              <Col lg={6} sm={12} xs={24}>
                <StatCard
                  hint="含 failed 与 dead_letter"
                  icon={failedJobs > 0 ? <ExclamationCircleOutlined /> : <CheckCircleOutlined />}
                  title="失败任务"
                  value={failedJobs}
                />
              </Col>
            </Row>

            <Row gutter={[16, 16]}>
              <Col lg={12} xs={24}>
                <DistributionCard
                  data={summary.events}
                  metaOf={eventStatusMeta}
                  title="事件状态分布"
                />
              </Col>
              <Col lg={12} xs={24}>
                <DistributionCard
                  data={summary.jobs}
                  metaOf={jobStatusMeta}
                  title="任务状态分布"
                />
              </Col>
            </Row>

            <Row gutter={[16, 16]}>
              <Col lg={12} xs={24}>
                <Card className="panel-card" title="Wiki 新鲜度">
                  <dl className="detail-list">
                    <div>
                      <dt>生效词条</dt>
                      <dd>{formatNumber(Number(summary.wiki.active_articles ?? 0))}</dd>
                    </div>
                    <div>
                      <dt>生效知识块</dt>
                      <dd>{formatNumber(Number(summary.wiki.active_chunks ?? 0))}</dd>
                    </div>
                    <div>
                      <dt>最近同步</dt>
                      <dd>
                        <DateTimeText value={summary.wiki.last_sync_at as string | null} />
                      </dd>
                    </div>
                    <div>
                      <dt>Outbox 待投递</dt>
                      <dd>{formatNumber(summary.outbox.pending ?? 0)}</dd>
                    </div>
                  </dl>
                </Card>
              </Col>
              <Col lg={12} xs={24}>
                <Card className="panel-card" title="失败与告警">
                  <dl className="detail-list">
                    <div>
                      <dt>死信任务</dt>
                      <dd>{formatNumber(summary.alerts.dead_letter_jobs ?? 0)}</dd>
                    </div>
                    <div>
                      <dt>死信 Outbox</dt>
                      <dd>{formatNumber(summary.alerts.dead_letter_outbox ?? 0)}</dd>
                    </div>
                    <div>
                      <dt>失败事件</dt>
                      <dd>{formatNumber(summary.alerts.failed_events ?? 0)}</dd>
                    </div>
                    <div>
                      <dt>无帮助反馈</dt>
                      <dd>{formatNumber(summary.alerts.not_helpful_feedback ?? 0)}</dd>
                    </div>
                  </dl>
                </Card>
              </Col>
            </Row>

            <Card className="panel-card" title="入口">
              <div className="quick-links">
                <Link href="/search">统一搜索</Link>
                <Link href="/memories/events">情景记忆</Link>
                <Link href="/entities">实体状态</Link>
                <Link href="/wiki">Wiki 知识</Link>
                <Link href="/jobs/wiki-sync">Wiki 同步</Link>
              </div>
              <p className="muted">
                <HourglassOutlined /> 汇总数据每 30 秒按需刷新；失败明细请按任务 ID 查询。
              </p>
            </Card>
          </>
        ) : null}
      </QueryState>
    </div>
  );
}
