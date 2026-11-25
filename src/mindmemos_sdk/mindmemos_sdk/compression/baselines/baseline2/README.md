# Baseline 2：16,384 阈值同步全量压缩

## 代码来源

本目录来自 WildClawBench 实验中的同步全量 v2 实现，底座与 [Baseline 1](../baseline1/README.md) 相同。

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

## 与来源代码的区别

新增入口将固定本机目录改为参数，复用固定 WildClawBench 的 `run_single_task()` 和原生 `OpenClawAgent`，不携带额外重跑队列、历史结果选择和研究批次状态。

同步补丁由 `agent.py` 在 `_set_model()` 接线阶段安装，发生在 gateway 启动之前；原实验在同一次容器准备流程中安装。已保留相同的 native helper、计数、prompt、保留边界和阈值。共享入口的首任务顺序保留 arXiv 优先，但不包含研究队列的 canary 放行条件；每项的模型执行逻辑不依赖该队列条件。

OpenClaw 主 bundle、原生 `generateSummary`、实际 provider 和工具循环文件不改写。补丁修改 Pi `AgentSession` 的初始化，安装请求包装器，并设 `compaction.mode=default`。该 baseline 是研究 harness 扩展，不是仅调整原生配置。

## 相对 Baseline 1 的改动

1. 每次主模型请求前检查可压缩历史，包含工具返回后的下一次请求。
2. 达到门槛时，同步调用原生 `generateSummary`；等待并应用摘要后才请求主模型。
3. 摘要覆盖旧摘要和全部可压缩历史；初始 task 和最近完整组保留原文。
4. 本层不加载 safeguard，禁用原 `_checkCompaction` 调度，由请求前检查统一调度。
5. 摘要请求经 Qwen 路由；主模型、评分和辅助请求正文保持透传。

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
