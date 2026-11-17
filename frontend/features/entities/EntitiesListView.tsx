"use client";

import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Card, Form, Input, Select, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import Link from "next/link";
import { useState } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { listEntities } from "@/lib/api/endpoints";
import type { EntitySummary } from "@/lib/api/contracts";
import { entityStatusMeta, visibilityMeta } from "@/lib/labels";

const STATUS_OPTIONS = [
  { label: "生效", value: "active" },
  { label: "重建中", value: "rebuilding" },
  { label: "已归档", value: "archived" },
];

export function EntitiesListView() {
  const [entityType, setEntityType] = useState("");
  const [status, setStatus] = useState<string | undefined>();
  const [filters, setFilters] = useState<{ entity_type?: string; status?: string }>({});

  const query = useInfiniteQuery({
    queryKey: ["entities", filters],
    queryFn: ({ pageParam }) =>
      listEntities({ ...filters, cursor: pageParam ?? undefined, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor,
  });

  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];

  const columns: ColumnsType<EntitySummary> = [
    {
      title: "实体",
      dataIndex: "entity_name",
      render: (value: string, record) => (
        <Link className="table-link" href={`/entities/${record.entity_id}`}>
          {value}
        </Link>
      ),
    },
    {
      title: "类型",
      dataIndex: "entity_type",
      width: 120,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (value: string) => {
        const meta = entityStatusMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "可读摘要",
      dataIndex: "readable_summary",
      render: (value: string) => (
        <Typography.Paragraph className="table-summary" ellipsis={{ rows: 2 }}>
          {value || "—"}
        </Typography.Paragraph>
      ),
    },
    { title: "状态版本", dataIndex: "state_version", width: 100 },
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
      title: "最近更新",
      dataIndex: "last_update_time",
      width: 180,
      render: (value: string | null) => <DateTimeText value={value} />,
    },
  ];

  return (
    <div className="page-container">
      <PageHeader
        description="按实体类型与状态浏览实体；摘要与状态版本来自后端，不在前端推断。"
        title="实体状态"
      />

      <Card className="filter-card">
        <Form
          layout="inline"
          onFinish={() =>
            setFilters({
              entity_type: entityType.trim() || undefined,
              status,
            })
          }
        >
          <Form.Item label="实体类型">
            <Input
              allowClear
              onChange={(event) => setEntityType(event.target.value)}
              placeholder="如 project、person"
              value={entityType}
            />
          </Form.Item>
          <Form.Item label="状态">
            <Select
              allowClear
              onChange={setStatus}
              options={STATUS_OPTIONS}
              placeholder="全部状态"
              style={{ minWidth: 140 }}
              value={status}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button htmlType="submit" icon={<SearchOutlined />} type="primary">
                筛选
              </Button>
              <Button
                icon={<ReloadOutlined />}
                onClick={() => {
                  setEntityType("");
                  setStatus(undefined);
                  setFilters({});
                }}
              >
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      <Card className="panel-card">
        <QueryState
          emptyDescription="实体在事件写入并完成状态更新后出现。"
          emptyTitle="没有符合条件的实体"
          error={query.error}
          isEmpty={!query.isLoading && rows.length === 0}
          isLoading={query.isLoading}
          loadingLabel="正在读取实体列表…"
          onRetry={() => void query.refetch()}
        >
          <Table
            columns={columns}
            dataSource={rows}
            pagination={false}
            rowKey="entity_id"
            scroll={{ x: 1000 }}
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
              <span className="muted">已到末页（本页共 {rows.length} 条）</span>
            )}
          </div>
        </QueryState>
      </Card>
    </div>
  );
}
