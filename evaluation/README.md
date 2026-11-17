# AG-07 质量评估

本目录是独立评估区，不修改 `backend/` 生产代码；只复用 `app.domain` 中的排序、
衰减、熟悉度与意图纯函数，保证评估口径与运行期一致。

## 快速开始

在 `memory-system` 根目录执行：

```powershell
.\evaluation\run_all.ps1
```

等价命令：

```powershell
.\.venv312\Scripts\python.exe -m evaluation.cli all
.\.venv312\Scripts\python.exe -m pytest evaluation/tests -q
```

`all` 会重建数据集、跑离线评估、写出 `results/baseline.json` 与
`results/REPORT.md`；若设置了 `DATABASE_URL`，还会附加在线指标。

其他入口：

```powershell
python -m evaluation.cli dataset    # 只生成数据集
python -m evaluation.cli offline    # 只跑离线评估与报告
python -m evaluation.cli online     # 只跑在线评估（需要 DATABASE_URL）
```

## 目录

```
evaluation/
  dataset.py                 # 确定性数据集构造器（唯一数据源）
  embedding.py               # 词法哈希向量 + IDF 词法精排代理
  metrics.py                 # Recall/Precision/MRR/MAP/Hit/nDCG + bootstrap CI
  runner.py                  # 离线评估执行器与消融配置
  report.py                  # Markdown 报告渲染
  online.py                  # 在线评估口径与只读 SQL
  cli.py                     # 统一入口
  run_all.ps1                # 一条命令：评估 + 测试
  data/eval_dataset.json     # 生成的数据集（可单独复用）
  results/baseline.json      # 机器可读结果
  results/REPORT.md          # 结果报告
  tests/test_evaluation.py   # 数据集/指标/执行器/在线口径测试
```

## 数据集

- **来源**：全部文本为本评估手写，题材取自设计文档 3.3 / 4.4 / 12.5
  与既有实现配置；不含真实用户数据、生产日志或第三方语料。
- **构造**：`dataset.py` 用固定命名空间 + 稳定 key 生成 UUIDv5；
  相同代码重复生成逐字节一致。
- **规模**：51 条事件、8 篇 Wiki / 24 个知识块、34 条查询。
- **标注**：每条查询带事件/Wiki 多级相关性（2=直接答案，1=相关背景），
  覆盖 event/wiki/mixed 意图与 `time_sensitive`、`cold_start`、
  `long_sequence` 三个切片；另有 1 条实体别名事件与困难负例。
- **反馈**：`feedback` 记录推导事件熟悉度（每次有效反馈 +0.08），
  用于评估反馈信号是否改变排序。

新增查询时在 `QUERIES` 中追加 `QuerySpec`，引用 `EVENTS` 的 key 与
`WIKI_ARTICLES` 的 chunk key，然后重跑 `python -m evaluation.cli dataset`。

## 指标

| 指标 | 定义 |
| --- | --- |
| Recall@K | 前 K 个结果覆盖的相关记忆数 / 全部相关记忆数 |
| Precision@K | 前 K 个结果中相关记忆数 / K |
| Hit@K | 前 K 个结果是否至少命中一个相关记忆 |
| MRR@K | 第一个相关结果排名倒数 |
| MAP@K | 各命中位置 precision 的均值，再按相关结果数归一化 |
| nDCG@K | 增益 2^rel-1，折损 1/log2(rank+1)，理想排序归一化 |
| 候选召回 | 标注相关项被候选集覆盖的比例（先定位召回瓶颈） |

Recall/Precision/MRR/MAP/Hit 将等级 1、2 都视为相关；nDCG 使用等级增益。
小型数据集额外给出 bootstrap 95% 置信区间。

## 离线配置

- `纯向量（dense）`：只按余弦相似度排序。
- `向量 + 精排`：候选召回后按 `relevance(cosine, rerank)` 排序。
- `完整排序`：再叠加时间衰减、熟悉度与 Wiki 权重。
- `统一搜索`：按意图权重融合事件与 Wiki，同一词条最多 2 个知识块。
- 消融：关闭精排、关闭熟悉度、Oracle 意图（量化意图分类损失）。

默认向量与精排是**确定性词法代理**，不是 BGE / LSTM；代理阈值 0.05
按词法余弦尺度校准，并额外报告生产阈值 0.15 下的候选召回。
配置真实 Provider 后可切换：

```powershell
python -m evaluation.cli offline --embedding real
```

真实模式要求 `.env.local` 中配置 `EMBEDDING_PROVIDER`；否则脚本会明确报错。
即使切换到真实向量，事件模型包缺失时门禁仍为 blocked。

## 在线评估口径

`online.py` 只读 `memory_query` 与 `memory_access_log`，窗口默认 30 天：

- CTR@10 = 前 10 位曝光中的点击行数 / 前 10 位曝光行数。
- 采纳率@10 = 前 10 位曝光中的 adopted 行数 / 前 10 位曝光行数。
- 有帮助率 = helpful / (helpful + not_helpful)，只看 `is_counted`。
- 反馈 nDCG@10：adopted=2，helpful/clicked=1，not_helpful=0，
  按曝光排名计算。
- 零结果率 = 窗口内没有任何曝光行的 query 占比。
- 去重与额度 = `not_counted_reason` 分布。
- 延迟未落库，需从 HTTP 指标另取。

## 门禁与局限

设计 4.4 的硬门槛是“测试集指标不得低于纯 BGE 基线”。本目录尚未配置
真实 BGE 与事件模型包，因此报告中的门禁状态为 **blocked**；当前数字
用于定位短板与做回归护栏，不作为发布结论。

其他局限：数据与标注为单人手写合成、切片的样本量小、词法代理在语义
改写上偏弱。详细结果见 [REPORT.md](results/REPORT.md)。
