# 内容任务规划 Agent

小影校园 AI Agent 开发岗笔试 · **题目二：内容任务规划 Agent**。

帮助运营同学在**预算与截止时间**内完成内容活动方案：检索素材 → 生成脚本 → 估算成本 → 保存方案；**估算超预算时自动换档重规划**，没有任何可行档位时返回未完成与缺口，工具失败时重试续跑并保留已成功步骤。Agent 基于 ReAct 循环动态决策，**仅保存方案，不执行真实投放**。

- 运行方式：CLI（Python 3.10+，零第三方依赖，无需密钥）
- 模型入口：离线确定性 Mock（默认）+ OpenAI 兼容 API（未随仓库验证，边界见下）
- 测试：9 个用例覆盖全部验收点 + 失败续跑加分项 + 步数上限 + CLI 边界，全部通过（[examples/test-results.txt](examples/test-results.txt)）

## 快速运行

在仓库根目录执行：

```bash
# 验收场景（输出 JSON 到 stdout）
python -m src.main --input examples/normal.input.json         # 正常完成 -> completed（standard 12 <= 预算 20）
python -m src.main --input examples/budget-overrun.input.json # 预算超限 -> completed（standard 12 > 预算 10，换档 lite 8 保存）
python -m src.main --input examples/no-feasible.input.json    # 无可行方案 -> incomplete（预算 5，最低档 8，缺口 3，不保存）
python -m src.main --input examples/missing.input.json        # 缺预算 -> missing_input（不调用工具，列出缺口）
python -m src.main --input examples/tool-failure.input.json   # 工具失败 -> completed（注入一次脚本生成失败，重试续跑；失败步骤保留 failed 记录）
# 重复执行：同一命令连跑两次，第二次 -> duplicate（方案仅一份）

# 自动化测试
python -m unittest discover -s tests -v
```

带 Trace 输出（每步含工具参数、观察结果、决策依据）：

```bash
python -m src.main --input examples/budget-overrun.input.json --trace-out examples/my-trace.json
```

约定：程序成功输出 JSON；业务异常（未完成/缺信息/保存失败/重复等）以 JSON 状态返回且退出码 0；输入文件不是合法 JSON 时返回非零退出码。默认方案写入 `data/plans.json`，重复运行同一输入会得到 `duplicate`（这是预期行为）；恢复初始状态可将 `data/plans.json` 重置为 `{"plans": {}, "next_plan_id": 1}`。

## 配置

复制 `.env.example` 为 `.env`（可选）。`AGENT_MODEL=mock`（默认）时**无需任何密钥**；`AGENT_MODEL=api` 时需配置 `OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL`。

> **Mock 边界**：Mock 模型是以"任务+历史观察"为输入的确定性策略函数，验证 ReAct 流程与工具协议，**不证明真实模型效果**——超预算换档、失败续跑、缺口计算均由同一策略依据观察结果推导，而非按场景硬编码。examples/ 中的全部运行记录均来自 Mock。

## 项目结构

```
├── README.md
├── requirements.txt            # 无第三方依赖声明
├── .env.example                # 变量清单，不含真实密钥
├── src/
│   ├── main.py                 # CLI 入口与输入文件校验
│   ├── agent.py                # ReAct 循环（步数上限/错误恢复/停止条件）
│   ├── model_client.py         # Mock 策略 + OpenAI 兼容 API 双入口
│   ├── tools.py                # 4 个业务工具（幂等/故障注入/费用事实与判断分离）
│   ├── prompts.py              # 系统提示词（任务边界/工具原则/失败处理/输出契约）
│   └── trace.py                # Trace 记录
├── tests/test_acceptance.py    # 验收点 + 红线 + CLI 边界
├── data/                       # 合成素材库、费用表、方案箱
├── examples/                   # 6 组输入/输出/Trace + 测试记录
└── docs/                       # 需求/方案/评估/AI 使用/复盘
```

## 评审导航

| 检查项 | 入口 |
| --- | --- |
| 业务目标与成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助范围与验证方式 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、学习收获、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 预算超限换档重规划（参考实例） | [examples/budget-overrun.trace.json](examples/budget-overrun.trace.json) |
| 无可行方案返回缺口 | [examples/no-feasible.output.json](examples/no-feasible.output.json) |
| 失败续跑且不虚报（加分项） | [examples/tool-failure.output.json](examples/tool-failure.output.json) |
| 重复执行与防重复 | [examples/duplicate.output.json](examples/duplicate.output.json) |

## 提交信息

- 题号：题目二（内容任务规划 Agent）
- 提交物：本仓库全部内容（代码、文档、合成数据、运行记录），不含真实密钥与隐私数据
- 评审可复现：按"快速运行"命令从零执行，结果应与 examples/ 一致（run_id、时间戳除外）
