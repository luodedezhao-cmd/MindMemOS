# Baseline 2：16,384 阈值同步全量压缩

## 代码来源

Baseline 2 与 [Baseline 1](../baseline1/README.md) 使用相同的上游 [OpenClaw](https://github.com/openclaw/openclaw)（`2026.3.11`）和固定版本 [WildClawBench](https://github.com/InternLM/WildClawBench/tree/56bc20d31ebf5fdee627c781b55a1e4959893bb0)。在这个底座上，我们增加了**每次主模型请求前检查、达到 16,384 门槛后同步全量摘要**的机制。

请求前调度、历史选择、摘要应用和 Qwen 路由是我们的实验代码；摘要生成函数 `generateSummary` 及其 prompt 沿用 OpenClaw 所依赖的 Pi 原生实现。下面区分相对 Baseline 1 的方法改动，以及将实验代码整理成可复现入口的封装改动。

## 相对 Baseline 1 的改动

1. **增加请求前触发检查。** Baseline 1 保留宿主原生压缩调度；Baseline 2 在每次主 Agent 请求 LLM 前检查，包括工具执行完成后的下一次请求。估计 token **≥ 16,384**、存在可压缩历史且 compaction 启用时，先尝试摘要。16,384 是我们新增的调度门槛，主模型的 `contextWindow` 仍为 200,000。
2. **明确计数和保留范围。** 触发量按与我们异步方法一致的口径计算：已有摘要 `M` 加可压缩历史 `eligible_raw`。初始 task、固定 system/developer 前缀、工具 schema 和最近完整组不计入门槛。一个工具组包含 assistant 的工具调用及其全部结果，不能只留下或删掉其中一条 observation。
3. **同步全量重写历史。** 达到门槛后，主模型请求等待原生 `generateSummary` 完成，将旧摘要与全部可压缩历史一起生成新摘要并写回会话，再继续请求主模型。初始 task 作为摘要参考且在主上下文中保留原文；最近完整组也保留原文。本实现不采用异步后台摘要或增量追加摘要。
4. **为摘要指定 Qwen122B。** 新增 [qwen_transport.py](qwen_transport.py)，将摘要请求的 `model` 改为 `qwen/qwen3.5-122b-a10b`、`reasoning` 改为 `high`。摘要 prompt 和其他请求参数沿用原生生成函数；主模型、评分及辅助请求的正文透传。
5. **替换原生压缩调度。** 设置 `compaction.mode=default`，本层不加载 safeguard；将 Pi 的 `_checkCompaction` 调度置空，由请求前检查统一触发摘要。这部分确实改变了宿主压缩行为，是我们添加的研究扩展。
6. **规定摘要后的继续方式。** 每次主请求最多尝试一次摘要；压缩后即使仍超过 16,384，也继续请求主模型，不增加输入硬上限。摘要失败或为空时保留原历史；历史持久化或工具组边界校验不匹配时中止当前 Agent。

上述扩展通过 [native_sync_full.py](native_sync_full.py) 修改 Pi `AgentSession` 的初始化并安装 [native_sync_full.mjs](native_sync_full.mjs) 请求包装器；计数和分组位于 [native_sync_full_accounting.mjs](native_sync_full_accounting.mjs)。OpenClaw 主 bundle、原生 `generateSummary`、实际 provider 和工具循环文件保持原样。Baseline 2 因此包含代码扩展，不是仅调整原生配置。

两者使用相同的主模型 GPT-5.5、评分模型 GPT-5.4、任务列表和任务截止时间，均不设置主模型 thinking override。同步摘要的等待计入任务截止时间。

实验接入也有差别：Baseline 1 使用容器内 observer 计量；Baseline 2 使用 relay 的 `request_ledger.jsonl` 和摘要事件日志，不叠加 Baseline 1 的 observer 补丁。图像请求分别注册为 `openrouter-vision/openai/gpt-5.5` 和 `openrouter/openai/gpt-5.5`，实际均为 GPT-5.5。两套入口的默认任务并发分别为 1 和 2，可用相同的 `--workers` 参数统一。

## 实现文件与来源

本目录整理自我们在 WildClawBench 工作区实现的同步全量压缩代码。表中的原文件路径指实验实现，不表示它们是 OpenClaw 或 WildClawBench 上游自带的功能。

| 文件 | 来源及用途 |
|---|---|
| `native_sync_full.py` | 原 `src/agents/openclaw/native_sync_full.py`，安装请求前检查补丁 |
| `native_sync_full.mjs` | 原同名文件，执行检查、原生摘要调用、持久化与上下文替换 |
| `native_sync_full_accounting.mjs` | 原同名文件，历史分组和与 ADyn 对齐的计数 |
| `model_probe.mjs` | 原模型描述探针，保留固定镜像的 provider 参数 |
| `qwen_transport.py` | 原 Qwen 同步实验 transport，只为摘要改模型与 reasoning |
| `transport_helpers.py` | 从原 transport 提取请求分类、原始 usage 解析和求和函数 |
| `agent.py` | 提取原 runner 的安装步骤及 `run31.py` 的 provider 接线 |
| `run.py` | 可移植运行入口，使用固定任务列表 |

前四个文件逐字复制。`qwen_transport.py` 只调整 helper 的导入路径；`transport_helpers.py` 提取原函数、不保留无关 provider relay。来源与 hash 见 [source_manifest.json](source_manifest.json)。

## 发布封装相对实验代码的改动

新增入口将固定本机目录改为参数，复用固定 WildClawBench 的 `run_single_task()` 和原生 `OpenClawAgent`，不携带额外重跑队列、历史结果选择和研究批次状态。

同步补丁由 `agent.py` 在 `_set_model()` 接线阶段安装，发生在 gateway 启动之前；原实验在同一次容器准备流程中安装。已保留相同的 native helper、计数、prompt、保留边界和阈值。共享入口的首任务顺序保留 arXiv 优先，但不包含研究队列的 canary 放行条件；每项的模型执行逻辑不依赖该队列条件。

## 模型与参数

| 项目 | 值 |
|---|---|
| 主 Agent | `openrouter/openai/gpt-5.5` |
| 压缩器 | `qwen/qwen3.5-122b-a10b` |
| 压缩器 reasoning | `high`，与我们的方法相同 |
| 压缩模式 | 同步、全量 |
| 触发门槛 | 估计 token **≥ 16,384** |
| 计数范围 | 已有摘要 `M` + 可压缩历史 `eligible_raw` |
| 排除项 | 初始 task、system/developer 固定前缀、工具 schema、最近完整组 |
| 最近历史保留 | 至少 1 个完整工具组 |
| compaction mode | `default`，不加载 safeguard |
| 原生 usage 检查 | 在该变体中禁用 |
| 主模型 descriptor contextWindow | 200,000，保持原值 |
| 主模型 descriptor maxTokens | 8,192，保持原值 |
| reserveTokens | 20,000，保持原值 |
| 原生摘要 maxTokens | `floor(0.8 × 20000)` = 16,000 |
| 压缩后仍超过门槛 | 继续请求主模型；不循环压缩，不额外拒绝 |
| 单次主请求的摘要尝试 | 最多一次 |
| 评分模型 | `openai/gpt-5.4` |
| 默认并发 | 2 |

触发量为 `ceil(UTF8_bytes(canonical_JSON(memoryMessages(M) + eligible_raw)) / 4)`。它是调度估值，不是模型 tokenizer 或完整请求输入量。原生摘要 prompt 保持原样，旧摘要放入 conversation 一起重写；不调用 SDK 自有的增量／全量 prompt。

初始 task 不计门槛，但提供给摘要器作为参考。一个完整工具组包括 assistant 的并行工具调用及其全部结果；不跨越不完整或多模态边界。摘要失败或为空时保留原历史，边界不匹配时中止当前 Agent。同步等待占用原任务截止时间。

## 运行

依赖准备见[公共说明](../README.md)。从 MindMemOS 仓库根目录：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/baselines/baseline2/run.py \
  --benchmark-root /path/to/WildClawBench --all

python src/mindmemos_sdk/mindmemos_sdk/compression/baselines/baseline2/run.py \
  --benchmark-root /path/to/WildClawBench \
  --all --env-file /path/to/private.env \
  --output /path/to/new-baseline2-output --execute
```

单任务使用 `--task <task_id>` 替换 `--all`。此入口会同时安装同步补丁和 Qwen 路由，不需要手动替换主模型。relay 使用 Linux Docker 默认 bridge 地址 `172.17.0.1`，运行环境需要容器能访问宿主该地址。

输出包括任务评分和轨迹、`request_ledger.jsonl`、每任务 `native_sync_full_patch.json`、模型 descriptor 和 `sync_full/*/events.jsonl`。检查实际摘要模型应看 ledger 的 `model_out`；SDK 原始 descriptor 仍为 GPT-5.5。判断摘要实际应用应看 `compaction_end.applied`，不能只统计请求发起数。任务列表位于 [compression/task_list_31.md](../../task_list_31.md)。
