# OpenClaw 实验 baseline

- [Baseline 1：无压缩](baseline1/README.md)
- [Baseline 2：16,384 阈值同步全量压缩](baseline2/README.md)
- [31 项任务列表](../task_list_31.md) / [JSON 清单](../task_list_31.json)

两个入口复用 WildClawBench 的任务执行和评分流程，主模型均为 GPT-5.5。它们是独立实验工具，不改变 MindMemOS SDK 的 `compress_memory()` 接口，也不要求启动 MindMemOS 服务。

## 公共依赖

需要 Python 3.11、Docker 和单独的 WildClawBench 工作区。基准代码固定为：

```bash
git clone https://github.com/InternLM/WildClawBench.git
git -C WildClawBench checkout 56bc20d31ebf5fdee627c781b55a1e4959893bb0
python -m pip install -r WildClawBench/requirements.txt
```

按照 WildClawBench README 准备干净的任务 workspace、fixture 和工具凭据。

实验镜像是 WildClawBench 官方的 OpenClaw 镜像 `wildclawbench-ubuntu:v1.3`，从 HuggingFace 下载并加载：

```bash
pip install -U "huggingface_hub[cli]"
hf download internlm/WildClawBench Images/wildclawbench-ubuntu_v1.3.tar --repo-type dataset --local-dir .
docker load -i Images/wildclawbench-ubuntu_v1.3.tar
docker image inspect wildclawbench-ubuntu:v1.3 --format '{{.Id}}'
```

最后一条输出的镜像 ID 应为：

```text
sha256:60eec8752cb597e180780ff08d7569c1892c169521f1f2b069c2efeb006a4078
OpenClaw 2026.3.11 / Pi coding agent 0.57.1
```

代码、任务输入 hash 和镜像 ID 记录在 [pinned_benchmark.json](pinned_benchmark.json)。执行前检查基准源码和任务输入，拒绝混入其他版本。镜像、任务数据、API 凭据和实验日志不包含在此目录。

从 MindMemOS 仓库根目录运行各目录的 `run.py`。省略 `--execute` 时只检查源码／任务 hash 并显示计划，不启动容器或调用模型。`--execute` 需要新的 `--output` 目录；使用 `--task <task_id>` 或 `--all` 选择范围。凭据通过环境或 `--env-file` 注入，不复制到输出目录。

共享 [common.py](common.py) 负责参数、路径、固定模型、任务队列和结果保存。它复用固定版本的 `run_single_task()`，不重新实现任务 prompt、工具循环或评分。最多两个任务并发，没有自动整任务重跑。初始任务先独立执行，之后调度剩余任务；这个可移植入口不包含原研究队列的人工放行、历史结果选择或补跑调度。

## 验证与输出口径

```bash
python -m unittest discover \
  -s src/mindmemos_sdk/mindmemos_sdk/compression/baselines/tests -v
```

测试使用本机模拟上游，不调用真实模型。压缩核心与计量源码的原样复制身份见两个目录各自的 `source_manifest.json`；新入口属于路径和接入方式的整理，不能把离线验证当作新的完整 31 项性能结果。

已准备固定镜像和 benchmark 依赖时，还可执行适配器及原生压缩边界检查：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/baselines/tests/container_smoke.py \
  --benchmark-root /path/to/WildClawBench
```

此检查创建 `--network none` 的临时容器，验证原生观测、图像模型注册、同步补丁和 12 种模拟请求场景，结束后删除容器，不调用模型 API。

原始 API 回执和轨迹分别保留。比较 token／费用时，匹配实际进入轨迹的主响应与实际应用的摘要；完整请求 ledger 还包含失败、未使用和辅助请求，不能直接全部求和冒充“已使用调用费用”。执行耗时与压缩器请求时长分别记录。未知用量不补零。

源自 WildClawBench 的代码遵循随附的 [MIT License](LICENSE_WildClawBench)。
