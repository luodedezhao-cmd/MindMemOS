# Working-memory compression

1. **SDK 的职责**：Memory SDK 只负责压缩 Agent 选定的历史。上下文管理、是否及何时触发压缩、动态阈值、增量／全量模式选择、异步调度和摘要应用，都由 Agent 实现。

2. **其他 Agent 如何调用**：使用 `mindmemos_sdk.compression.compress_memory(task=..., memory=..., messages=..., mode=..., config=...)`。传入原始任务、已有记忆、待压缩历史和 `CompressionConfig`；`mode` 为 `"incremental"` 或 `"full"`。函数返回摘要，由 Agent 将它应用到自己的上下文。

3. **复现我们的实验**：使用 [agent/](agent/README.md) 下的代码，入口是 [agent/run.py](agent/run.py)。其中包含实验使用的上下文管理、动态阈值和异步压缩策略，模型、参数及运行方式见该目录的 README。31 项任务的分数、token、费用和耗时见 [comparison/](comparison/README.md)。

4. **复现 baseline**：使用 [baselines/](baselines/README.md) 下的代码。[Baseline 1](baselines/baseline1/README.md) 对应原生 OpenClaw 无压缩实验条件；[Baseline 2](baselines/baseline2/README.md) 对应 16,384 阈值的同步全量压缩。各目录的 README 提供复现说明。
