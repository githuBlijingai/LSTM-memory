"""把评估结果渲染成可读的中文 Markdown 报告。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.runner import REFERENCE_TARGETS

REPORT_PATH = Path(__file__).resolve().parent / "results" / "REPORT.md"
PERCENT_METRICS = ("recall", "precision", "hit", "mrr", "map", "ndcg")


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _metric_row(name: str, summary: dict[str, Any]) -> str:
    ci = summary.get("ndcg_ci95")
    ci_text = f"{ci[0] * 100:.1f}% ~ {ci[1] * 100:.1f}%" if ci else "—"
    return (
        f"| {name} | {_pct(summary.get('recall'))} | {_pct(summary.get('hit'))} | "
        f"{_pct(summary.get('mrr'))} | {_pct(summary.get('ndcg'))} | "
        f"{_pct(summary.get('map'))} | {ci_text} |"
    )


def _table_header() -> str:
    return (
        "| 配置 | Recall@10 | Hit@10 | MRR@10 | nDCG@10 | MAP@10 | nDCG 95% CI |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"
    )


def _slice_table(slices: dict[str, Any]) -> str:
    lines = [
        "| 切片 | 查询数 | Recall@10 | MRR@10 | nDCG@10 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in slices.items():
        lines.append(
            f"| {name} | {summary.get('cases', 0)} | {_pct(summary.get('recall'))} | "
            f"{_pct(summary.get('mrr'))} | {_pct(summary.get('ndcg'))} |"
        )
    return "\n".join(lines)


def _composition(payload: dict[str, Any]) -> str:
    query_count = len(payload["queries"])
    event_labels = sum(len(query["event_labels"]) for query in payload["queries"])
    wiki_labels = sum(len(query["wiki_labels"]) for query in payload["queries"])
    slice_counts = Counter(
        name for query in payload["queries"] for name in query.get("slices", [])
    )
    intent_counts = Counter(query["expected_intent"] for query in payload["queries"])
    auto_count = sum(1 for query in payload["queries"] if query["memory_type"] == "all")
    return "\n".join(
        [
            "| 项目 | 数量 | 说明 |",
            "| --- | ---: | --- |",
            f"| 事件 | {len(payload['events'])} | 覆盖项目/人物/团队/客户/系统/冷启动偏好/长序列 |",
            f"| Wiki 词条 / 知识块 | {len(payload['wiki_articles'])} / "
            f"{sum(len(article['chunks']) for article in payload['wiki_articles'])} | "
            "主题取自系统实现概念 |",
            f"| 查询 | {query_count} | 每条带多级相关性标注 |",
            f"| 事件标注 | {event_labels} | 2=直接答案，1=相关背景 |",
            f"| Wiki 标注 | {wiki_labels} | 同上 |",
            f"| 时间敏感切片 | {slice_counts.get('time_sensitive', 0)} | 最近一次/第 N 次 |",
            f"| 冷启动切片 | {slice_counts.get('cold_start', 0)} | 实体仅有 1 条事件 |",
            f"| 长序列切片 | {slice_counts.get('long_sequence', 0)} | 发布流水线 12 条事件 |",
            f"| 期望意图分布 | event {intent_counts.get('event', 0)} / "
            f"wiki {intent_counts.get('wiki', 0)} / mixed {intent_counts.get('mixed', 0)} | "
            f"其中 {auto_count} 条走 memory_type=all 的自动意图路径 |",
        ]
    )


def _confusion(intent: dict[str, Any]) -> str:
    predicted = sorted(
        {name for row in intent["confusion"].values() for name in row}
    )
    header = "| 期望 \\ 预测 | " + " | ".join(predicted) + " |"
    separator = "| --- | " + " | ".join("---:" for _ in predicted) + " |"
    lines = [header, separator]
    for expected in ("event", "wiki", "mixed"):
        row = intent["confusion"].get(expected, {})
        cells = " | ".join(str(row.get(name, 0)) for name in predicted)
        lines.append(f"| {expected} | {cells} |")
    return "\n".join(lines)


def _failures(intent: dict[str, Any]) -> str:
    if not intent["failures"]:
        return "无。"
    lines = [
        "| 查询 | 文本 | 期望 | 预测 | 规则原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for failure in intent["failures"]:
        lines.append(
            f"| {failure['query']} | {failure['text']} | {failure['expected']} | "
            f"{failure['predicted']} | {failure['reason']} |"
        )
    return "\n".join(lines)


def _feedback_table(feedback: dict[str, Any], labels: dict[str, str]) -> str:
    lines = [
        "| 查询 | 目标记忆 | 增益前排名 | 增益后排名 |",
        "| --- | --- | ---: | ---: |",
    ]
    for sample in feedback["rank_samples"]:
        lines.append(
            f"| {sample['query']} | {labels.get(sample['target'], sample['target'])} | "
            f"{sample['rank_before'] or '未命中'} | {sample['rank_after'] or '未命中'} |"
        )
    return "\n".join(lines)


def _memory_labels(payload: dict[str, Any]) -> dict[str, str]:
    labels = {
        event["event_id"]: f"event:{event['key']} {event['title']}"
        for event in payload["events"]
    }
    for article in payload["wiki_articles"]:
        for chunk in article["chunks"]:
            labels[chunk["chunk_id"]] = (
                f"wiki:{chunk['key']} {chunk['title']} / {' > '.join(chunk['heading_path'])}"
            )
    return labels


def _gate_table(gate: dict[str, Any]) -> str:
    lines = [
        "| 检查项 | 当前值 | 参考线 | 结论 |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, row in gate["checks"].items():
        lines.append(
            f"| {name} | {_pct(row['value'])} | {_pct(row['target'])} | "
            f"{'通过' if row['status'] == 'pass' else '未达'} |"
        )
    return "\n".join(lines)


def render_report(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    online_result: dict[str, Any] | None = None,
) -> str:
    metadata = result["metadata"]
    event = result["event"]
    wiki = result["wiki"]
    unified = result["unified"]
    feedback = result["feedback"]
    intent = result["extraction"]["intent"]
    entity = result["extraction"]["entity_resolution"]
    fields = result["extraction"]["field_completeness"]
    gate = result["gate"]

    event_full = event["full"]["@10"]
    wiki_full = wiki["full"]["@10"]
    unified_live = unified["live"]["@10"]
    event_candidate = event["candidate"]
    wiki_candidate = wiki["candidate"]
    event_prod = event["candidate_at_production_threshold"]
    wiki_prod = wiki["candidate_at_production_threshold"]

    rerank_delta_event = event["with_rerank"]["@10"]["ndcg"] - event["dense"]["@10"]["ndcg"]
    rerank_delta_unified = unified_live["ndcg"] - unified["no_rerank"]["@10"]["ndcg"]
    intent_gap = unified_live["ndcg"] - unified["oracle_intent"]["@10"]["ndcg"]

    summary_points = [
        f"事件检索 nDCG@10 = {_pct(event_full['ndcg'])}、Recall@10 = {_pct(event_full['recall'])}；"
        f"Wiki 检索 nDCG@10 = {_pct(wiki_full['ndcg'])}、Recall@10 = {_pct(wiki_full['recall'])}。",
        f"统一搜索（自动意图 + 双路融合 + 去重）nDCG@10 = {_pct(unified_live['ndcg'])}、"
        f"Recall@10 = {_pct(unified_live['recall'])}、Hit@10 = {_pct(unified_live['hit'])}。",
        f"主要短板：自动意图准确率仅 {_pct(intent['auto_accuracy'])}；"
        f"长序列切片 nDCG@10 = {_pct(event['full_slices'].get('long_sequence', {}).get('ndcg'))}；"
        f"Wiki 候选召回 {_pct(wiki_candidate['candidate_recall'])}；"
        "反馈信号对排序的增益接近噪声。",
        f"门禁状态：**{gate['status']}**。当前数字来自词法代理，"
        "不能替代真实 BGE / 事件模型的发布结论。",
    ]

    sections: list[str] = [
        "# AG-07 检索质量评估报告（基线 v1）",
        "",
        f"- 数据集：`{metadata['dataset_version']}`，生成于 {metadata['dataset_generated_at']}",
        f"- 评估时钟：{metadata['eval_clock']}（固定，保证时间衰减可复现）",
        f"- 向量：`{metadata['embedding']['model_version']}`，"
        f"维度 {metadata['embedding']['dimension']}",
        f"- 精排：`{metadata['reranker']}`",
        f"- 候选/截断：candidate_k={metadata['candidate_k']}，top_n={metadata['top_n']}，"
        f"代理阈值={metadata['min_similarity']}，生产阈值={metadata['production_min_similarity']}",
        "",
        "## 1. 结论摘要",
        "",
        *[f"- {point}" for point in summary_points],
        "",
        "> 说明：本报告是**可重复的下限基线**。数据与标签为手写合成，向量与精排为确定性词法代理；"
        "真实 BGE 与事件模型尚未配置，因此发布门槛仍处于 blocked。",
        "",
        "## 2. 评估数据集",
        "",
        "来源：全部文本为本评估手写，题材取自设计文档 3.3 / 4.4 / 12.5 与既有实现配置；"
        "不含真实用户数据、生产日志或第三方语料。构造器使用固定命名空间 + 稳定 key 生成 UUIDv5，"
        "重复生成逐字节一致。",
        "",
        _composition(payload),
        "",
        "## 3. 指标定义",
        "",
        "| 指标 | 定义 |",
        "| --- | --- |",
        "| Recall@K | 前 K 个结果覆盖的相关记忆数 / 全部相关记忆数（等级 > 0） |",
        "| Precision@K | 前 K 个结果中相关记忆数 / K |",
        "| Hit@K | 前 K 个结果是否至少命中一个相关记忆 |",
        "| MRR@K | 第一个相关结果排名倒数；未命中为 0 |",
        "| MAP@K | 各命中位置 precision 的均值，再对相关结果数归一化 |",
        "| nDCG@K | 增益 2^rel-1，折损 1/log2(rank+1)，用理想排序归一化 |",
        "| 候选召回 | 标注相关项中被候选集覆盖的比例（先看召回瓶颈，再看排序） |",
        "",
        "相关性等级：2 = 直接答案，1 = 相关背景，0/未标注 = 无关。"
        "Recall/Precision/MRR/MAP/Hit 将等级 1、2 都视为相关；nDCG 使用等级增益。",
        "",
        "## 4. 基线数字",
        "",
        "### 4.1 事件检索",
        "",
        _table_header(),
        _metric_row("纯向量（dense）", event["dense"]["@10"]),
        _metric_row("向量 + 精排（无熟悉度）", event["with_rerank"]["@10"]),
        _metric_row("完整排序（精排 + 衰减 + 熟悉度）", event_full),
        "",
        f"精排代理对事件排序的 nDCG 变化：**{rerank_delta_event:+.3f}**。"
        "该代理是词法重叠，不代表真实交叉编码器；若为负，说明当前代理特征不足以提升事件排序。",
        "",
        "### 4.2 Wiki 检索",
        "",
        _table_header(),
        _metric_row("纯向量（dense）", wiki["dense"]["@10"]),
        _metric_row("完整排序（精排 + 权重 + 熟悉度）", wiki_full),
        "",
        "### 4.3 统一搜索",
        "",
        _table_header(),
        _metric_row("线上路径（含显式 memory_type）", unified_live),
        _metric_row("Oracle 意图（排序上限）", unified["oracle_intent"]["@10"]),
        _metric_row("关闭精排", unified["no_rerank"]["@10"]),
        _metric_row("关闭熟悉度", unified["no_familiarity"]["@10"]),
        "",
        f"- 精排对统一搜索的 nDCG 增益：**{rerank_delta_unified:+.3f}**。",
        f"- 自动意图相对 Oracle 的 nDCG 损失：**{intent_gap:+.3f}**"
        "（负数表示当前意图分类造成的排序损失）。",
        f"- 同词条最多 {2} 个知识块的去重违规：**{unified['wiki_dedupe_violations']}** 次。",
        "",
        "### 4.4 切片",
        "",
        "事件检索：",
        "",
        _slice_table(event["full_slices"]),
        "",
        "统一搜索：",
        "",
        _slice_table(unified["live_slices"]),
        "",
        "### 4.5 候选召回（第一段瓶颈）",
        "",
        "| 通道 | 代理阈值下候选召回 | 生产阈值 0.15 下候选召回 | 相关项总数 | 零词法重叠相关项 |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| 事件 | {_pct(event_candidate['candidate_recall'])} | "
        f"{_pct(event_prod['candidate_recall'])} | {event_candidate['relevant_items']} | "
        f"{len(event_candidate['zero_overlap_relevant'])} |",
        f"| Wiki | {_pct(wiki_candidate['candidate_recall'])} | "
        f"{_pct(wiki_prod['candidate_recall'])} | {wiki_candidate['relevant_items']} | "
        f"{len(wiki_candidate['zero_overlap_relevant'])} |",
        "",
        "代理阈值按词法向量的余弦尺度校准；生产阈值一列用于观察直接套用 BGE 阈值时的损失。"
        "零词法重叠指查询与目标没有任何字符 n-gram 交集，词法代理必然漏召，"
        "这部分应由真实 BGE 覆盖，不能归因于排序逻辑。",
        "",
        "## 5. 事件与实体抽取",
        "",
        "系统不包含 LLM 抽取器：事件字段由写入方提供，检索前只做意图分类与实体精确匹配。"
        "因此这里评估“结构化输入的可用性”，而不是开放信息抽取质量。",
        "",
        "| 检查项 | 结果 |",
        "| --- | ---: |",
        f"| 事件字段完整率 | {_pct(fields['completeness'])}"
        f"（{fields['complete_events']}/{fields['events']}） |",
        f"| 实体精确匹配率 | {_pct(entity['exact_match_rate'])}"
        f"（{entity['exact_match_events']}/{entity['events']}） |",
        f"| 意图准确率（含显式 memory_type） | {_pct(intent['accuracy'])} |",
        f"| 意图准确率（memory_type=all 自动路径） | {_pct(intent['auto_accuracy'])}"
        f"（{intent['auto_correct']}/{intent['auto_cases']}） |",
        "",
        "意图混淆矩阵：",
        "",
        _confusion(intent),
        "",
        "自动路径的失败样例：",
        "",
        _failures(intent),
        "",
        "实体短板：运行期按 `(owner_id, subject, entity_type)` 精确匹配；"
        f"数据集含 {len(entity['alias_events'])} 条别名主体事件，会被拆成独立实体：",
        "",
        *[
            f"- `{item['event']}`：`{item['subject']}` ≠ `{item['canonical_subject']}`"
            for item in entity["alias_events"]
        ],
        "",
        "## 6. 反馈信号",
        "",
        f"- 带熟悉度标注的查询：{feedback['familiar_queries']} 条。",
        f"- 开启熟悉度后排名上升 / 下降：{feedback['improved']} / {feedback['degraded']} 条。",
        f"- nDCG@10 变化：**{feedback['delta_ndcg']:+.3f}**。",
        "",
        _feedback_table(feedback, _memory_labels(payload)),
        "",
        "单次有效反馈最多 +0.08 熟悉度，熟悉度只通过 `1 + 0.5F` 进入权重，"
        "因此对排序的推动有限；这与数据量小、相关项本身已靠前有关，"
        "但当前增益不足以支撑“反馈显著改善排序”的结论。",
        "",
        "## 7. 发布门禁",
        "",
        f"状态：**{gate['status']}**（blocked 表示缺少真实模型，不能出具发布结论）。",
        "",
        "阻断项：",
        "",
        *[f"- {item}" for item in gate["blockers"]],
        "",
        "本评估参考线（建议值，非设计硬指标）：",
        "",
        _gate_table(gate),
        "",
        "设计文档 4.4 的唯一硬门槛是“测试集指标不得低于纯 BGE 基线”。"
        "该门槛需要真实 BGE 与事件模型包，本次无法执行。",
        "",
        "## 8. 短板与建议",
        "",
        f"1. **自动意图分类（高）**：memory_type=all 时准确率 {_pct(intent['auto_accuracy'])}，"
        "规则在无关键词命中时一律退化为 mixed，导致 Wiki/事件单通道查询也走双路。"
        "建议扩充领域词表或改用轻量分类器，并保留 mixed 作为兜底。",
        f"2. **Wiki 候选召回（高）**：代理阈值下 {_pct(wiki_candidate['candidate_recall'])}，"
        f"生产阈值 0.15 下仅 {_pct(wiki_prod['candidate_recall'])}。"
        "知识块文本短、口语查询与词条表述差异大，建议为 Wiki 增加标题/别名/关键词索引，"
        "或对 Wiki 使用更低的候选阈值并依赖精排兜底。",
        f"3. **长序列排序（中）**：长序列切片 nDCG@10 = "
        f"{_pct(event['full_slices'].get('long_sequence', {}).get('ndcg'))}，"
        "明显低于整体。时间衰减与熟悉度可能把早期关键决策压下去，"
        "建议对“第 N 次/最早一次”类查询引入显式序列位置特征。",
        f"4. **反馈信号（中）**：nDCG 仅 {feedback['delta_ndcg']:+.3f}，"
        f"{feedback['improved']} 升 {feedback['degraded']} 降。"
        "建议评估提高 familiarity_beta 或在排序阶段加入反馈特征，但必须用在线指标验证。",
        "5. **实体别名（中）**：精确匹配把 `Alpha 项目` 与 `项目 Alpha` 拆成两个实体。"
        "建议引入别名表/归一化键，写入时归并。",
        f"6. **事件候选召回未达建议线（中）**：{_pct(event_candidate['candidate_recall'])}，"
        f"低于 {_pct(REFERENCE_TARGETS['event_candidate_recall'])}；"
        f"其中 {len(event_candidate['zero_overlap_relevant'])} 条相关项与查询零词法重叠。"
        "建议先补同义词/实体别名索引，再用真实 BGE 判断语义召回缺口。",
        f"7. **精排代理无事件增益（低，待复测）**：事件 nDCG 变化 {rerank_delta_event:+.3f}，"
        f"统一搜索变化 {rerank_delta_unified:+.3f}。代理不是真实交叉编码器，"
        "需在真实 BGE + 精排环境复测后再判断。",
        "",
        "## 9. 在线评估口径（可选）",
        "",
        "离线标签无法代表真实流量，上线后建议按同一查询维度补充以下指标。"
        "数据源为 `memory_query` 与 `memory_access_log`（均按 query_id 关联）：",
        "",
        "| 指标 | 口径 |",
        "| --- | --- |",
        "| CTR@10 | 前 10 位曝光中被点击的结果数 / 前 10 位曝光数 |",
        "| 采纳率@10 | 前 10 位曝光中被采纳的结果数 / 前 10 位曝光数 |",
        "| 有帮助率 | helpful / (helpful + not_helpful)，排除未计分反馈 |",
        "| 反馈 nDCG@10 | 以 adopted=2、helpful/clicked=1、not_helpful=0 构造相关性，"
        "按离线公式计算 |",
        "| 零结果率 | 窗口内没有任何曝光行的 query / 总 query |",
        "| 意图分布 | event / wiki / mixed 占比，按 memory_type 与自动判定分组 |",
        "| 候选规模 | 每次查询 event_candidates、wiki_candidates 的 P50/P95 |",
        "| 去重与额度 | not_counted_reason 分布、每日额度命中率 |",
        "",
        "运行：`python -m evaluation.cli online`（需要 `DATABASE_URL`）。"
        "没有数据库时脚本会明确报错退出，不参与离线门禁。",
        "",
        "## 10. 复现",
        "",
        "一条命令重建数据集、跑离线评估并生成本报告：",
        "",
        "```powershell",
        ".\\evaluation\\run_all.ps1",
        "```",
        "",
        "等价命令：",
        "",
        "```powershell",
        ".\\.venv312\\Scripts\\python.exe -m evaluation.cli all",
        ".\\.venv312\\Scripts\\python.exe -m pytest evaluation/tests -q",
        "```",
        "",
        "产物：",
        "",
        "- `evaluation/data/eval_dataset.json`：数据集（可单独复用）",
        "- `evaluation/results/baseline.json`：机器可读指标",
        "- `evaluation/results/REPORT.md`：本报告",
        "",
        "## 11. 局限",
        "",
        "- 词法代理不是 BGE，事件模型包缺失，当前数字只能作为**下限**和回归护栏。",
        "- 代理阈值经过校准；生产阈值下的候选召回单独列出，避免口径混淆。",
        f"- {len(payload['queries'])} 条查询、单标注人，置信区间较宽；"
        "结论用于定位短板，不用于宣称绝对值。",
        "- 冷启动与长序列切片样本量小，切片结论需在真实流量上复核。",
        "",
    ]

    if online_result:
        sections.extend(
            [
                "## 12. 本次在线评估结果",
                "",
                "```json",
                __import__("json").dumps(online_result, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )

    return "\n".join(sections)


def write_report(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    output_path: Path = REPORT_PATH,
    online_result: dict[str, Any] | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_report(result, payload, online_result=online_result), encoding="utf-8"
    )
    return output_path
