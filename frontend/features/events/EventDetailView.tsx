"use client";

import { ArrowLeftOutlined, EditOutlined, SafetyOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Space,
  Tag,
} from "antd";
import type { Dayjs } from "dayjs";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { CopyButton } from "@/components/common/CopyButton";
import { DateTimeText } from "@/components/common/DateTimeText";
import { PageHeader } from "@/components/common/PageHeader";
import { QueryState } from "@/components/common/QueryState";
import { useHasRole } from "@/components/providers/SessionProvider";
import {
  archiveEvent,
  correctEvent,
  getEvent,
  restoreEvent,
  updateFamiliarity,
} from "@/lib/api/endpoints";
import { formatScore } from "@/lib/format";
import { eventStatusMeta, visibilityMeta } from "@/lib/labels";

type DialogMode = "correct" | "archive" | "restore" | "familiarity" | null;

export function EventDetailView() {
  const params = useParams<{ id: string }>();
  const eventId = params.id;
  const router = useRouter();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const isAdmin = useHasRole("admin");

  const [dialog, setDialog] = useState<DialogMode>(null);
  const [correctionTitle, setCorrectionTitle] = useState("");
  const [correctionContent, setCorrectionContent] = useState("");
  const [correctionTime, setCorrectionTime] = useState<Dayjs | null>(null);
  const [correctionReason, setCorrectionReason] = useState("");
  const [deleteMode, setDeleteMode] = useState<"archive" | "purge">("archive");
  const [deleteReason, setDeleteReason] = useState("");
  const [restoreReason, setRestoreReason] = useState("");
  const [familiarityScore, setFamiliarityScore] = useState(0);
  const [familiarityReason, setFamiliarityReason] = useState("");

  const query = useQuery({
    queryKey: ["memory", "event", eventId],
    queryFn: () => getEvent(eventId),
    enabled: Boolean(eventId),
  });

  const event = query.data?.data;

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["memory", "event", eventId] });
    await queryClient.invalidateQueries({ queryKey: ["memory", "events"] });
  }

  const correctionMutation = useMutation({
    mutationFn: () =>
      correctEvent(eventId, {
        title: correctionTitle.trim() || null,
        content: correctionContent.trim(),
        event_time: correctionTime ? correctionTime.toISOString() : null,
        reason: correctionReason.trim(),
      }),
    onSuccess: async (result) => {
      message.success("纠偏请求已受理，已创建新版本事件。");
      setDialog(null);
      await refresh();
      router.push(`/memories/events/${result.data.new_event_id}`);
    },
    onError: (error: Error) => message.error(error.message),
  });

  const deleteMutation = useMutation({
    mutationFn: () => archiveEvent(eventId, { mode: deleteMode, reason: deleteReason.trim() }),
    onSuccess: async () => {
      message.success(deleteMode === "purge" ? "合规清除流程已受理。" : "归档请求已受理。");
      setDialog(null);
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const restoreMutation = useMutation({
    mutationFn: () => restoreEvent(eventId, { reason: restoreReason.trim() }),
    onSuccess: async () => {
      message.success("恢复请求已受理。");
      setDialog(null);
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const familiarityMutation = useMutation({
    mutationFn: () =>
      updateFamiliarity("event", eventId, {
        score: familiarityScore,
        reason: familiarityReason.trim(),
      }),
    onSuccess: async () => {
      message.success("熟悉度已更新并写入审计日志。");
      setDialog(null);
      await refresh();
    },
    onError: (error: Error) => message.error(error.message),
  });

  function openFamiliarity() {
    setFamiliarityScore(event?.familiarity_score ?? 0);
    setFamiliarityReason("");
    setDialog("familiarity");
  }

  return (
    <div className="page-container">
      <PageHeader
        description="查看事件原文、模型与分数信息；纠偏、归档、恢复和熟悉度手调仅管理员可执行。"
        extra={
          <Space wrap>
            <Link href="/memories/events">
              <Button icon={<ArrowLeftOutlined />}>返回列表</Button>
            </Link>
            {isAdmin && event ? (
              <>
                <Button icon={<EditOutlined />} onClick={() => setDialog("correct")}>
                  纠偏
                </Button>
                <Button icon={<SafetyOutlined />} onClick={openFamiliarity}>
                  手调熟悉度
                </Button>
                {event.status === "archived" ? (
                  <Button onClick={() => setDialog("restore")}>恢复</Button>
                ) : (
                  <Button danger onClick={() => setDialog("archive")}>
                    归档
                  </Button>
                )}
              </>
            ) : null}
          </Space>
        }
        title="事件详情"
      />

      <QueryState
        error={query.error}
        isLoading={query.isLoading}
        loadingLabel="正在读取事件详情…"
        onRetry={() => void query.refetch()}
      >
        {event ? (
          <>
            <Card className="panel-card">
              <div className="detail-title-row">
                <div>
                  <Space align="center" size={8} wrap>
                    <h2>{event.title}</h2>
                    <Tag color={eventStatusMeta(event.status).color}>
                      {eventStatusMeta(event.status).label}
                    </Tag>
                    <Tag color={visibilityMeta(event.visibility).color}>
                      {visibilityMeta(event.visibility).label}
                    </Tag>
                  </Space>
                  <p className="muted detail-subtitle">
                    事件 ID：<code>{event.event_id}</code>
                    <CopyButton text={event.event_id} label="复制事件 ID" />
                  </p>
                </div>
              </div>

              <Descriptions
                bordered
                column={{ lg: 3, md: 2, sm: 1, xs: 1 }}
                items={[
                  { key: "subject", label: "主体", children: event.subject },
                  { key: "type", label: "事件类型", children: event.event_type },
                  { key: "time", label: "事件时间", children: <DateTimeText value={event.event_time} /> },
                  { key: "importance", label: "重要性", children: formatScore(event.importance, 2) },
                  { key: "decay", label: "衰减分", children: formatScore(event.decay_score) },
                  {
                    key: "familiarity",
                    label: "熟悉度",
                    children: formatScore(event.familiarity_score),
                  },
                  {
                    key: "entity",
                    label: "所属实体",
                    children: (
                      <Link className="table-link" href={`/entities/${event.entity_id}`}>
                        查看实体状态
                      </Link>
                    ),
                  },
                  { key: "sequence", label: "状态序列号", children: event.sequence_no ?? "—" },
                  {
                    key: "model",
                    label: "事件模型",
                    children: event.event_model_version ?? "—",
                  },
                  { key: "source", label: "来源", children: event.source },
                  { key: "created", label: "创建时间", children: <DateTimeText value={event.created_at} /> },
                  { key: "updated", label: "更新时间", children: <DateTimeText value={event.updated_at} /> },
                ]}
              />
            </Card>

            <Card className="panel-card" title="原文">
              <pre className="content-block">{event.content ?? "（无正文）"}</pre>
            </Card>

            <Card className="panel-card" title="纠偏链">
              {event.supersedes_event_id ? (
                <Alert
                  action={
                    <Link href={`/memories/events/${event.supersedes_event_id}`}>
                      查看被纠正事件
                    </Link>
                  }
                  message="本事件是由旧事件纠偏生成的新版本"
                  showIcon
                  type="info"
                />
              ) : (
                <p className="muted">该事件没有前置纠偏版本；若被后续版本纠正，可在新版本详情中追溯。</p>
              )}
              <p className="muted">
                纠偏会创建新事件并触发实体状态重建，原事件保留用于审计。
              </p>
            </Card>
          </>
        ) : null}
      </QueryState>

      <Modal
        confirmLoading={correctionMutation.isPending}
        okText="提交纠偏"
        onCancel={() => setDialog(null)}
        onOk={() => {
          if (!correctionContent.trim() || !correctionReason.trim()) {
            message.warning("请填写纠正后的正文和纠偏原因。");
            return;
          }
          correctionMutation.mutate();
        }}
        open={dialog === "correct"}
        title="创建纠偏版本"
      >
        <Form layout="vertical">
          <Form.Item label="新标题（可选）">
            <Input
              maxLength={256}
              onChange={(e) => setCorrectionTitle(e.target.value)}
              placeholder={event?.title}
              value={correctionTitle}
            />
          </Form.Item>
          <Form.Item label="纠正后的正文（必填）">
            <Input.TextArea
              maxLength={20000}
              onChange={(e) => setCorrectionContent(e.target.value)}
              placeholder={event?.content ?? ""}
              rows={5}
              value={correctionContent}
            />
          </Form.Item>
          <Form.Item label="新事件时间（可选）">
            <DatePicker
              onChange={(value) => setCorrectionTime(value)}
              showTime
              style={{ width: "100%" }}
              value={correctionTime}
            />
          </Form.Item>
          <Form.Item label="纠偏原因（必填）">
            <Input
              maxLength={500}
              onChange={(e) => setCorrectionReason(e.target.value)}
              placeholder="说明为什么需要纠正"
              value={correctionReason}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        confirmLoading={deleteMutation.isPending}
        okButtonProps={{ danger: true }}
        okText={deleteMode === "purge" ? "发起清除" : "提交归档"}
        onCancel={() => setDialog(null)}
        onOk={() => {
          if (!deleteReason.trim()) {
            message.warning("请填写原因。");
            return;
          }
          deleteMutation.mutate();
        }}
        open={dialog === "archive"}
        title={`处理事件：${event?.title ?? ""}`}
      >
        <Radio.Group
          onChange={(e) => setDeleteMode(e.target.value)}
          style={{ marginBottom: 16 }}
          value={deleteMode}
        >
          <Radio.Button value="archive">归档</Radio.Button>
          <Radio.Button value="purge">合规清除</Radio.Button>
        </Radio.Group>
        <p className="muted">
          归档保留审计记录并退出检索；合规清除仅管理员可执行，进入不可逆删除流程。
        </p>
        <Input.TextArea
          maxLength={500}
          onChange={(e) => setDeleteReason(e.target.value)}
          placeholder="原因（必填，最多 500 字）"
          rows={3}
          value={deleteReason}
        />
      </Modal>

      <Modal
        confirmLoading={restoreMutation.isPending}
        okText="恢复事件"
        onCancel={() => setDialog(null)}
        onOk={() => {
          if (!restoreReason.trim()) {
            message.warning("请填写恢复原因。");
            return;
          }
          restoreMutation.mutate();
        }}
        open={dialog === "restore"}
        title="恢复归档事件"
      >
        <Input.TextArea
          maxLength={500}
          onChange={(e) => setRestoreReason(e.target.value)}
          placeholder="恢复原因（必填）"
          rows={3}
          value={restoreReason}
        />
      </Modal>

      <Modal
        confirmLoading={familiarityMutation.isPending}
        okText="保存"
        onCancel={() => setDialog(null)}
        onOk={() => {
          if (!familiarityReason.trim()) {
            message.warning("手调熟悉度必须填写原因，以便写入审计日志。");
            return;
          }
          familiarityMutation.mutate();
        }}
        open={dialog === "familiarity"}
        title="手调熟悉度"
      >
        <Alert
          title="取值范围 0～2；手调不会增加访问次数，也不会绕过每日增长记录。"
          showIcon
          style={{ marginBottom: 16 }}
          type="info"
        />
        <Form layout="vertical">
          <Form.Item label="熟悉度">
            <InputNumber
              max={2}
              min={0}
              onChange={(value) => setFamiliarityScore(Number(value ?? 0))}
              step={0.05}
              style={{ width: "100%" }}
              value={familiarityScore}
            />
          </Form.Item>
          <Form.Item label="原因（必填）">
            <Input.TextArea
              maxLength={500}
              onChange={(e) => setFamiliarityReason(e.target.value)}
              rows={3}
              value={familiarityReason}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
