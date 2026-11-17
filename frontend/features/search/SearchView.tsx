"use client";

import { ClearOutlined, LinkOutlined, SearchOutlined } from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Segmented,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  keepPreviousData,
  useMutation,
  useQuery,
} from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useRef, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { searchMemories, submitFeedback } from "@/lib/api/endpoints";
import type {
  FeedbackType,
  SearchResult,
  SearchResultBase,
} from "@/lib/api/contracts";
import { formatScore, toIso } from "@/lib/format";
import {
  feedbackReasonLabel,
  intentLabelOf,
  scoreLabel,
} from "@/lib/labels";

interface SearchFormState {
  query: string;
  memoryType: "all" | "event" | "wiki";
  subject: string;
  eventTypes: string;
  wikiCategories: string;
  topN: number;
  enableFamiliarity: boolean;
  startTime?: Dayjs | null;
  endTime?: Dayjs | null;
}

const DEFAULT_FORM: SearchFormState = {
  query: "",
  memoryType: "all",
  subject: "",
  eventTypes: "",
  wikiCategories: "",
  topN: 10,
  enableFamiliarity: true,
  startTime: null,
  endTime: null,
};

function splitList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

interface FeedbackState {
  type: FeedbackType;
  counted: boolean;
  reason: string | null;
}

function ResultCard({
  result,
  queryId,
  feedback,
  onFeedback,
}: {
  result: SearchResult;
  queryId: string | undefined;
  feedback: FeedbackState | undefined;
  onFeedback: (result: SearchResult, type: FeedbackType) => void;
}) {
  const [scoreOpen, setScoreOpen] = useState(false);
  const clickedRef = useRef(false);

  const isWiki = result.memory_type === "wiki";
  const href = isWiki
    ? `/wiki/${result.article_id}?chunk=${result.chunk_index}`
    : `/memories/events/${result.memory_id}`;

  const consolidationLocked =
    feedback?.type === "adopted" ||
    feedback?.type === "helpful" ||
    feedback?.type === "not_helpful";

  function markClicked() {
    if (clickedRef.current || !queryId) {
      return;
    }
    clickedRef.current = true;
    onFeedback(result, "clicked");
  }

  return (
    <Card className="result-card">
      <div className="result-head">
        <Space align="center" size={8} wrap>
          <Tag color={isWiki ? "cyan" : "blue"}>
            {isWiki ? "Wiki 知识" : "情景记忆"}
          </Tag>
          <Link className="result-title" href={href} onClick={markClicked}>
            {result.title}
          </Link>
        </Space>
        <Tooltip title="最终排序分">
          <span className="result-score">{formatScore(result.final_score)}</span>
        </Tooltip>
      </div>

      <Typography.Paragraph className="result-content" ellipsis={{ rows: 3 }}>
        {result.content}
      </Typography.Paragraph>

      <div className="result-meta">
        {isWiki ? (
          <>
            <span>来源：{result.source.name}</span>
            <span>修订：{result.source.revision_id}</span>
            {result.heading_path.length > 0 ? (
              <span>路径：{result.heading_path.join(" / ")}</span>
            ) : null}
          </>
        ) : (
          <>
            <span>事件时间：{new Date(result.event_time).toLocaleString("zh-CN")}</span>
            <span>模型：{result.model_version ?? "—"}</span>
          </>
        )}
      </div>

      <div className="result-actions">
        <Space size={4} wrap>
          <Link href={href} onClick={markClicked}>
            <Button size="small" type="link">打开</Button>
          </Link>
          {isWiki ? (
            <Button
              href={result.source.url}
              icon={<LinkOutlined />}
              rel="noopener noreferrer"
              size="small"
              target="_blank"
              type="link"
            >
              原始来源
            </Button>
          ) : null}
          <Button
            disabled={!queryId || consolidationLocked}
            onClick={() => onFeedback(result, "adopted")}
            size="small"
            type={feedback?.type === "adopted" ? "primary" : "default"}
          >
            采用
          </Button>
          <Button
            disabled={!queryId || consolidationLocked}
            onClick={() => onFeedback(result, "helpful")}
            size="small"
            type={feedback?.type === "helpful" ? "primary" : "default"}
          >
            有用
          </Button>
          <Button
            disabled={!queryId || consolidationLocked}
            onClick={() => onFeedback(result, "not_helpful")}
            size="small"
            type={feedback?.type === "not_helpful" ? "primary" : "default"}
          >
            无帮助
          </Button>
          <Button onClick={() => setScoreOpen(true)} size="small" type="link">
            分数详情
          </Button>
        </Space>
        {feedback ? (
          <span className={feedback.counted ? "feedback-ok" : "feedback-skip"}>
            {feedback.counted
              ? "已反馈并计入"
              : `已反馈但未计入：${feedbackReasonLabel(feedback.reason) ?? "未说明"}`}
          </span>
        ) : null}
      </div>

      <Drawer
        onClose={() => setScoreOpen(false)}
        open={scoreOpen}
        size={420}
        title="分数解释"
      >
        <Descriptions
          column={1}
          items={[
            { key: "final", label: "最终分", children: formatScore(result.final_score) },
            { key: "model", label: "模型版本", children: result.model_version ?? "—" },
            {
              key: "feedback",
              label: "反馈状态",
              children: result.feedback_state ?? "—",
            },
            ...Object.entries(result.score_breakdown ?? {}).map(([key, value]) => ({
              key,
              label: scoreLabel(key),
              children: typeof value === "number" ? formatScore(value) : "—",
            })),
          ]}
        />
        <p className="muted">
          分数来自检索接口；前端不做二次加权，也不在浏览器重算排序。
        </p>
      </Drawer>
    </Card>
  );
}

interface AppliedSearch extends Omit<SearchFormState, "startTime" | "endTime"> {
  startTime: Date | null;
  endTime: Date | null;
}

function SearchControls({
  initial,
  onClear,
  onSubmit,
}: {
  initial: AppliedSearch;
  onClear: () => void;
  onSubmit: (form: SearchFormState) => void;
}) {
  const [form, setForm] = useState<SearchFormState>(() => ({
    ...DEFAULT_FORM,
    ...initial,
    topN: Number.isFinite(initial.topN) ? initial.topN : 10,
    startTime: initial.startTime ? dayjs(initial.startTime) : null,
    endTime: initial.endTime ? dayjs(initial.endTime) : null,
  }));

  return (
    <Card className="filter-card">
      <Form
        layout="vertical"
        onFinish={() => onSubmit(form)}
      >
        <div className="search-row">
          <Input
            allowClear
            onChange={(event) => setForm({ ...form, query: event.target.value })}
            onPressEnter={() => onSubmit(form)}
            placeholder="例如：项目 Alpha 之前确定的检索阈值是什么？"
            prefix={<SearchOutlined />}
            size="large"
            value={form.query}
          />
          <Button htmlType="submit" size="large" type="primary">
            搜索
          </Button>
        </div>

        <div className="filter-grid">
          <Form.Item label="记忆类型">
            <Segmented
              onChange={(value) =>
                setForm({ ...form, memoryType: value as SearchFormState["memoryType"] })
              }
              options={[
                { label: "全部", value: "all" },
                { label: "情景记忆", value: "event" },
                { label: "Wiki", value: "wiki" },
              ]}
              value={form.memoryType}
            />
          </Form.Item>
          <Form.Item label="主体">
            <Input
              allowClear
              onChange={(event) => setForm({ ...form, subject: event.target.value })}
              placeholder="精确主体名"
              value={form.subject}
            />
          </Form.Item>
          <Form.Item label="事件类型">
            <Input
              allowClear
              onChange={(event) => setForm({ ...form, eventTypes: event.target.value })}
              placeholder="逗号分隔，如 decision,fact"
              value={form.eventTypes}
            />
          </Form.Item>
          <Form.Item label="Wiki 分类">
            <Input
              allowClear
              onChange={(event) => setForm({ ...form, wikiCategories: event.target.value })}
              placeholder="逗号分隔"
              value={form.wikiCategories}
            />
          </Form.Item>
          <Form.Item label="时间范围">
            <DatePicker.RangePicker
              onChange={(value) =>
                setForm({
                  ...form,
                  startTime: value?.[0] ?? null,
                  endTime: value?.[1] ?? null,
                })
              }
              showTime
              value={[form.startTime ?? null, form.endTime ?? null]}
            />
          </Form.Item>
          <Form.Item label="返回条数">
            <InputNumber
              max={50}
              min={1}
              onChange={(value) => setForm({ ...form, topN: Number(value ?? 10) })}
              style={{ width: "100%" }}
              value={form.topN}
            />
          </Form.Item>
          <Form.Item label="熟悉度参与排序">
            <Switch
              checked={form.enableFamiliarity}
              onChange={(checked) => setForm({ ...form, enableFamiliarity: checked })}
            />
          </Form.Item>
          <Form.Item label=" ">
            <Button icon={<ClearOutlined />} onClick={onClear}>
              清除筛选
            </Button>
          </Form.Item>
        </div>
      </Form>
    </Card>
  );
}

export function SearchView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { message } = App.useApp();
  const paramsKey = searchParams.toString();

  const [feedbackMap, setFeedbackMap] = useState<Record<string, FeedbackState>>({});

  const applied = useMemo(() => {
    const params = new URLSearchParams(paramsKey);
    const start = params.get("start_time");
    const end = params.get("end_time");
    const rawType = params.get("type");
    const memoryType: SearchFormState["memoryType"] =
      rawType === "event" || rawType === "wiki" ? rawType : "all";
    return {
      query: params.get("q")?.trim() ?? "",
      memoryType,
      subject: params.get("subject") ?? "",
      eventTypes: params.get("event_types") ?? "",
      wikiCategories: params.get("wiki_categories") ?? "",
      topN: Number(params.get("top_n") ?? 10),
      enableFamiliarity: params.get("familiarity") !== "0",
      startTime: start ? new Date(start) : null,
      endTime: end ? new Date(end) : null,
    };
  }, [paramsKey]);

  const searchRequest = useMemo(() => {
    if (!applied.query) {
      return null;
    }
    return {
      query: applied.query,
      memory_type: applied.memoryType,
      filters: {
        subject: applied.subject || undefined,
        event_types: splitList(applied.eventTypes),
        wiki_categories: splitList(applied.wikiCategories),
        start_time: applied.startTime ? applied.startTime.toISOString() : undefined,
        end_time: applied.endTime ? applied.endTime.toISOString() : undefined,
      },
      top_n: Math.min(Math.max(applied.topN || 10, 1), 50),
      enable_familiarity: applied.enableFamiliarity,
      include_score_breakdown: true,
    };
  }, [applied]);

  const query = useQuery({
    queryKey: ["memory", "search", paramsKey],
    queryFn: () => {
      if (!searchRequest) {
        throw new Error("缺少检索条件");
      }
      return searchMemories(searchRequest);
    },
    enabled: searchRequest !== null,
    placeholderData: keepPreviousData,
  });

  const feedbackMutation = useMutation({
    mutationFn: ({
      result,
      type,
      queryId,
    }: {
      result: SearchResultBase & { memory_type: "event" | "wiki" };
      type: FeedbackType;
      queryId: string;
    }) =>
      submitFeedback({
        query_id: queryId,
        memory_type: result.memory_type,
        memory_id: result.memory_id,
        feedback_type: type,
      }),
    onSuccess: (response, variables) => {
      if (variables.type === "clicked") {
        return;
      }
      setFeedbackMap((current) => ({
        ...current,
        [variables.result.memory_id]: {
          type: variables.type,
          counted: response.data.counted,
          reason: response.data.not_counted_reason,
        },
      }));
    },
    onError: (error: Error, variables) => {
      if (variables.type === "clicked") {
        return;
      }
      message.error(error.message);
    },
  });

  function handleFeedback(result: SearchResult, type: FeedbackType) {
    const queryId = query.data?.data.query_id;
    if (!queryId) {
      return;
    }
    feedbackMutation.mutate({ result, type, queryId });
  }

  function submit(form: SearchFormState) {
    const params = new URLSearchParams();
    const text = form.query.trim();
    if (text) {
      params.set("q", text);
    }
    if (form.memoryType !== "all") {
      params.set("type", form.memoryType);
    }
    if (form.subject.trim()) {
      params.set("subject", form.subject.trim());
    }
    if (form.eventTypes.trim()) {
      params.set("event_types", form.eventTypes.trim());
    }
    if (form.wikiCategories.trim()) {
      params.set("wiki_categories", form.wikiCategories.trim());
    }
    if (form.topN !== 10) {
      params.set("top_n", String(form.topN));
    }
    if (!form.enableFamiliarity) {
      params.set("familiarity", "0");
    }
    const start = toIso(form.startTime?.toISOString());
    const end = toIso(form.endTime?.toISOString());
    if (start) {
      params.set("start_time", start);
    }
    if (end) {
      params.set("end_time", end);
    }
    router.replace(`/search${params.toString() ? `?${params.toString()}` : ""}`);
  }

  function clearFilters() {
    router.replace(
      applied.query ? `/search?q=${encodeURIComponent(applied.query)}` : "/search",
    );
  }

  const results = query.data?.data.results ?? [];
  const responseIntent = query.data?.data.intent;
  const hasQuery = Boolean(applied.query);
  const filterSummary = [
    applied.memoryType !== "all" ? `类型：${applied.memoryType}` : null,
    applied.subject ? `主体：${applied.subject}` : null,
    applied.eventTypes ? `事件类型：${applied.eventTypes}` : null,
    applied.wikiCategories ? `Wiki 分类：${applied.wikiCategories}` : null,
    applied.startTime ? `起始：${applied.startTime.toLocaleString("zh-CN")}` : null,
    applied.endTime ? `截止：${applied.endTime.toLocaleString("zh-CN")}` : null,
  ]
    .filter(Boolean)
    .join("；");

  return (
    <div className="page-container">
      <PageHeader
        description="混合检索情景记忆与 Wiki 知识；筛选条件同步到 URL，便于刷新和分享内部链接。"
        title="统一搜索"
      />

      <SearchControls
        initial={applied}
        key={paramsKey}
        onClear={clearFilters}
        onSubmit={submit}
      />

      {!hasQuery ? (
        <Card className="panel-card">
          <Empty
            description="输入问题后开始检索。结果只来自后端统一检索接口。"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        </Card>
      ) : (
        <QueryState
          emptyAction={<Button onClick={clearFilters}>清除筛选</Button>}
          emptyDescription={
            filterSummary
              ? `当前筛选条件：${filterSummary}。系统不会自动放宽权限或时间条件。`
              : "当前没有符合条件的结果；请调整查询或筛选条件。"
          }
          emptyTitle="没有匹配结果"
          error={query.error}
          isEmpty={!query.isLoading && results.length === 0}
          isLoading={query.isLoading}
          loadingLabel="正在检索记忆…"
          onRetry={() => void query.refetch()}
        >
          {query.isPlaceholderData ? (
            <Alert
              title="正在获取新结果，下方仍为上一次搜索的结果"
              showIcon
              type="warning"
            />
          ) : null}

          {responseIntent ? (
            <div className="search-summary">
              <Space size={8} wrap>
                <Tag color="processing">意图：{intentLabelOf(responseIntent)}</Tag>
                <span className="muted">共 {results.length} 条结果</span>
                {filterSummary ? <span className="muted">{filterSummary}</span> : null}
              </Space>
            </div>
          ) : null}

          <div className="result-list">
            {results.map((result) => (
              <ResultCard
                feedback={feedbackMap[result.memory_id]}
                key={`${result.memory_type}-${result.memory_id}-${result.memory_type === "wiki" ? result.chunk_index : ""}`}
                onFeedback={handleFeedback}
                queryId={query.data?.data.query_id}
                result={result}
              />
            ))}
          </div>
        </QueryState>
      )}
    </div>
  );
}
