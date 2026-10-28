# 02 - 方案（Solution）

设计方法遵循"通用 Agent 设计方法论"三板斧推导：**先拆材料与能力入口，再编排推理循环，最后定义评测与回流**。

## 一、调用流程

```text
用户请求（input.json）
  └─> CLI（src/main.py：输入文件校验，非法 JSON 非零退出；缺预算/截止时间不在此拦截）
        └─> ReAct 循环（src/agent.py，步数上限默认 12）
              ├─ 模型决策（src/model_client.py：Mock 策略 / OpenAI 兼容 API 双入口）
              │     动作 = tool_call(name, args) 或 finish(payload)
              ├─ 工具执行（src/tools.py：只读 x3 + 唯一写操作 save_plan）
              │     观察结果（含异常转观察，错误恢复不终止循环）
              ├─ Trace 记录（src/trace.py：步骤/参数/返回摘要/决策依据）
              └─ 循环直到 finish 或达到上限（返回 incomplete）
```

典型路径为 4 次工具调用 + 1 次 finish：`search_materials → generate_script(standard) → estimate_cost → save_plan`（先查证后结论）；预算不够时在估算后追加"换档重生成 + 再估算"两步。

## 二、动态决策（ReAct 循环的核心）

模型不是走固定分支，而是根据**最近一次工具观察**调整下一步：

| 观察结果 | 下一步动作 | 最终状态 |
| --- | --- | --- |
| 缺 topic / budget / deadline | **直接结束**并列出缺口，不调用工具、不编造 | `missing_input` |
| 尚未检索素材 | `search_materials(topic)` | — |
| 检索返回 topic_not_found | **终止**（重试无意义），如实报告 | `incomplete` |
| 已检索，未生成脚本 | `generate_script(standard)`（初始档位） | — |
| 已生成脚本，估算档位不一致 | `estimate_cost(当前档位)`（费用事实由工具给出） | — |
| 估算 total > 预算，当前是 standard | **改选 lite 档重新生成并再估算**（重规划） | — |
| 估算 total > 预算，当前已是 lite | **停止并给出缺口**（缺口 = 估算 − 预算），不保存 | `incomplete` |
| 估算 total ≤ 预算 | `save_plan(topic, tier, 估算值, 脚本标题)` | — |
| 保存返回 saved | 依据观察收尾 | `completed` |
| 保存返回 duplicate | 如实报告方案已存在 | `duplicate` |
| 保存返回 failed | 如实报告失败原因 | `save_failed` |
| 其他工具失败（failed/tool_error） | **重试同一步骤一次**（失败续跑）；连续 2 次失败则终止并保留已成功步骤 | `incomplete` |

这些分支由**观察结果驱动**而非预设脚本：同一策略函数，预算 20 时直存（normal）、预算 10 时换档（budget-overrun）、预算 5 时给出缺口（no-feasible）、注入故障时先失败再续跑（tool-failure）——Trace（`examples/*.trace.json`）中每步的 `observation` 与 `decision_basis` 记录了"为什么走这条路"。

## 三、模块职责（对应第二板斧五模块）

| 模块 | 职责 | 边界 |
| --- | --- | --- |
| Prompt（`prompts.py`） | 任务边界、角色约束、工具原则（先检索后生成/先脚本后估算）、失败处理、输出契约 | 不承载长期流程与全部背景 |
| Agent Loop（`agent.py`） | 观察-行动循环、停止条件（finish / 步数上限）、错误恢复（异常转观察） | 不含业务判断，判断归模型 |
| Memory | 任务内状态：历史观察即记忆（模型每步基于完整历史决策） | 不做跨轮次长期记忆 |
| Skill | 不沉淀——一次性任务，流程未稳定重复 | — |
| Tool（`tools.py`） | 4 个原子工具：素材检索/脚本生成/成本估算/方案保存 | 不承载完整业务流程 |

**为什么不上多 Agent**：任务无并行子任务、无权限隔离需求、上下文短，单 Agent + 工具足够；按"单 Agent 失效信号"逐条检查均不命中。

## 四、技术取舍

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 框架 | **原生实现**（不用 LangGraph/ADK 等） | 核心 Loop 仅 ~40 行，原生实现最透明、依赖为零，且避免"用框架默认行为代替设计解释" |
| 语言 | Python 3.10+ 标准库 | 零第三方依赖，评审可在隔离环境直接运行；API 模式用标准库 urllib 调 OpenAI 兼容接口 |
| 模型入口 | Mock（默认）+ API 双路径 | 无 Key 时全链路可离线验证；**Mock 边界：以"任务+历史观察"为输入的策略函数，验证 ReAct 流程与工具协议，不证明真实模型效果** |
| 运行方式 | CLI | 最贴合测试自动化与 Trace 落盘；界面/HTTP 非必需 |
| 材料接入 | 本地 JSON + 工具按需读取 | 材料量小且需精确主题/档位引用，不需要 RAG；单进程本地工具，不需要 MCP |

## 五、权限与安全边界（第一板斧：模型不是安全边界）

- **费用事实与预算判断分离（本题关键设计）**：`estimate_cost` 只返回 total 与 breakdown 的**费用事实，不判断是否超预算**；"估算 vs 预算"的对比、换档与缺口计算全部由模型完成。若工具层替模型判预算，重规划就成了预设分支——判断留在模型侧，重规划才是真实的动态决策。
- **结构性防越权**：工具层不存在投放/发布能力，`save_plan` 只写方案文件——"越权投放"在结构上不可能发生；Prompt 同步约束"不得声称已投放/已执行"。
- **防重复（幂等在工具层）**：`save_plan` 以 `topic:tier` 为幂等键，相同请求重复保存只产生一份方案并返回 `duplicate`；模型无法绕过。
- **失败如实记录**：步骤状态由 `_steps_from_history` 按实际观察生成，失败保留 `failed`、重试成功另起一条 `success` 记录——失败步骤在结构上无法被改写为成功。
- **故障注入**：`inject.script_fail_once` 由输入显式声明（注入一次失败、重试成功），用于确定性复现"失败续跑"加分项。
- **审计**：全部工具调用（含参数与返回）进入 Trace。
