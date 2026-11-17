"use client";

import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import Link from "next/link";
import { useState } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { archiveEvent, listEvents } from "@/lib/api/endpoints";
import type { EventSummary } from "@/lib/api/contracts";
import { formatScore } from "@/lib/format";
import { EVENT_STATUS_OPTIONS, eventStatusMeta, visibilityMeta } from "@/lib/labels";

interface AppliedFilters {
  subject?: string;
  event_type?: string;
  status?: string;
  start_time?: string;
  end_time?: string;
}

export function EventsListView() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [subject, setSubject] = useState("");
  const [eventType, setEventType] = useState("");
  const [status, setStatus] = useState<string | undefined>();
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [filters, setFilters] = useState<AppliedFilters>({});
  const [archiveTarget, setArchiveTarget] = useState<EventSummary | null>(null);
  const [archiveReason, setArchiveReason] = useState("");

  const query = useInfiniteQuery({
    queryKey: ["memory", "events", filters],
    queryFn: ({ pageParam }) =>
      listEvents({ ...filters, cursor: pageParam ?? undefined, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor,
  });

  const archiveMutation = useMutation({
    mutationFn: ({ eventId, reason }: { eventId: string; reason: string }) =>
      archiveEvent(eventId, { mode: "archive", reason }),
    onSuccess: async () => {
      message.success("归档请求已受理。");
      setArchiveTarget(null);
      setArchiveReason("");
      await queryClient.invalidateQueries({ queryKey: ["memory", "events"] });
    },
    onError: (error: Error) => message.error(error.message),
  });

  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];

  const columns: ColumnsType<EventSummary> = [
    {
      title: "事件时间",
      dataIndex: "event_time",
      width: 180,
      render: (value: string) => <DateTimeText value={value} />,
    },
    {
      title: "标题",
      dataIndex: "title",
      render: (value: string, record) => (
        <Link className="table-link" href={`/memories/events/${record.event_id}`}>
          {value}
        </Link>
      ),
    },
    { title: "主体", dataIndex: "subject", width: 150 },
    {
      title: "类型",
      dataIndex: "event_type",
      width: 120,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: "重要性",
      dataIndex: "importance",
      width: 90,
      render: (value: number) => formatScore(value, 2),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (value: string) => {
        const meta = eventStatusMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "可见性",
      dataIndex: "visibility",
      width: 90,
      render: (value: string) => {
        const meta = visibilityMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_, record) => (
        <Space size={4}>
          <Link href={`/memories/events/${record.event_id}`}>查看</Link>
          {record.status === "active" ? (
            <Button
              danger
              onClick={() => setArchiveTarget(record)}
              size="small"
              type="link"
            >
              归档
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  function applyFilters() {
    setFilters({
      subject: subject.trim() || undefined,
      event_type: eventType.trim() || undefined,
      status,
      start_time: range?.[0]?.toISOString(),
      end_time: range?.[1]?.toISOString(),
    });
  }

  function resetFilters() {
    setSubject("");
    setEventType("");
    setStatus(undefined);
    setRange(null);
    setFilters({});
  }

  return (
    <div className="page-container">
      <PageHeader
        description="按主体、类型、状态与时间筛选情景记忆；列表使用游标分页，不展示伪造总数。"
        title="情景记忆"
      />

      <Card className="filter-card">
        <Form layout="inline" onFinish={applyFilters}>
          <Form.Item label="主体">
            <Input
              allowClear
              onChange={(event) => setSubject(event.target.value)}
              placeholder="精确匹配主体"
              value={subject}
            />
          </Form.Item>
          <Form.Item label="事件类型">
            <Input
              allowClear
              onChange={(event) => setEventType(event.target.value)}
              placeholder="如 decision"
              value={eventType}
            />
          </Form.Item>
          <Form.Item label="状态">
            <Select
              allowClear
              onChange={setStatus}
              options={EVENT_STATUS_OPTIONS}
              placeholder="全部状态"
              style={{ minWidth: 140 }}
              value={status}
            />
          </Form.Item>
          <Form.Item label="事件时间">
            <DatePicker.RangePicker
              onChange={(value) => setRange(value as [Dayjs | null, Dayjs | null] | null)}
              showTime
              value={range as [Dayjs, Dayjs] | null | undefined}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button htmlType="submit" icon={<SearchOutlined />} type="primary">
                筛选
              </Button>
              <Button icon={<ReloadOutlined />} onClick={resetFilters}>
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      <Card className="panel-card">
        <QueryState
          emptyDescription="调整筛选条件后重试，或确认该时间段内是否已写入事件。"
          emptyTitle="没有符合条件的事件"
          error={query.error}
          isEmpty={!query.isLoading && rows.length === 0}
          isLoading={query.isLoading}
          loadingLabel="正在读取事件列表…"
          onRetry={() => void query.refetch()}
        >
          <Table
            columns={columns}
            dataSource={rows}
            pagination={false}
            rowKey="event_id"
            scroll={{ x: 1100 }}
            size="middle"
          />
          <div className="load-more">
            {query.hasNextPage ? (
              <Button
                loading={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
              >
                加载下一批
              </Button>
            ) : (
              <span className="muted">
                已到末页{query.data ? `（本页共 ${rows.length} 条）` : ""}
              </span>
            )}
          </div>
        </QueryState>
      </Card>

      <Modal
        confirmLoading={archiveMutation.isPending}
        okButtonProps={{ danger: true }}
        okText="提交归档"
        onCancel={() => setArchiveTarget(null)}
        onOk={() => {
          if (!archiveTarget) {
            return;
          }
          const reason = archiveReason.trim();
          if (!reason) {
            message.warning("请填写归档原因。");
            return;
          }
          archiveMutation.mutate({ eventId: archiveTarget.event_id, reason });
        }}
        open={archiveTarget !== null}
        title={`归档事件：${archiveTarget?.title ?? ""}`}
      >
        <p className="muted">
          归档后事件不再参与检索，但保留审计记录。需要彻底删除时由管理员发起合规清除。
        </p>
        <Input.TextArea
          maxLength={500}
          onChange={(event) => setArchiveReason(event.target.value)}
          placeholder="归档原因（必填，最多 500 字）"
          rows={3}
          value={archiveReason}
        />
      </Modal>
    </div>
  );
}
