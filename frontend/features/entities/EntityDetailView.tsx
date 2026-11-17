"use client";

import { ArrowLeftOutlined, BuildOutlined } from "@ant-design/icons";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Modal,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { CopyButton } from "@/components/common/CopyButton";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { useHasRole } from "@/components/providers/SessionProvider";
import {
  getEntity,
  getEntityState,
  listEntityEvents,
  listEntityReplays,
  rebuildEntity,
} from "@/lib/api/endpoints";
import type { EventSummary, ReplayLog } from "@/lib/api/contracts";
import { formatNumber } from "@/lib/format";
import {
  entityStatusMeta,
  eventStatusMeta,
  jobStatusMeta,
  visibilityMeta,
} from "@/lib/labels";

export function EntityDetailView() {
  const params = useParams<{ id: string }>();
  const entityId = params.id;
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const canRebuild = useHasRole("admin");
  const [rebuildOpen, setRebuildOpen] = useState(false);

  const entityQuery = useQuery({
    queryKey: ["entity", entityId],
    queryFn: () => getEntity(entityId),
    enabled: Boolean(entityId),
  });

  const stateQuery = useQuery({
    queryKey: ["entity", entityId, "state"],
    queryFn: () => getEntityState(entityId),
    enabled: Boolean(entityId),
  });

  const replaysQuery = useQuery({
    queryKey: ["entity", entityId, "replays"],
    queryFn: () => listEntityReplays(entityId, 20),
    enabled: Boolean(entityId),
  });

  const eventsQuery = useInfiniteQuery({
    queryKey: ["entity", entityId, "events"],
    queryFn: ({ pageParam }) =>
      listEntityEvents(entityId, { cursor: pageParam ?? undefined, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor,
    enabled: Boolean(entityId),
  });

  const rebuildMutation = useMutation({
    mutationFn: () => rebuildEntity(entityId),
    onSuccess: async (result) => {
      message.success(`重建任务已受理：${result.data.job_id}`);
      setRebuildOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["entity", entityId] });
    },
    onError: (error: Error) => message.error(error.message),
  });

  const entity = entityQuery.data?.data;
  const state = stateQuery.data?.data;
  const replays = replaysQuery.data?.data ?? [];
  const events = eventsQuery.data?.pages.flatMap((page) => page.data) ?? [];

  const replayColumns: ColumnsType<ReplayLog> = [
    {
      title: "原因",
      dataIndex: "reason",
      render: (value: string) => <Tag>{value}</Tag>,
    },
    { title: "事件", dataIndex: "event_id", render: (value: string) => (
      <Link className="table-link" href={`/memories/events/${value}`}>查看事件</Link>
    ) },
    {
      title: "版本",
      key: "version",
      render: (_, record) => `${record.before_state_version} → ${record.after_state_version ?? "—"}`,
    },
    { title: "模型", dataIndex: "model_version" },
    {
      title: "状态",
      dataIndex: "status",
      render: (value: string) => {
        const meta = jobStatusMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: "尝试", dataIndex: "attempt_count", width: 70 },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 180,
      render: (value: string) => <DateTimeText value={value} />,
    },
    {
      title: "完成时间",
      dataIndex: "completed_at",
      width: 180,
      render: (value: string | null) => <DateTimeText value={value} />,
    },
    {
      title: "错误",
      dataIndex: "error_message",
      render: (value: string | null) => value ?? "—",
    },
  ];

  return (
    <div className="page-container">
      <PageHeader
        description="查看可读摘要、来源事件、状态版本与重放记录；重建仅 admin 可触发。"
        extra={
          <Space>
            <Link href="/entities">
              <Button icon={<ArrowLeftOutlined />}>返回列表</Button>
            </Link>
            {canRebuild && entity ? (
              <Button icon={<BuildOutlined />} onClick={() => setRebuildOpen(true)}>
                触发重建
              </Button>
            ) : null}
          </Space>
        }
        title="实体详情"
      />

      <QueryState
        error={entityQuery.error}
        isLoading={entityQuery.isLoading}
        loadingLabel="正在读取实体详情…"
        onRetry={() => void entityQuery.refetch()}
      >
        {entity ? (
          <>
            <Card className="panel-card">
              <Space align="center" size={10} wrap>
                <h2>{entity.entity_name}</h2>
                <Tag color={entityStatusMeta(entity.status).color}>
                  {entityStatusMeta(entity.status).label}
                </Tag>
                <Tag>{entity.entity_type}</Tag>
                <Tag color={visibilityMeta(entity.visibility).color}>
                  {visibilityMeta(entity.visibility).label}
                </Tag>
              </Space>
              <p className="muted detail-subtitle">
                实体 ID：<code>{entity.entity_id}</code>
                <CopyButton text={entity.entity_id} label="复制实体 ID" />
              </p>

              <Descriptions
                bordered
                column={{ lg: 3, md: 2, sm: 1, xs: 1 }}
                items={[
                  { key: "version", label: "状态版本", children: entity.state_version },
                  { key: "model", label: "模型版本", children: entity.model_version ?? "—" },
                  { key: "sequence", label: "最近序列号", children: entity.last_sequence_no },
                  {
                    key: "update",
                    label: "最近更新",
                    children: <DateTimeText value={entity.last_update_time} />,
                  },
                  {
                    key: "summary-version",
                    label: "摘要版本",
                    children: entity.summary_version ?? "—",
                  },
                  {
                    key: "summary-updated",
                    label: "摘要更新时间",
                    children: <DateTimeText value={entity.summary_updated_at} />,
                  },
                  {
                    key: "last-event",
                    label: "最近事件",
                    children: entity.last_event_id ? (
                      <Link className="table-link" href={`/memories/events/${entity.last_event_id}`}>
                        查看事件
                      </Link>
                    ) : (
                      "—"
                    ),
                  },
                  {
                    key: "last-event-time",
                    label: "最近事件时间",
                    children: <DateTimeText value={entity.last_event_time} />,
                  },
                  { key: "owner", label: "所有者", children: entity.owner_id },
                ]}
              />
            </Card>

            <Card className="panel-card" title="可读摘要">
              <p className="summary-text">{entity.readable_summary || "暂无摘要"}</p>
            </Card>

            <Card className="panel-card" title="来源事件">
              <QueryState
                emptyDescription="该实体还没有关联事件。"
                emptyTitle="暂无来源事件"
                error={eventsQuery.error}
                isEmpty={!eventsQuery.isLoading && events.length === 0}
                isLoading={eventsQuery.isLoading}
                loadingLabel="正在读取来源事件…"
                onRetry={() => void eventsQuery.refetch()}
              >
                <div className="entity-event-list">
                  {events.map((event: EventSummary) => {
                    const meta = eventStatusMeta(event.status);
                    return (
                      <Link
                        className="entity-event-row"
                        href={`/memories/events/${event.event_id}`}
                        key={event.event_id}
                      >
                        <Space size={8} wrap>
                          <Tag color={meta.color}>{meta.label}</Tag>
                          <span className="entity-event-title">{event.title}</span>
                        </Space>
                        <span className="muted">
                          {event.event_type} · <DateTimeText value={event.event_time} />
                        </span>
                      </Link>
                    );
                  })}
                </div>
                {eventsQuery.hasNextPage ? (
                  <div className="load-more">
                    <Button
                      loading={eventsQuery.isFetchingNextPage}
                      onClick={() => void eventsQuery.fetchNextPage()}
                    >
                      加载更多事件
                    </Button>
                  </div>
                ) : null}
              </QueryState>
            </Card>

            <Card className="panel-card" title="状态与重放">
              <QueryState
                error={stateQuery.error ?? replaysQuery.error}
                isLoading={stateQuery.isLoading || replaysQuery.isLoading}
                loadingLabel="正在读取状态与重放记录…"
                onRetry={() => {
                  void stateQuery.refetch();
                  void replaysQuery.refetch();
                }}
              >
                {state ? (
                  <Descriptions
                    bordered
                    column={{ lg: 3, md: 2, sm: 1, xs: 1 }}
                    items={[
                      { key: "state-status", label: "状态", children: state.status },
                      { key: "state-version", label: "状态版本", children: state.state_version },
                      { key: "state-model", label: "模型版本", children: state.model_version ?? "—" },
                      {
                        key: "snapshot",
                        label: "最近快照",
                        children: <DateTimeText value={state.latest_snapshot_at} />,
                      },
                      {
                        key: "pending",
                        label: "待重放",
                        children: formatNumber(state.pending_replay_count),
                      },
                      {
                        key: "rebuild-job",
                        label: "进行中重建任务",
                        children: state.active_rebuild_job_id ? (
                          <Space size={2}>
                            <code>{state.active_rebuild_job_id}</code>
                            <CopyButton text={state.active_rebuild_job_id} label="复制任务 ID" />
                          </Space>
                        ) : (
                          "—"
                        ),
                      },
                      {
                        key: "last-event",
                        label: "最近事件",
                        children: state.last_event_id ? (
                          <Link className="table-link" href={`/memories/events/${state.last_event_id}`}>
                            查看事件
                          </Link>
                        ) : (
                          "—"
                        ),
                      },
                      { key: "sequence", label: "最近序列号", children: state.last_sequence_no },
                      {
                        key: "updated",
                        label: "最近更新",
                        children: <DateTimeText value={state.last_update_time} />,
                      },
                    ]}
                  />
                ) : null}

                <h3 className="section-title">重放记录</h3>
                {replays.length === 0 ? (
                  <p className="muted">暂无重放记录。</p>
                ) : (
                  <Table
                    columns={replayColumns}
                    dataSource={replays}
                    pagination={false}
                    rowKey="replay_id"
                    scroll={{ x: 1100 }}
                    size="small"
                  />
                )}
              </QueryState>
            </Card>
          </>
        ) : null}
      </QueryState>

      <Modal
        confirmLoading={rebuildMutation.isPending}
        okText="触发重建"
        onCancel={() => setRebuildOpen(false)}
        onOk={() => rebuildMutation.mutate()}
        open={rebuildOpen}
        title="触发实体状态重建"
      >
        <Alert
          title="重建会按事件顺序重新计算实体状态，并可能生成新的状态快照。"
          showIcon
          type="warning"
        />
        <p className="muted">
          重建为异步任务，返回任务 ID 后可在状态区域查看进行中的重建任务。
        </p>
      </Modal>
    </div>
  );
}
