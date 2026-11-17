"use client";

import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
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
  Drawer,
  Form,
  Input,
  Progress,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { useHasRole } from "@/components/providers/SessionProvider";
import {
  createWikiSyncTask,
  getWikiSyncTask,
  listWikiSyncTasks,
  retryWikiSyncTask,
} from "@/lib/api/endpoints";
import type { WikiSyncMode, WikiSyncTask } from "@/lib/api/contracts";
import { formatNumber } from "@/lib/format";
import { jobStatusMeta, wikiSyncModeLabel } from "@/lib/labels";

const ACTIVE_STATUSES = ["queued", "running", "retry_wait"];

const SOURCE_OPTIONS = [
  { label: "中文维基百科", value: "wikipedia_zh" },
  { label: "英文维基百科", value: "wikipedia_en" },
];

function usePageVisible() {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  return visible;
}

export function WikiSyncView() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const canOperate = useHasRole("operator");
  const pageVisible = usePageVisible();
  const [source, setSource] = useState("wikipedia_zh");
  const [mode, setMode] = useState<WikiSyncMode>("incremental");
  const [packageUri, setPackageUri] = useState("");
  const [packageVersion, setPackageVersion] = useState("");
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const requiresPackageUri = mode !== "reembed";
  const canCreate =
    canOperate &&
    packageVersion.trim().length > 0 &&
    (!requiresPackageUri || packageUri.trim().length > 0);

  const listQuery = useInfiniteQuery({
    queryKey: ["wiki", "sync-tasks"],
    queryFn: ({ pageParam }) =>
      listWikiSyncTasks({ cursor: pageParam ?? undefined, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      const hasActive = pages.some((page) =>
        page.data.some((task) => ACTIVE_STATUSES.includes(task.status)),
      );
      return pageVisible && hasActive ? 3000 : false;
    },
  });

  const tasks = listQuery.data?.pages.flatMap((page) => page.data) ?? [];
  const hasActive = tasks.some((task) => ACTIVE_STATUSES.includes(task.status));

  const detailQuery = useQuery({
    queryKey: ["wiki", "sync-task", detailTaskId],
    queryFn: () => getWikiSyncTask(detailTaskId as string),
    enabled: Boolean(detailTaskId),
    refetchInterval: (query) => {
      const status = query.state.data?.data.status;
      return pageVisible && status && ACTIVE_STATUSES.includes(status) ? 3000 : false;
    },
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createWikiSyncTask({
        source,
        mode,
        package_uri: requiresPackageUri ? packageUri.trim() : null,
        package_version: packageVersion.trim(),
      }),
    onSuccess: async (result) => {
      message.success(`同步任务已创建：${result.data.task_id}`);
      await queryClient.invalidateQueries({ queryKey: ["wiki", "sync-tasks"] });
    },
    onError: (error: Error) => message.error(error.message),
  });

  const retryMutation = useMutation({
    mutationFn: (taskId: string) => retryWikiSyncTask(taskId),
    onSuccess: async (result) => {
      message.success(`任务已重新入队：${result.data.task_id}`);
      await queryClient.invalidateQueries({ queryKey: ["wiki", "sync-tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["wiki", "sync-task"] });
    },
    onError: (error: Error) => message.error(error.message),
  });

  const columns: ColumnsType<WikiSyncTask> = [
    {
      title: "任务",
      dataIndex: "task_id",
      render: (value: string) => <code>{value.slice(0, 12)}…</code>,
    },
    { title: "来源", dataIndex: "source", width: 140 },
    {
      title: "模式",
      dataIndex: "mode",
      width: 110,
      render: (value: string) => <Tag>{wikiSyncModeLabel(value)}</Tag>,
    },
    { title: "包版本", dataIndex: "package_version", width: 120 },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: string) => {
        const meta = jobStatusMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "进度",
      key: "progress",
      width: 180,
      render: (_, record) =>
        record.total_count ? (
          <Progress
            percent={Math.min(
              100,
              Math.round((record.processed_count / record.total_count) * 1000) / 10,
            )}
            size="small"
          />
        ) : (
          <span>已处理 {formatNumber(record.processed_count)}</span>
        ),
    },
    {
      title: "成功 / 失败",
      key: "counts",
      width: 130,
      render: (_, record) =>
        `${formatNumber(record.success_count)} / ${formatNumber(record.failed_count)}`,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 180,
      render: (value: string) => <DateTimeText value={value} />,
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_, record) => (
        <Space size={4}>
          <Button onClick={() => setDetailTaskId(record.task_id)} size="small" type="link">
            详情
          </Button>
          {canOperate && ["failed", "dead_letter"].includes(record.status) ? (
            <Button
              loading={retryMutation.isPending && retryMutation.variables === record.task_id}
              onClick={() => retryMutation.mutate(record.task_id)}
              size="small"
              type="link"
            >
              重试
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const detailTask = detailQuery.data?.data;

  return (
    <div className="page-container">
      <PageHeader
        description="导入 Wikipedia 镜像包并跟踪增量/全量同步；任务进行中每 3 秒轮询，页面不可见时暂停。"
        title="Wiki 同步"
      />

      {!canOperate ? (
        <Alert
          title="当前账号为只读权限：可以查看同步任务与错误样本，但不能创建或重试任务。"
          showIcon
          type="info"
        />
      ) : null}

      <Card className="filter-card" title="新建同步任务">
        <Form layout="inline">
          <Form.Item label="来源">
            <Select
              onChange={setSource}
              options={SOURCE_OPTIONS}
              style={{ minWidth: 160 }}
              value={source}
            />
          </Form.Item>
          <Form.Item label="模式">
            <Segmented
              onChange={(value) => setMode(value as WikiSyncMode)}
              options={[
                { label: "增量", value: "incremental" },
                { label: "全量", value: "full" },
                { label: "重新向量化", value: "reembed" },
              ]}
              value={mode}
            />
          </Form.Item>
          <Form.Item label="镜像包 URI">
            <Input
              disabled={!requiresPackageUri}
              onChange={(event) => setPackageUri(event.target.value)}
              placeholder="本地路径或 https://…"
              style={{ width: 260 }}
              value={packageUri}
            />
          </Form.Item>
          <Form.Item label="包版本">
            <Input
              onChange={(event) => setPackageVersion(event.target.value)}
              placeholder="必填，如 2026-11-13"
              style={{ width: 160 }}
              value={packageVersion}
            />
          </Form.Item>
          <Form.Item>
            <Button
              disabled={!canCreate}
              icon={<PlusOutlined />}
              loading={createMutation.isPending}
              onClick={() => createMutation.mutate()}
              type="primary"
            >
              创建任务
            </Button>
          </Form.Item>
        </Form>
        <p className="muted">
          全量同步会重算全部词条与知识块，耗时更长；增量同步按游标继续；重新向量化只重算已有知识块的向量。
        </p>
      </Card>

      <Card className="panel-card">
        <QueryState
          emptyDescription="创建同步任务后，这里会显示进度、包地址与错误样本。"
          emptyTitle="还没有同步任务"
          error={listQuery.error}
          isEmpty={!listQuery.isLoading && tasks.length === 0}
          isLoading={listQuery.isLoading}
          loadingLabel="正在读取同步任务…"
          notFoundAsUnavailable
          onRetry={() => void listQuery.refetch()}
          unavailableDescription="后端 Wiki 同步接口暂时不可用，请检查服务状态后重试。"
          unavailableTitle="Wiki 同步接口不可用"
        >
          <Space className="toolbar" wrap>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void listQuery.refetch()}
            >
              刷新
            </Button>
            {hasActive ? <Tag color="processing">任务进行中，自动轮询</Tag> : null}
            {hasActive && !pageVisible ? (
              <Tag color="orange">页面不可见，轮询已暂停</Tag>
            ) : null}
          </Space>

          <Table
            columns={columns}
            dataSource={tasks}
            pagination={false}
            rowKey="task_id"
            scroll={{ x: 1200 }}
            size="middle"
          />
          <div className="load-more">
            {listQuery.hasNextPage ? (
              <Button
                loading={listQuery.isFetchingNextPage}
                onClick={() => void listQuery.fetchNextPage()}
              >
                加载下一批
              </Button>
            ) : (
              <span className="muted">已到末页（本页共 {tasks.length} 条）</span>
            )}
          </div>
        </QueryState>
      </Card>

      <Drawer
        onClose={() => setDetailTaskId(null)}
        open={detailTaskId !== null}
        size={560}
        title="同步任务详情"
      >
        <QueryState
          error={detailQuery.error}
          isLoading={detailQuery.isLoading}
          loadingLabel="正在读取任务详情…"
          notFoundAsUnavailable
          onRetry={() => void detailQuery.refetch()}
          unavailableDescription="任务不存在，或 Wiki 同步接口暂时不可用。"
          unavailableTitle="任务不可用"
        >
          {detailTask ? (
            <>
              <Descriptions
                bordered
                column={1}
                items={[
                  { key: "id", label: "任务 ID", children: <code>{detailTask.task_id}</code> },
                  {
                    key: "job",
                    label: "任务作业",
                    children: detailTask.job_id ? <code>{detailTask.job_id}</code> : "—",
                  },
                  { key: "source", label: "来源", children: detailTask.source },
                  { key: "mode", label: "模式", children: wikiSyncModeLabel(detailTask.mode) },
                  {
                    key: "status",
                    label: "状态",
                    children: (
                      <Tag color={jobStatusMeta(detailTask.status).color}>
                        {jobStatusMeta(detailTask.status).label}
                      </Tag>
                    ),
                  },
                  { key: "version", label: "包版本", children: detailTask.package_version },
                  {
                    key: "uri",
                    label: "镜像包 URI",
                    children: detailTask.package_uri ? (
                      <code>{detailTask.package_uri}</code>
                    ) : (
                      "—"
                    ),
                  },
                  { key: "requested", label: "发起人", children: detailTask.requested_by },
                  {
                    key: "progress",
                    label: "进度",
                    children: detailTask.total_count
                      ? `${formatNumber(detailTask.processed_count)} / ${formatNumber(detailTask.total_count)}`
                      : `已处理 ${formatNumber(detailTask.processed_count)}`,
                  },
                  {
                    key: "counts",
                    label: "成功 / 失败",
                    children: `${formatNumber(detailTask.success_count)} / ${formatNumber(detailTask.failed_count)}`,
                  },
                  { key: "created", label: "创建时间", children: <DateTimeText value={detailTask.created_at} /> },
                  { key: "started", label: "开始时间", children: <DateTimeText value={detailTask.started_at} /> },
                  { key: "finished", label: "完成时间", children: <DateTimeText value={detailTask.finished_at} /> },
                ]}
              />

              <h3 className="section-title">错误样本</h3>
              {detailTask.error_sample.length ? (
                <Space direction="vertical" style={{ width: "100%" }}>
                  {detailTask.error_sample.map((item, index) => (
                    <Alert
                      description={item.message}
                      key={`${item.code}-${item.source_item_id ?? index}`}
                      title={
                        <Space size={6} wrap>
                          <Tag color="red">{item.code}</Tag>
                          <span>{item.source_item_id ?? "未知来源条目"}</span>
                        </Space>
                      }
                      type="error"
                    />
                  ))}
                </Space>
              ) : (
                <p className="muted">该任务没有记录错误样本。</p>
              )}

              {canOperate && detailTask.retryable ? (
                <Button
                  className="drawer-action"
                  loading={retryMutation.isPending}
                  onClick={() => retryMutation.mutate(detailTask.task_id)}
                  type="primary"
                >
                  重新入队
                </Button>
              ) : null}
            </>
          ) : null}
        </QueryState>
      </Drawer>
    </div>
  );
}
