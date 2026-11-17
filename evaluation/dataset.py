"""评估数据集的唯一来源与构造逻辑。

来源说明
--------
- 全部文本为**本评估手写**，题材取自本仓库设计文档（12.5 端到端验收场景、
  4.4 模型验收、3.3 混合检索）与既有实现中的真实配置项；不包含任何真实用户数据、
  第三方语料或生产日志。
- 实体、事件、Wiki 词条、查询与反馈均为确定性构造：ID 由固定命名空间 + 稳定 key
  生成 UUIDv5，因此任意机器重复生成的结果逐字节一致。
- 事件时间集中在 2026-01 ~ 2026-03，用于触发时间衰减；评估时钟固定为
  2026-04-15T00:00:00Z，保证基线数字可复现。

构造方式
--------
1. 11 个实体覆盖：项目、人物、团队、客户、系统、冷启动偏好、长序列发布流水线。
2. 50 条事件按实体写入，包含决策/状态/风险/任务/事实/偏好 6 类；
   另含 1 条别名主体事件（"Alpha 项目"）用于暴露实体精确匹配的短板。
3. 8 篇 Wiki 词条、24 个知识块，覆盖余弦相似度、HNSW、LSTM、衰减、nDCG、
   Outbox、Pgvector、幂等与反馈额度。
4. 33 条查询，每条带多级相关性标注（2=直接答案，1=相关背景，0=无关），
   覆盖 event / wiki / mixed 三种意图以及 time_sensitive / cold_start /
   long_sequence 三个切片；部分查询故意使用别名、省略主体或同义表达。
5. 反馈记录用于推导事件熟悉度（adopted/helpful 每次 +0.08），
   用于评估"反馈信号是否改变排序"。

运行 ``python -m evaluation.cli dataset`` 可重新生成
``evaluation/data/eval_dataset.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from evaluation import DATASET_VERSION

EVAL_CLOCK = datetime(2026, 4, 15, tzinfo=UTC)
ID_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
OWNER_ID = "eval-analyst"
DEFAULT_IMPORTANCE = 0.7
DEFAULT_INITIAL_DECAY = 1.0
DATASET_PATH = Path(__file__).resolve().parent / "data" / "eval_dataset.json"


def stable_id(kind: str, key: str) -> UUID:
    return uuid5(ID_NAMESPACE, f"memory-system:{kind}:{key}")


@dataclass(frozen=True)
class EntitySpec:
    key: str
    name: str
    entity_type: str


@dataclass(frozen=True)
class EventSpec:
    key: str
    entity: str
    event_type: str
    title: str
    content: str
    day_offset: int
    sequence_no: int
    importance: float = DEFAULT_IMPORTANCE
    subject: str | None = None


@dataclass(frozen=True)
class WikiChunkSpec:
    key: str
    heading: tuple[str, ...]
    text: str
    importance: float = 0.5


@dataclass(frozen=True)
class WikiArticleSpec:
    key: str
    title: str
    categories: tuple[str, ...]
    chunks: tuple[WikiChunkSpec, ...]


@dataclass(frozen=True)
class QuerySpec:
    key: str
    text: str
    memory_type: str
    expected_intent: str
    event_labels: tuple[tuple[str, int], ...] = ()
    wiki_labels: tuple[tuple[str, int], ...] = ()
    slices: tuple[str, ...] = ()
    has_entity_filter: bool = False
    has_time_filter: bool = False


@dataclass(frozen=True)
class FeedbackSpec:
    event: str
    feedback_type: str
    count: int


ENTITIES: tuple[EntitySpec, ...] = (
    EntitySpec("alpha", "项目 Alpha", "project"),
    EntitySpec("beta", "项目 Beta", "project"),
    EntitySpec("zhangwei", "张伟", "person"),
    EntitySpec("lina", "李娜", "person"),
    EntitySpec("retrieval_team", "检索组", "team"),
    EntitySpec("ops", "模型服务运维", "team"),
    EntitySpec("xinghe", "客户星河科技", "organization"),
    EntitySpec("yunhai", "客户云海制造", "organization"),
    EntitySpec("pipeline", "发布流水线", "system"),
    EntitySpec("coffee", "用户-咖啡", "person"),
    EntitySpec("timezone", "用户-时区", "person"),
    EntitySpec("frontend", "前端控制台", "project"),
)

# day_offset 以 2026-01-01 为 0。
EVENTS: tuple[EventSpec, ...] = (
    # 项目 Alpha（含别名主体事件）
    EventSpec("a1", "alpha", "decision", "确定检索候选阈值",
              "项目 Alpha 决定将首版向量候选最低余弦相似度设为 0.15，每路候选数 100。",
              60, 1, 0.9),
    EventSpec("a2", "alpha", "status", "完成双路召回联调",
              "项目 Alpha 完成事件与 Wiki 双路召回联调，候选数与去重逻辑符合预期。",
              63, 2, 0.7),
    EventSpec("a3", "alpha", "risk", "精排延迟超过目标",
              "项目 Alpha 开启精排后搜索 P95 达到 1.2 秒，超过 800 毫秒目标。",
              67, 3, 0.85),
    EventSpec("a4", "alpha", "decision", "每个词条最多保留两个分块",
              "项目 Alpha 决定同一 Wiki 词条在搜索结果中最多保留 2 个知识块。",
              70, 4, 0.8),
    EventSpec("a5", "alpha", "task", "增加时间敏感切片评估",
              "项目 Alpha 的离线评估增加时间敏感查询切片，单独报告排序指标。",
              76, 5, 0.7),
    EventSpec("a6", "alpha", "fact", "镜像改用 CPU-only torch",
              "项目 Alpha 的镜像使用 CPU-only torch，移除 CUDA 运行时以缩小体积。",
              80, 6, 0.5),
    EventSpec("a7", "alpha", "status", "Alpha 项目完成灰度",
              "Alpha 项目完成 10% 灰度，搜索结果分数解释与反馈入口均已验证。",
              83, 7, 0.7, subject="Alpha 项目"),
    # 项目 Beta（与 Alpha 共享词汇的困难负例）
    EventSpec("b1", "beta", "decision", "Beta 采用更宽松的候选阈值",
              "项目 Beta 将向量候选最低余弦相似度设为 0.25，每路候选数 200。",
              61, 1, 0.8),
    EventSpec("b2", "beta", "status", "Beta 精排延迟达标",
              "项目 Beta 精排后搜索 P95 为 0.6 秒，低于 800 毫秒目标。",
              65, 2, 0.7),
    EventSpec("b3", "beta", "decision", "Beta 选用 BGE-large 向量",
              "项目 Beta 决定使用 BGE-large-zh-v1.5 作为基础向量模型。",
              69, 3, 0.75),
    EventSpec("b4", "beta", "fact", "Beta 回归集规模",
              "项目 Beta 维护 120 条查询回归集，覆盖事件与 Wiki 两路。",
              73, 4, 0.6),
    EventSpec("b5", "beta", "risk", "Beta 冷启动失败率偏高",
              "项目 Beta 新实体冷启动查询的无结果率达到 18%。",
              78, 5, 0.8),
    EventSpec("b6", "beta", "status", "Beta 灰度扩大到一半",
              "项目 Beta 灰度扩大到 50%，暂未观察到排序回退。",
              84, 6, 0.7),
    # 张伟
    EventSpec("z1", "zhangwei", "preference", "偏好深色主题",
              "张伟偏好深色主题，控制台默认跟随系统外观。", 40, 1, 0.6),
    EventSpec("z2", "zhangwei", "decision", "每周三做需求评审",
              "张伟安排在每周三下午做需求评审，不接受临时插会。", 44, 2, 0.7),
    EventSpec("z3", "zhangwei", "task", "负责反馈信号",
              "张伟负责反馈信号与熟悉度更新，关注十分钟去重规则。", 50, 3, 0.75),
    EventSpec("z4", "zhangwei", "fact", "前端使用 pnpm",
              "张伟要求前端统一使用 pnpm，不使用 npm 锁文件。", 55, 4, 0.5),
    # 李娜
    EventSpec("l1", "lina", "decision", "采用 nDCG@10 作为主指标",
              "李娜决定排序质量以 nDCG@10 为主指标，Recall@10 用于召回诊断。",
              41, 1, 0.8),
    EventSpec("l2", "lina", "risk", "担心新实体冷启动",
              "李娜担心新实体只有一两条事件时，Query Adapter 无法提供稳定排序。",
              48, 2, 0.75),
    EventSpec("l3", "lina", "preference", "报告要简洁",
              "李娜偏好单页结论先行的报告，细节放在附录。", 54, 3, 0.6),
    # 检索组
    EventSpec("t1", "retrieval_team", "decision", "每周五跑基线评估",
              "检索组决定每周五运行一次离线基线评估，记录 Recall@10、MRR@10 与 nDCG@10。",
              36, 1, 0.8),
    EventSpec("t2", "retrieval_team", "status", "基线评估脚本就绪",
              "检索组完成基线评估脚本，可在无 GPU 环境复现纯 BGE 对比。",
              57, 2, 0.7),
    EventSpec("t3", "retrieval_team", "task", "补充长序列切片",
              "检索组计划补充长序列查询切片，单独观察窗口截断的影响。",
              71, 3, 0.7),
    # 模型服务运维
    EventSpec("o1", "ops", "decision", "模型包只读挂载",
              "模型服务运维决定把模型包以只读方式挂载到容器 /models 目录。",
              35, 1, 0.8),
    EventSpec("o2", "ops", "risk", "CUDA 镜像体积过大",
              "模型服务运维发现带 CUDA 的镜像达到 6.8GB，构建经常超时中断。",
              42, 2, 0.8),
    EventSpec("o3", "ops", "task", "改用 CPU-only torch",
              "模型服务运维改用 CPU-only torch wheel，并在构建期落盘依赖。",
              49, 3, 0.7),
    EventSpec("o4", "ops", "status", "镜像构建通过",
              "模型服务运维完成干净镜像构建，事件编码在容器内可正常运行。",
              58, 4, 0.7),
    EventSpec("o5", "ops", "decision", "工作记忆过期清理口径",
              "工作记忆清理只删除已关闭且已过期的会话条目，保留会话元数据。",
              74, 5, 0.7),
    # 客户
    EventSpec("x1", "xinghe", "decision", "星河科技选择私有部署",
              "客户星河科技决定采用私有化部署，不接受跨租户共享 Wiki。", 38, 1, 0.8),
    EventSpec("x2", "xinghe", "risk", "星河科技要求合规删除",
              "客户星河科技要求隐私删除优先于记忆保护，删除后正文与向量不可检索。",
              46, 2, 0.85),
    EventSpec("x3", "xinghe", "fact", "星河科技数据规模",
              "客户星河科技预计有 10 万条活跃事件。", 52, 3, 0.6),
    EventSpec("y1", "yunhai", "decision", "云海制造先做 Wiki 同步",
              "客户云海制造决定首期只做 Wiki 同步与只读检索。", 39, 1, 0.75),
    EventSpec("y2", "yunhai", "task", "云海制造提供词条",
              "客户云海制造将提供约 3000 条内部词条用于同步测试。", 47, 2, 0.7),
    EventSpec("y3", "yunhai", "risk", "云海制造内网无外网",
              "客户云海制造的内网无法访问外网，远程 Embedding 服务不可用。", 53, 3, 0.8),
    # 发布流水线：12 条长序列
    EventSpec("p1", "pipeline", "status", "v1 上线", "发布流水线 v1 上线。", 4, 1, 0.6),
    EventSpec("p2", "pipeline", "task", "v2 增加回滚", "发布流水线 v2 增加一键回滚。", 11, 2, 0.6),
    EventSpec("p3", "pipeline", "decision", "v3 调整候选阈值",
              "发布流水线 v3 将候选最低余弦相似度从 0.20 调整为 0.15。", 18, 3, 0.9),
    EventSpec("p4", "pipeline", "task", "v4 增加灰度",
              "发布流水线 v4 增加 10% 灰度能力。", 25, 4, 0.6),
    EventSpec("p5", "pipeline", "task", "v5 调整精排批大小",
              "发布流水线 v5 将精排批大小调整为 32。", 32, 5, 0.6),
    EventSpec("p6", "pipeline", "task", "v6 增加死信告警",
              "发布流水线 v6 增加 Outbox 死信告警。", 39, 6, 0.7),
    EventSpec("p7", "pipeline", "task", "v7 修复备份脚本",
              "发布流水线 v7 修复数据库备份脚本的路径问题。", 46, 7, 0.7),
    EventSpec("p8", "pipeline", "task", "v8 增加限流",
              "发布流水线 v8 增加按用户限流。", 53, 8, 0.6),
    EventSpec("p9", "pipeline", "task", "v9 移除 CUDA",
              "发布流水线 v9 从运行镜像移除 CUDA 运行时。", 60, 9, 0.7),
    EventSpec("p10", "pipeline", "status", "v10 阈值维持不变",
              "发布流水线 v10 候选阈值维持 0.15 不变。", 67, 10, 0.6),
    EventSpec("p11", "pipeline", "decision", "v11 提高候选阈值",
              "发布流水线 v11 将候选阈值提高到 0.18。", 74, 11, 0.85),
    EventSpec("p12", "pipeline", "task", "v12 增加评估报告",
              "发布流水线 v12 增加每次发布的评估报告。", 81, 12, 0.7),
    # 冷启动实体
    EventSpec("c1", "coffee", "preference", "早上喝美式咖啡",
              "用户偏好早上喝美式咖啡，不加糖。", 59, 1, 0.6),
    EventSpec("u1", "timezone", "preference", "使用东八区时间",
              "用户所在时区为 Asia/Shanghai，排期按北京时间展示。", 58, 1, 0.6),
    # 前端控制台（干扰实体）
    EventSpec("f1", "frontend", "status", "搜索结果展示分数解释",
              "前端控制台在搜索结果中展示分数解释与模型版本。", 62, 1, 0.7),
    EventSpec("f2", "frontend", "decision", "图表使用 ECharts",
              "前端控制台决定图表统一使用 ECharts。", 66, 2, 0.7),
    EventSpec("f3", "frontend", "task", "Wiki 锚点跳转",
              "前端控制台完成 Wiki 结果到知识块的锚点跳转。", 72, 3, 0.7),
)

WIKI_ARTICLES: tuple[WikiArticleSpec, ...] = (
    WikiArticleSpec("w1", "余弦相似度", ("数学", "向量"), (
        WikiChunkSpec("w1c1", ("余弦相似度", "定义"),
                      "余弦相似度通过测量两个向量夹角的余弦值来衡量方向相似程度，"
                      "取值越接近 1 表示方向越一致。", 0.7),
        WikiChunkSpec("w1c2", ("余弦相似度", "归一化"),
                      "余弦相似度原始范围为 -1 到 1，检索评分中常用 (x + 1) / 2 "
                      "映射到 0 到 1。", 0.5),
        WikiChunkSpec("w1c3", ("余弦相似度", "应用"),
                      "在向量检索中，余弦相似度用于候选召回；复合排序还会结合精排分数、"
                      "时间衰减与熟悉度。", 0.6),
    )),
    WikiArticleSpec("w2", "HNSW", ("检索", "索引"), (
        WikiChunkSpec("w2c1", ("HNSW", "原理"),
                      "HNSW 是分层可导航小世界图，通过多层图结构实现高维向量的"
                      "近似最近邻搜索。", 0.6),
        WikiChunkSpec("w2c2", ("HNSW", "参数"),
                      "常见参数包括 M、ef_construction 与 ef_search；增大参数会提高召回，"
                      "同时增加内存与延迟。", 0.5),
        WikiChunkSpec("w2c3", ("HNSW", "适用场景"),
                      "HNSW 适合高维向量的在线近似最近邻检索，不适合需要精确全量扫描的场景。", 0.5),
    )),
    WikiArticleSpec("w3", "LSTM", ("模型", "序列"), (
        WikiChunkSpec("w3c1", ("LSTM", "遗忘门"),
                      "LSTM 遗忘门通过 sigmoid 输出决定上一时刻细胞状态保留多少，"
                      "首版实现不修改 PyTorch 门控内部。", 0.7),
        WikiChunkSpec("w3c2", ("LSTM", "状态更新"),
                      "细胞状态 c_t 与隐藏状态 h_t 在每个事件上迭代，"
                      "事件向量取隐藏状态投影。", 0.6),
        WikiChunkSpec("w3c3", ("LSTM", "重放"),
                      "重放是在当前状态上额外执行一次前向更新，不增加事件顺序号，"
                      "单事件最多重放 2 次。", 0.5),
    )),
    WikiArticleSpec("w4", "时间衰减", ("记忆", "评分"), (
        WikiChunkSpec("w4c1", ("时间衰减", "公式"),
                      "事件基础衰减 D_event = initial_decay_score * "
                      "exp(-DECAY_LAMBDA * age_days)。", 0.6),
        WikiChunkSpec("w4c2", ("时间衰减", "熟悉度"),
                      "熟悉度使有效记忆权重衰减更慢："
                      "W_event = min(D_event * (1 + 0.5 * F), 2.5)。", 0.6),
        WikiChunkSpec("w4c3", ("时间衰减", "归档"),
                      "delete_score 小于 0.1、事件超过 48 小时且非保护状态时才会自动归档。", 0.5),
    )),
    WikiArticleSpec("w5", "nDCG", ("检索", "指标"), (
        WikiChunkSpec("w5c1", ("nDCG", "定义"),
                      "nDCG 是归一化折损累计增益，用于衡量带相关性等级的排序质量。", 0.6),
        WikiChunkSpec("w5c2", ("nDCG", "计算"),
                      "DCG 按位置折损累加相关性增益，nDCG 再用理想排序的 DCG 归一化。", 0.6),
        WikiChunkSpec("w5c3", ("nDCG", "用途"),
                      "nDCG 适合有多级相关性标注的排序评估，常与 Recall 和 MRR 一起报告。", 0.5),
    )),
    WikiArticleSpec("w6", "Outbox", ("架构", "可靠性"), (
        WikiChunkSpec("w6c1", ("Outbox", "事务消息"),
                      "Outbox 与业务写入在同一数据库事务中落库，再由分发器投递到消息队列。", 0.6),
        WikiChunkSpec("w6c2", ("Outbox", "重试"),
                      "分发失败时按退避策略重试，并记录重试次数与错误类型。", 0.5),
        WikiChunkSpec("w6c3", ("Outbox", "死信"),
                      "超过最大重试次数的消息进入死信，管理员可查询并重放死信任务。", 0.5),
    )),
    WikiArticleSpec("w7", "Pgvector", ("数据库", "检索"), (
        WikiChunkSpec("w7c1", ("Pgvector", "索引"),
                      "Pgvector 支持 HNSW 与 IVFFlat 索引，本系统事件与 Wiki 向量均使用 "
                      "vector_cosine_ops。", 0.6),
        WikiChunkSpec("w7c2", ("Pgvector", "距离算子"),
                      "余弦距离算子为 <=>，返回 1 - 余弦相似度，排序时按距离升序。", 0.5),
        WikiChunkSpec("w7c3", ("Pgvector", "过滤"),
                      "权限与标量过滤必须写入候选 SQL 的 WHERE 条件，在召回前生效，"
                      "不能在排序后补过滤。", 0.7),
    )),
    WikiArticleSpec("w8", "幂等与反馈额度", ("反馈", "评分"), (
        WikiChunkSpec("w8c1", ("幂等", "幂等键"),
                      "同一幂等键重复提交返回首次结果，不产生重复副作用。", 0.5),
        WikiChunkSpec("w8c2", ("反馈", "十分钟去重"),
                      "同会话、同一规范化查询哈希、同一记忆在 10 分钟内只计一次有效反馈。", 0.6),
        WikiChunkSpec("w8c3", ("反馈", "每日额度"),
                      "单次有效反馈最多增加熟悉度 0.08，每日累计最多 0.3。", 0.6),
    )),
)

QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec("q_alpha_threshold", "项目 Alpha 之前确定的检索候选最低相似度是多少？",
              "all", "event", event_labels=(("a1", 2), ("a4", 1), ("b1", 1))),
    QuerySpec("q_alpha_rerank_risk", "Alpha 精排 P95 超过目标后记录了什么风险？",
              "all", "event", event_labels=(("a3", 2), ("b2", 1))),
    QuerySpec("q_alpha_gray", "Alpha 项目灰度结果如何？",
              "all", "event", event_labels=(("a7", 2), ("a2", 1))),
    QuerySpec("q_beta_compare", "Beta 的候选数和阈值与 Alpha 有何不同？",
              "all", "event", event_labels=(("b1", 2), ("a1", 1))),
    QuerySpec("q_zhang_theme", "张伟偏好什么主题？",
              "all", "event", event_labels=(("z1", 2),)),
    QuerySpec("q_zhang_review", "张伟每周什么时候做评审？",
              "event", "event", event_labels=(("z2", 2),)),
    QuerySpec("q_lina_cold", "李娜为什么关注冷启动指标？",
              "all", "event", event_labels=(("l2", 2), ("l1", 1))),
    QuerySpec("q_team_cadence", "检索组多久做一次基线评估？",
              "event", "event", event_labels=(("t1", 2), ("t2", 1))),
    QuerySpec("q_ops_cuda", "模型运维为什么放弃带 CUDA 的镜像？",
              "all", "event", event_labels=(("o2", 2), ("o3", 1))),
    QuerySpec("q_xinghe_delete", "客户星河科技对删除有什么要求？",
              "event", "event", event_labels=(("x2", 2), ("x1", 1))),
    QuerySpec("q_yunhai_data", "云海制造的数据量有多大？",
              "event", "event", event_labels=(("y2", 2), ("x3", 1))),
    QuerySpec("q_pipe_third", "发布流水线第 3 次发布改了什么阈值？",
              "all", "event", event_labels=(("p3", 2), ("p11", 1)),
              slices=("time_sensitive", "long_sequence")),
    QuerySpec("q_pipe_latest", "发布流水线最近一次发布把阈值调成了多少？",
              "all", "event", event_labels=(("p11", 2), ("p10", 1)),
              slices=("time_sensitive", "long_sequence")),
    QuerySpec("q_coffee", "用户早上喝什么咖啡？",
              "all", "event", event_labels=(("c1", 2),), slices=("cold_start",)),
    QuerySpec("q_timezone", "用户所在时区是什么？",
              "all", "event", event_labels=(("u1", 2),), slices=("cold_start",)),
    QuerySpec("q_frontend_chart", "前端控制台用的什么图表库？",
              "event", "event", event_labels=(("f2", 2),)),
    QuerySpec("q_cosine", "什么是余弦相似度？",
              "wiki", "wiki", wiki_labels=(("w1c1", 2), ("w1c2", 1))),
    QuerySpec("q_hnsw", "HNSW 索引适合什么场景？",
              "all", "wiki", wiki_labels=(("w2c1", 2), ("w2c3", 1))),
    QuerySpec("q_lstm", "LSTM 的遗忘门如何工作？",
              "all", "wiki", wiki_labels=(("w3c1", 2), ("w3c2", 1))),
    QuerySpec("q_decay", "事件衰减公式里的 lambda 是什么？",
              "wiki", "wiki", wiki_labels=(("w4c1", 2), ("w4c2", 1))),
    QuerySpec("q_ndcg", "nDCG 怎么计算？",
              "all", "wiki", wiki_labels=(("w5c2", 2), ("w5c1", 1))),
    QuerySpec("q_outbox", "Outbox 死信怎么处理？",
              "all", "wiki", wiki_labels=(("w6c3", 2), ("w6c2", 1))),
    QuerySpec("q_pgvector_filter", "Pgvector 如何在召回前做权限过滤？",
              "all", "wiki", wiki_labels=(("w7c3", 2), ("w7c1", 1))),
    QuerySpec("q_feedback_dedup", "反馈去重的十分钟规则是什么？",
              "wiki", "wiki", wiki_labels=(("w8c2", 2), ("w8c3", 1))),
    QuerySpec("q_team_ranking", "检索组如何评估排序质量？",
              "all", "mixed", event_labels=(("t1", 2), ("t2", 1)),
              wiki_labels=(("w5c3", 1),)),
    QuerySpec("q_alpha_cosine", "Alpha 的阈值和余弦相似度的关系？",
              "all", "mixed", event_labels=(("a1", 2),), wiki_labels=(("w1c1", 1), ("w1c3", 1))),
    QuerySpec("q_ops_outbox", "模型运维的镜像方案和 Outbox 有关吗？",
              "all", "mixed", event_labels=(("o3", 1), ("o4", 1)), wiki_labels=(("w6c1", 1),)),
    QuerySpec("q_pipe_who", "发布流水线的阈值后来又被谁调整过？",
              "event", "event", event_labels=(("p11", 2), ("p3", 1)),
              slices=("time_sensitive", "long_sequence")),
    QuerySpec("q_customers", "星河科技和云海制造分别处于什么阶段？",
              "all", "mixed", event_labels=(("x1", 2), ("y1", 2))),
    QuerySpec("q_idempotent", "幂等键重复提交会怎样？",
              "all", "wiki", wiki_labels=(("w8c1", 2), ("w8c2", 1))),
    QuerySpec("q_latest_eval", "最近一次基线评估发现了哪些短板？",
              "all", "mixed", event_labels=(("t2", 2), ("t3", 1)),
              slices=("time_sensitive",)),
    QuerySpec("q_vague_threshold", "向量候选下限是多少？",
              "all", "event", event_labels=(("a1", 2), ("b1", 1))),
    QuerySpec("q_working_memory_cleanup", "多久清一次过期的工作记忆？",
              "all", "event", event_labels=(("o5", 2),)),
    QuerySpec("q_replay_limit", "重放次数上限是多少？",
              "all", "wiki", wiki_labels=(("w3c3", 2),)),
)

FEEDBACK: tuple[FeedbackSpec, ...] = (
    FeedbackSpec("a1", "helpful", 2),
    FeedbackSpec("a3", "adopted", 1),
    FeedbackSpec("t1", "adopted", 3),
    FeedbackSpec("p3", "helpful", 3),
    FeedbackSpec("p11", "helpful", 2),
    FeedbackSpec("z1", "helpful", 2),
    FeedbackSpec("w1c1", "helpful", 3),
    FeedbackSpec("w5c2", "adopted", 2),
)

FAMILIARITY_STEP = 0.08


@dataclass
class DatasetBundle:
    payload: dict[str, Any]
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    wiki_chunks: dict[str, dict[str, Any]] = field(default_factory=dict)
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_datetime(day_offset: int, sequence_no: int) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
    return base.replace(hour=9 + (sequence_no % 8), minute=(sequence_no * 7) % 60)


def build_dataset() -> DatasetBundle:
    entity_ids = {entity.key: stable_id("entity", entity.key) for entity in ENTITIES}
    entity_names = {entity.key: entity.name for entity in ENTITIES}
    events: dict[str, dict[str, Any]] = {}
    payload_events: list[dict[str, Any]] = []
    for spec in EVENTS:
        event_id = stable_id("event", spec.key)
        event_time = _event_datetime(spec.day_offset, spec.sequence_no)
        record = {
            "key": spec.key,
            "event_id": str(event_id),
            "entity_id": str(entity_ids[spec.entity]),
            "entity_key": spec.entity,
            "canonical_subject": entity_names[spec.entity],
            "subject": spec.subject or entity_names[spec.entity],
            "event_type": spec.event_type,
            "title": spec.title,
            "content": spec.content,
            "event_time": _iso(event_time),
            "sequence_no": spec.sequence_no,
            "importance": spec.importance,
            "initial_decay_score": DEFAULT_INITIAL_DECAY,
            "familiarity_score": 0.0,
            "familiarity_updated_at": None,
            "status": "active",
            "visibility": "shared",
            "owner_id": OWNER_ID,
        }
        events[spec.key] = record
        payload_events.append(record)

    fragments: dict[str, list[dict[str, Any]]] = {}
    wiki_chunks: dict[str, dict[str, Any]] = {}
    payload_articles: list[dict[str, Any]] = []
    for article in WIKI_ARTICLES:
        article_id = stable_id("wiki_article", article.key)
        chunks = []
        for index, chunk in enumerate(article.chunks):
            chunk_id = stable_id("wiki_chunk", chunk.key)
            record = {
                "key": chunk.key,
                "chunk_id": str(chunk_id),
                "article_id": str(article_id),
                "article_key": article.key,
                "title": article.title,
                "chunk_index": index,
                "heading_path": list(chunk.heading),
                "raw_text": chunk.text,
                "importance": chunk.importance,
                "familiarity_score": 0.0,
                "familiarity_updated_at": None,
                "status": "active",
                "model_version": "eval-proxy-bge",
            }
            wiki_chunks[chunk.key] = record
            chunks.append(record)
            fragments.setdefault(article.key, []).append(record)
        payload_articles.append(
            {
                "article_id": str(article_id),
                "key": article.key,
                "title": article.title,
                "source": "memory-system-eval-handbook",
                "source_url": f"https://example.invalid/wiki/{article.key}",
                "source_page_id": f"page-{article.key}",
                "revision_id": "rev-1",
                "language": "zh",
                "categories": list(article.categories),
                "status": "active",
                "chunks": chunks,
            }
        )

    query_records: list[dict[str, Any]] = []
    queries: dict[str, dict[str, Any]] = {}
    for spec in QUERIES:
        record = {
            "key": spec.key,
            "query_id": str(stable_id("query", spec.key)),
            "text": spec.text,
            "memory_type": spec.memory_type,
            "expected_intent": spec.expected_intent,
            "event_labels": [
                {"key": key, "gain": gain, "memory_id": str(stable_id("event", key))}
                for key, gain in spec.event_labels
            ],
            "wiki_labels": [
                {"key": key, "gain": gain, "memory_id": str(stable_id("wiki_chunk", key))}
                for key, gain in spec.wiki_labels
            ],
            "slices": list(spec.slices),
            "has_entity_filter": spec.has_entity_filter,
            "has_time_filter": spec.has_time_filter,
        }
        queries[spec.key] = record
        query_records.append(record)

    feedback_records: list[dict[str, Any]] = []
    for spec in FEEDBACK:
        is_wiki = spec.event.startswith("w")
        memory_id = stable_id("wiki_chunk" if is_wiki else "event", spec.event)
        target = wiki_chunks[spec.event] if is_wiki else events[spec.event]
        target["familiarity_score"] = round(spec.count * FAMILIARITY_STEP, 2)
        target["familiarity_updated_at"] = _iso(
            datetime(2026, 4, 1, tzinfo=UTC) + timedelta(days=FEEDBACK.index(spec))
        )
        anchor_query = next(
            (
                query
                for query in query_records
                if any(
                    label["memory_id"] == str(memory_id)
                    for label in query["event_labels"] + query["wiki_labels"]
                )
            ),
            query_records[0],
        )
        for index in range(spec.count):
            feedback_records.append(
                {
                    "query_id": anchor_query["query_id"],
                    "memory_type": "wiki" if is_wiki else "event",
                    "memory_id": str(memory_id),
                    "feedback_type": spec.feedback_type,
                    "is_counted": True,
                    "sequence": index + 1,
                }
            )

    payload = {
        "version": DATASET_VERSION,
        "generated_at": _iso(datetime(2026, 4, 2, tzinfo=UTC)),
        "eval_clock": _iso(EVAL_CLOCK),
        "provenance": {
            "source": "本评估手写；题材取自设计文档 3.3 / 4.4 / 12.5 与既有实现配置",
            "contains_real_user_data": False,
            "construction": "确定性 Python 构造器 + UUIDv5 稳定 ID，重复生成逐字节一致",
            "generator": "evaluation/dataset.py",
        },
        "entities": [
            {
                "key": entity.key,
                "entity_id": str(entity_ids[entity.key]),
                "entity_name": entity.name,
                "entity_type": entity.entity_type,
                "owner_id": OWNER_ID,
                "visibility": "shared",
            }
            for entity in ENTITIES
        ],
        "events": payload_events,
        "wiki_articles": payload_articles,
        "queries": query_records,
        "feedback": feedback_records,
    }
    return DatasetBundle(payload=payload, events=events, wiki_chunks=wiki_chunks, queries=queries)


def validate_dataset(payload: dict[str, Any]) -> list[str]:
    """返回不变量问题列表；空列表表示数据集可用。"""
    problems: list[str] = []
    event_ids = {item["event_id"] for item in payload.get("events", [])}
    chunk_ids = {
        chunk["chunk_id"]
        for article in payload.get("wiki_articles", [])
        for chunk in article.get("chunks", [])
    }
    if len(event_ids) != len(payload.get("events", [])):
        problems.append("事件 ID 不唯一")
    chunk_total = sum(
        len(article.get("chunks", [])) for article in payload.get("wiki_articles", [])
    )
    if len(chunk_ids) != chunk_total:
        problems.append("Wiki 知识块 ID 不唯一")

    query_ids: set[str] = set()
    for query in payload.get("queries", []):
        query_id = query["query_id"]
        if query_id in query_ids:
            problems.append(f"查询 ID 重复：{query_id}")
        query_ids.add(query_id)
        if not query.get("event_labels") and not query.get("wiki_labels"):
            problems.append(f"查询无相关性标注：{query['key']}")
        for label in query.get("event_labels", []):
            if label["memory_id"] not in event_ids:
                problems.append(f"查询 {query['key']} 引用了不存在的事件 {label['key']}")
            if label["gain"] not in {1, 2}:
                problems.append(f"查询 {query['key']} 的相关性等级必须在 1 或 2")
        for label in query.get("wiki_labels", []):
            if label["memory_id"] not in chunk_ids:
                problems.append(f"查询 {query['key']} 引用了不存在的知识块 {label['key']}")
            if label["gain"] not in {1, 2}:
                problems.append(f"查询 {query['key']} 的相关性等级必须在 1 或 2")
        if query["expected_intent"] not in {"event", "wiki", "mixed"}:
            problems.append(f"查询 {query['key']} 的期望意图无效")

    for event in payload.get("events", []):
        if not event["title"].strip() or not event["content"].strip():
            problems.append(f"事件 {event['key']} 缺少标题或正文")
        if len(event["title"]) > 256:
            problems.append(f"事件 {event['key']} 标题超过 256 字符")
        if not event["event_time"].endswith("Z"):
            problems.append(f"事件 {event['key']} 时间必须为 UTC")

    slices = {name for query in payload.get("queries", []) for name in query.get("slices", [])}
    for required in ("time_sensitive", "cold_start", "long_sequence"):
        if required not in slices:
            problems.append(f"缺少切片：{required}")
    return problems


def write_dataset(path: Path = DATASET_PATH) -> Path:
    bundle = build_dataset()
    problems = validate_dataset(bundle.payload)
    if problems:
        raise ValueError("评估数据集不合法：" + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle.payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    if not path.is_file():
        write_dataset(path)
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    target = write_dataset()
    print(f"已生成评估数据集：{target}")
