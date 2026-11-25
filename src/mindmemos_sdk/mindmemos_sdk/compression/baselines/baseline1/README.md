# Baseline 1：无压缩

## 代码来源

Agent 与任务执行采用 [WildClawBench](https://github.com/InternLM/WildClawBench/tree/56bc20d31ebf5fdee627c781b55a1e4959893bb0) 的原生 OpenClaw runner 和 `eval/run_batch.py`。固定源码、镜像及准备方式见[公共说明](../README.md)。

本目录的 `observer/install.py`、`meter.mjs`、`metrics.py` 和 `model_registration.py` 原样复制自实验的 `host_observation` 代码；`vision_registration.json` 保存同一 GPT-5.5 的图像能力注册描述。逐文件来源与 SHA-256 见 [source_manifest.json](source_manifest.json)。

## 与来源代码的区别

[agent.py](agent.py) 从原 `host_observation/worker.py` 提取 Agent 接入逻辑，[run.py](run.py) 使用共享可移植入口。改动是将本机固定目录改为 `--benchmark-root`、`--output` 和 `--env-file`，并将计量结果保存在每项任务输出目录。

原生主模型调用、工具执行与评分函数复用 WildClawBench。观测钩子记录原始 usage 和原生 compaction 事件，不改写模型消息、不主动摘要。保留实验使用的 GPT-5.5 图像能力注册：图像工具使用 `openrouter-vision/openai/gpt-5.5`，仍是同一模型。

宿主 compaction 保留默认配置；“无压缩”指该任务集合的实验轨迹没有实际应用原生摘要，不表示强制关闭所有宿主上下文管理。新执行时仍应检查计量中的 `native_auto_compaction_started` 和 `native_auto_compaction_succeeded`。

## 模型与参数

| 项目 | 值 |
|---|---|
| 主 Agent | `openrouter/openai/gpt-5.5` |
| 主模型 thinking | 不设置 override |
| 评分模型 | `openai/gpt-5.4` |
| API | OpenRouter |
| 自定义压缩器 | 不启用 |
| 宿主原生 compaction | 默认配置 |
| 任务截止时间 | 沿用任务 Markdown |
| 默认并发 | 1 |

## 运行

从 MindMemOS 仓库根目录，先核对源码和任务：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/baselines/baseline1/run.py \
  --benchmark-root /path/to/WildClawBench --all
```

实际运行需要环境中的 `OPENROUTER_API_KEY` 和任务工具凭据，或一个包含它们的私有 dotenv 文件：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/baselines/baseline1/run.py \
  --benchmark-root /path/to/WildClawBench \
  --all --env-file /path/to/private.env \
  --output /path/to/new-baseline1-output --execute
```

单任务可将 `--all` 换成 `--task 01_Productivity_Flow_task_1_arxiv_digest`。输出包括 manifest、逐任务 result、正式评分、`chat.jsonl`、`meter/events.jsonl` 和 `measurement.json`。

## 与 Baseline 2 的区别

Baseline 1 直接使用原生历史，不安装固定阈值摘要扩展。Baseline 2 增加每次主模型请求前的检查，达到 16,384 时等待 Qwen122B 同步生成摘要。两者的主模型保持 GPT-5.5，使用相同的[任务列表](../../task_list_31.md)。
