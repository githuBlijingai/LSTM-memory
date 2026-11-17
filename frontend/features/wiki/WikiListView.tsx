"use client";

import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, Row, Select, Space, Tag, Typography } from "antd";
import Link from "next/link";
import { useState } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { listWikiArticles } from "@/lib/api/endpoints";
import type { WikiArticleSummary } from "@/lib/api/contracts";
import { wikiArticleStatusMeta } from "@/lib/labels";

const STATUS_OPTIONS = [
  { label: "生效", value: "active" },
  { label: "重定向", value: "redirected" },
  { label: "已删除", value: "deleted" },
];

const LANGUAGE_OPTIONS = [
  { label: "中文", value: "zh" },
  { label: "英文", value: "en" },
  { label: "日文", value: "ja" },
];

export function WikiListView() {
  const [queryText, setQueryText] = useState("");
  const [category, setCategory] = useState("");
  const [language, setLanguage] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const [filters, setFilters] = useState<{
    q?: string;
    category?: string;
    language?: string;
    status?: string;
  }>({});

  const query = useInfiniteQuery({
    queryKey: ["wiki", "articles", filters],
    queryFn: ({ pageParam }) =>
      listWikiArticles({ ...filters, cursor: pageParam ?? undefined, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.meta.next_cursor,
  });

  const articles = query.data?.pages.flatMap((page) => page.data) ?? [];

  function reset() {
    setQueryText("");
    setCategory("");
    setLanguage(undefined);
    setStatus(undefined);
    setFilters({});
  }

  return (
    <div className="page-container">
      <PageHeader
        description="词条搜索、分类/语言/状态筛选与分页；只展示后端返回的生效内容。"
        title="Wiki 知识"
      />

      <Card className="filter-card">
        <Form
          layout="inline"
          onFinish={() =>
            setFilters({
              q: queryText.trim() || undefined,
              category: category.trim() || undefined,
              language,
              status,
            })
          }
        >
          <Form.Item label="词条">
            <Input
              allowClear
              onChange={(event) => setQueryText(event.target.value)}
              placeholder="标题或关键词"
              value={queryText}
            />
          </Form.Item>
          <Form.Item label="分类">
            <Input
              allowClear
              onChange={(event) => setCategory(event.target.value)}
              placeholder="分类名"
              value={category}
            />
          </Form.Item>
          <Form.Item label="语言">
            <Select
              allowClear
              onChange={setLanguage}
              options={LANGUAGE_OPTIONS}
              placeholder="全部语言"
              style={{ minWidth: 130 }}
              value={language}
            />
          </Form.Item>
          <Form.Item label="状态">
            <Select
              allowClear
              onChange={setStatus}
              options={STATUS_OPTIONS}
              placeholder="全部状态"
              style={{ minWidth: 130 }}
              value={status}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button htmlType="submit" icon={<SearchOutlined />} type="primary">
                搜索
              </Button>
              <Button icon={<ReloadOutlined />} onClick={reset}>
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      <Card className="panel-card">
        <QueryState
          emptyDescription="调整关键词或筛选条件后重试；不会自动放宽筛选。"
          emptyTitle="没有符合条件的词条"
          error={query.error}
          isEmpty={!query.isLoading && articles.length === 0}
          isLoading={query.isLoading}
          loadingLabel="正在读取 Wiki 词条…"
          notFoundAsUnavailable
          onRetry={() => void query.refetch()}
          unavailableDescription="后端 Wiki 接口（AG-04）尚未实现或未启用；页面不会展示模拟词条。接口就绪后无需改前端即可显示真实数据。"
          unavailableTitle="Wiki 接口尚未就绪"
        >
          <Row gutter={[16, 16]}>
            {articles.map((article) => (
              <Col key={article.article_id} lg={8} md={12} xs={24}>
                <WikiCard article={article} />
              </Col>
            ))}
          </Row>
          <div className="load-more">
            {query.hasNextPage ? (
              <Button
                loading={query.isFetchingNextPage}
                onClick={() => void query.fetchNextPage()}
              >
                加载下一批
              </Button>
            ) : (
              <span className="muted">已到末页（本页共 {articles.length} 条）</span>
            )}
          </div>
        </QueryState>
      </Card>
    </div>
  );
}

function WikiCard({ article }: { article: WikiArticleSummary }) {
  const statusMeta = wikiArticleStatusMeta(article.status);

  return (
    <Card className="wiki-card" hoverable>
      <div className="wiki-card-head">
        <Link className="result-title" href={`/wiki/${article.article_id}`}>
          {article.title}
        </Link>
        <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
      </div>
      <Typography.Paragraph className="result-content" ellipsis={{ rows: 3 }}>
        {article.summary || "（无摘要）"}
      </Typography.Paragraph>
      <div className="wiki-card-meta">
        <Space size={4} wrap>
          <Tag>{article.language}</Tag>
          {article.domain ? <Tag color="purple">{article.domain}</Tag> : null}
          {article.categories.slice(0, 3).map((item) => (
            <Tag key={item}>{item}</Tag>
          ))}
        </Space>
      </div>
      <div className="result-meta">
        <span>来源：{article.source}</span>
        <span>修订：{article.revision_id}</span>
        <span>知识块：{article.chunk_count}</span>
        <span>
          同步：<DateTimeText value={article.sync_time} />
        </span>
      </div>
    </Card>
  );
}
