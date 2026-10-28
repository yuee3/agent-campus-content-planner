# 03 - 评估（Evaluation）

评分方式：**确定性评分器**（unittest 断言）——本任务输出为结构化 JSON 且行为可枚举，笔试范围内不需要 LLM-as-judge（留作下一步，见 05）。

## 测试执行方式

```bash
python -m unittest discover -s tests -v
```

实际运行记录：[examples/test-results.txt](../examples/test-results.txt)（9 个用例全部通过）。

## 用例、预期与实际结果

5 个必须验收点 + 重复执行 + 加分项（失败续跑）+ 步数边界 + 2 个 CLI 边界，全部使用隔离临时数据目录，不污染仓库数据：

| # | 验收点 | 输入 | 预期行为 | 实际结果 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | 正常完成 | 预算 20（standard 档 12 足够） | 检索→脚本→估算→保存；`completed`，费用合计 12 | 通过：4 步全 success，方案写入 | [normal.output.json](../examples/normal.output.json) / [normal.trace.json](../examples/normal.trace.json) |
| 2 | 预算超限（参考实例） | 预算 10 | standard 估算 12 超限 → **换 lite 档**再生成、再估算 → 保存 8；`completed` | 通过：工具序列含两轮 generate/estimate，首次估算 12、重规划决策依据含"超出预算"、最终存 lite 8 | [budget-overrun.output.json](../examples/budget-overrun.output.json) / [budget-overrun.trace.json](../examples/budget-overrun.trace.json) |
| 3 | 无可行方案 | 预算 5 | 连 lite 都超限 → **不保存**，返回未完成 + 缺口 3 | 通过：`incomplete`，reason 含"缺口"与"3"，方案箱为空 | [no-feasible.output.json](../examples/no-feasible.output.json) / [no-feasible.trace.json](../examples/no-feasible.trace.json) |
| 4 | 缺预算/截止时间 | 删除 budget 字段 | 不调用任何工具，返回 `missing_input` + 缺口清单 | 通过：0 次工具调用，方案箱为空 | [missing.output.json](../examples/missing.output.json) / [missing.trace.json](../examples/missing.trace.json) |
| 5 | 工具失败 + 失败续跑（加分项） | 注入 `script_fail_once=true` | 首次脚本生成 failed → 重试 success → 后续步骤照常 → `completed` | 通过：steps 为 success/failed/success/success/success，方案正常保存 | [tool-failure.output.json](../examples/tool-failure.output.json) / [tool-failure.trace.json](../examples/tool-failure.trace.json) |
| 6 | 重复执行 | 相同请求连续运行两次 | 第二次返回 `duplicate`，方案只有一份、ID 不变 | 通过：两次 saved_plan_id 相同 | [duplicate.output.json](../examples/duplicate.output.json) / [duplicate.trace.json](../examples/duplicate.trace.json) |
| 7 | 步数上限 | max_steps=2 | 返回明确 `incomplete` + 上限说明 | 通过 | tests/test_acceptance.py::test_7 |
| 8 | CLI 边界 | 合法输入经 CLI | 退出码 0，输出 JSON，Trace 落盘 | 通过 | tests/test_acceptance.py::CliTests |
| 9 | CLI 边界 | 非法 JSON | 非零退出码 + stderr JSON | 通过 | tests/test_acceptance.py::CliTests |

## 红线断言（所有场景统一检查）

`tests/test_acceptance.py::assert_red_lines` 对每个场景断言：

1. 只要存在已保存方案：`cost.within_budget=True` 且 `estimated_total ≤ budget`——**超预算方案不可能被存为可行**；
2. 步骤状态只能是 `success/failed`，且失败记录原样保留（**失败步骤不被改写为成功**）；
3. `incomplete / missing_input` 必须给出 `incomplete_reason`（原因与缺口）；
4. `input_summary.budget` 原样回显，未被更改。

预算超限场景额外断言：工具序列真实包含两轮 generate/estimate、首次估算观察 total=12、重规划步的 decision_basis 写明"超出预算"、最终只保存 lite 档（方案箱中 `校园咖啡节:lite` 的 estimated_total=8）。

## 两条不同执行路径的运行记录

- **路径 A（预算内直存）**：[normal.trace.json](../examples/normal.trace.json) —— 观察到 standard 档估算 12 ≤ 预算 20 → 决策依据"standard 档估算 12 在预算 20 内，保存该方案"。
- **路径 B（超预算重规划）**：[budget-overrun.trace.json](../examples/budget-overrun.trace.json) —— 观察到估算 12 > 预算 10 → 决策依据"观察到 standard 档估算 12 超出预算 10，改选 lite 档重新生成脚本并再估算"。

同一 Mock 策略在不同观察下走出不同路径，体现"根据工具结果调整动作、调整计划"的动态决策。

## 已知限制

1. **Mock 边界**：examples/ 全部结果来自离线确定性 Mock 模型，只验证 ReAct 流程与工具协议，**不证明真实模型效果**；`AGENT_MODEL=api` 路径（OpenAI 兼容）未随本仓库验证。
2. 未覆盖真实模型的不确定性（格式漂移、幻觉、工具误选），因此确定性断言对 API 模式的保护有限。
3. 档位只有 standard/lite 两档、费用为固定报价表；真实场景的议价、分项取舍（如砍渠道推广降成本）未实现。
4. 截止时间 deadline 仅参与信息完整性校验，未做时间可行性推演（如按 tool_fees 的耗时估算能否赶上截止）。
5. 评分器为确定性检查，缺少 LLM-as-judge 对脚本大纲质量的评估。
