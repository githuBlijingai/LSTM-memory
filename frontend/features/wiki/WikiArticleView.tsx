"use client";

import { ArrowLeftOutlined, CopyOutlined, LinkOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, App, Button, Card, Descriptions, Space, Tag } from "antd";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo } from "react";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { useHasRole } from "@/components/providers/SessionProvider";
import { SafeMarkdown } from "@/components/wiki/SafeMarkdown";
import { getWikiArticle } from "@/lib/api/endpoints";
import type { WikiChunk } from "@/lib/api/contracts";
import { formatDate } from "@/lib/format";
import { wikiArticleStatusMeta, wikiChunkStatusMeta } from "@/lib/labels";

function chunkLabel(chunk: WikiChunk) {
  return chunk.heading_path.length > 0
    ? chunk.heading_path.join(" / ")
    : `知识块 #${chunk.chunk_index}`;
}

function scrollToChunk(index: number) {
  const element = document.getElementById(`chunk-${index}`);
  if (!element) {
    return;
  }
  element.scrollIntoView({ behavior: "smooth", block: "start" });
  element.classList.remove("chunk-highlight");
  // 强制重排以便重复触发高亮动画。
  void element.offsetWidth;
  element.classList.add("chunk-highlight");
  window.setTimeout(() => element.classList.remove("chunk-highlight"), 2500);
}

export function WikiArticleView() {
  const params = useParams<{ articleId: string }>();
  const articleId = params.articleId;
  const searchParams = useSearchParams();
  const router = useRouter();
  const { message } = App.useApp();
  const canViewInactive = useHasRole("operator");

  const query = useQuery({
    queryKey: ["wiki", "article", articleId, canViewInactive],
    queryFn: () => getWikiArticle(articleId, canViewInactive),
    enabled: Boolean(articleId),
  });

  const article = query.data?.data;
  const chunkParam = searchParams.get("chunk");
  const targetIndex = chunkParam !== null && chunkParam !== "" ? Number(chunkParam) : null;

  const sortedChunks = useMemo(
    () =>
      [...(article?.chunks ?? [])].sort((left, right) => left.chunk_index - right.chunk_index),
    [article],
  );
  const activeChunks = useMemo(
    () => sortedChunks.filter((chunk) => chunk.status === "active"),
    [sortedChunks],
  );
  const otherChunks = useMemo(
    () => sortedChunks.filter((chunk) => chunk.status !== "active"),
    [sortedChunks],
  );

  useEffect(() => {
    if (!article || targetIndex === null || Number.isNaN(targetIndex)) {
      return;
    }
    if (!document.getElementById(`chunk-${targetIndex}`)) {
      return;
    }
    scrollToChunk(targetIndex);
  }, [article, targetIndex]);

  const currentChunk = useMemo(() => {
    if (targetIndex !== null) {
      const found = sortedChunks.find((chunk) => chunk.chunk_index === targetIndex);
      if (found) {
        return found;
      }
    }
    return activeChunks[0] ?? sortedChunks[0] ?? null;
  }, [activeChunks, sortedChunks, targetIndex]);

  const activePosition = currentChunk
    ? activeChunks.findIndex((chunk) => chunk.chunk_index === currentChunk.chunk_index)
    : -1;
  const previousChunk = activePosition > 0 ? activeChunks[activePosition - 1] : null;
  const nextChunk =
    activePosition >= 0 && activePosition < activeChunks.length - 1
      ? activeChunks[activePosition + 1]
      : null;

  function goToChunk(index: number) {
    router.replace(`/wiki/${articleId}?chunk=${index}`, { scroll: false });
    scrollToChunk(index);
  }

  async function copyCitation() {
    if (!article) {
      return;
    }
    const citation = [
      `《${article.title}》`,
      `来源：${article.source_url}`,
      `修订：${article.revision_id}`,
      `知识块：#${currentChunk?.chunk_index ?? 0}`,
      `访问日期：${formatDate(new Date().toISOString())}`,
    ].join("\n");
    try {
      await navigator.clipboard.writeText(citation);
      message.success("引用信息已复制");
    } catch {
      message.error("复制失败，请手动选择文本。");
    }
  }

  const statusMeta = article ? wikiArticleStatusMeta(article.status) : null;

  return (
    <div className="page-container">
      <PageHeader
        description="只读正文、知识块锚点与来源修订信息；正文经过 Markdown 白名单清洗。"
        extra={
          <Space wrap>
            <Link href="/wiki">
              <Button icon={<ArrowLeftOutlined />}>返回词条列表</Button>
            </Link>
            {article ? (
              <Button icon={<CopyOutlined />} onClick={() => void copyCitation()}>
                复制引用
              </Button>
            ) : null}
          </Space>
        }
        title={article?.title ?? "Wiki 词条"}
      />

      <QueryState
        error={query.error}
        isLoading={query.isLoading}
        loadingLabel="正在读取词条正文…"
        notFoundAsUnavailable
        onRetry={() => void query.refetch()}
        unavailableDescription="Wiki 接口（AG-04）尚未实现或已关闭，或该词条不存在。页面不会展示模拟正文。"
        unavailableTitle="Wiki 词条暂不可用"
      >
        {article ? (
          <>
            {article.status !== "active" ? (
              <Alert
                title={`该词条当前状态为「${statusMeta?.label ?? article.status}」，内容仅供管理员核对。`}
                showIcon
                type="warning"
              />
            ) : null}

            <div className="wiki-layout">
              <Card className="panel-card wiki-toc" title="目录">
                {activeChunks.length === 0 ? (
                  <p className="muted">该词条没有生效知识块。</p>
                ) : (
                  <ol className="toc-list">
                    {activeChunks.map((chunk) => (
                      <li key={chunk.chunk_index}>
                        <button
                          className={
                            currentChunk?.chunk_index === chunk.chunk_index
                              ? "toc-link toc-link-active"
                              : "toc-link"
                          }
                          onClick={() => goToChunk(chunk.chunk_index)}
                          type="button"
                        >
                          {chunkLabel(chunk)}
                        </button>
                      </li>
                    ))}
                  </ol>
                )}
              </Card>

              <div className="wiki-main">
                <Card className="panel-card">
                  <Space align="center" size={8} wrap>
                    {statusMeta ? (
                      <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
                    ) : null}
                    <Tag>{article.language}</Tag>
                    {article.domain ? <Tag color="purple">{article.domain}</Tag> : null}
                    {article.categories.map((item) => (
                      <Tag key={item}>{item}</Tag>
                    ))}
                  </Space>
                  <p className="wiki-summary">{article.summary}</p>
                </Card>

                <Card className="panel-card wiki-content">
                  {activeChunks.length === 0 ? (
                    <SafeMarkdown>{article.content}</SafeMarkdown>
                  ) : (
                    activeChunks.map((chunk) => (
                      <section
                        className="wiki-chunk"
                        id={`chunk-${chunk.chunk_index}`}
                        key={`${chunk.chunk_index}-${chunk.chunk_id ?? ""}`}
                      >
                        <div className="chunk-heading">
                          <span className="chunk-index">#{chunk.chunk_index}</span>
                          <span className="chunk-path">{chunkLabel(chunk)}</span>
                          <span className="muted">{chunk.token_count} tokens</span>
                        </div>
                        <SafeMarkdown>{chunk.raw_text}</SafeMarkdown>
                      </section>
                    ))
                  )}

                  <div className="wiki-chunk-nav">
                    <Button
                      disabled={!previousChunk}
                      onClick={() => previousChunk && goToChunk(previousChunk.chunk_index)}
                    >
                      上一块
                    </Button>
                    <Button
                      disabled={!nextChunk}
                      onClick={() => nextChunk && goToChunk(nextChunk.chunk_index)}
                    >
                      下一块
                    </Button>
                  </div>
                </Card>

                {canViewInactive && otherChunks.length > 0 ? (
                  <Card className="panel-card" title="非生效知识块（仅 operator 及以上可见）">
                    {otherChunks.map((chunk) => {
                      const meta = wikiChunkStatusMeta(chunk.status);
                      return (
                        <details className="other-chunk" key={`${chunk.chunk_index}-${chunk.chunk_id ?? ""}`}>
                          <summary>
                            <Space size={6}>
                              <Tag color={meta.color}>{meta.label}</Tag>
                              <span>{chunkLabel(chunk)}</span>
                            </Space>
                          </summary>
                          <SafeMarkdown>{chunk.raw_text}</SafeMarkdown>
                        </details>
                      );
                    })}
                  </Card>
                ) : null}

                <Card className="panel-card" title="来源与修订">
                  <Descriptions
                    bordered
                    column={{ lg: 2, sm: 1, xs: 1 }}
                    items={[
                      { key: "source", label: "来源", children: article.source },
                      {
                        key: "url",
                        label: "原始 URL",
                        children: (
                          <a href={article.source_url} rel="noopener noreferrer" target="_blank">
                            <LinkOutlined /> {article.source_url}
                          </a>
                        ),
                      },
                      { key: "page", label: "Page ID", children: article.source_page_id },
                      { key: "revision", label: "Revision ID", children: article.revision_id },
                      {
                        key: "source-updated",
                        label: "来源更新时间",
                        children: <DateTimeText value={article.source_updated_at} />,
                      },
                      {
                        key: "sync",
                        label: "本次同步时间",
                        children: <DateTimeText value={article.sync_time} />,
                      },
                    ]}
                  />
                  <p className="muted wiki-footer">
                    内容来自 {article.source}，请遵循来源许可与署名要求；本系统不提供 Wiki 编辑能力。
                  </p>
                </Card>
              </div>
            </div>
          </>
        ) : null}
      </QueryState>
    </div>
  );
}
