"""业务工具层（第一板斧：外部能力入口与边界）。

设计边界：
- Tool 只执行原子动作，不承载完整业务流程；流程编排由 agent.py 的 ReAct 循环完成。
- search_materials / generate_script / estimate_cost 为只读工具；
- save_plan 是唯一写操作，只写方案文件——系统中不存在真实投放/发布工具。
- estimate_cost 只返回费用事实（total/breakdown），**不判断是否超预算**；
  "估算值 vs 预算"的对比与换档决策由模型完成，保证重规划是真实的动态决策。
- 幂等（防重复方案）与单次故障注入都在工具层实现（模型外部），模型不能绕过。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

DATA_FILES = {
    "materials": "materials.json",
    "cost_table": "cost_table.json",
    "plans": "plans.json",
}

# 工具清单（模型可见的能力入口；同时用于生成 API 模式的 tool schemas）
TOOL_SCHEMAS = [
    {
        "name": "search_materials",
        "description": "按主题检索合成素材库，返回可用素材与方案档位（standard/lite）（只读）",
        "params": {"topic": "string，必填"},
    },
    {
        "name": "generate_script",
        "description": "基于检索到的素材生成指定档位（standard/lite）的内容脚本大纲（只读）",
        "params": {"topic": "string，必填", "tier": "string：standard / lite，必填"},
    },
    {
        "name": "estimate_cost",
        "description": "按工具费用与耗时表估算指定档位方案的总成本，返回 total 与 breakdown（只读；不判断预算）",
        "params": {"tier": "string：standard / lite，必填"},
    },
    {
        "name": "save_plan",
        "description": (
            "保存内容活动方案（唯一写操作）。幂等：相同 topic+tier 只产生一份方案，"
            "重复保存返回 duplicate。仅保存方案，不执行真实投放。"
        ),
        "params": {
            "topic": "string，必填",
            "tier": "string：standard / lite，必填",
            "estimated_total": "number，必填，估算成本合计",
            "script_title": "string，必填，脚本标题",
        },
    },
]


def _read(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


class ToolRegistry:
    """工具注册表：模型通过名称调用；执行、参数校验、幂等与审计均在本层完成。"""

    def __init__(self, data_dir: str, inject: dict | None = None) -> None:
        self.data_dir = data_dir
        self.inject = inject or {}
        self._script_calls = 0  # 用于实现"注入一次失败"

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, DATA_FILES[name])

    # ------------------------------------------------------------------
    # 工具实现（每个工具返回一个 JSON 观察结果，异常不外抛）
    # ------------------------------------------------------------------

    def search_materials(self, topic: str) -> dict:
        topics = _read(self._path("materials")).get("topics", {})
        if topic not in topics:
            return {
                "status": "topic_not_found",
                "topic": topic,
                "message": f"主题 {topic} 在素材库中无合成素材",
            }
        return {
            "status": "ok",
            "topic": topic,
            "tiers_available": sorted(topics[topic].keys()),
            "materials": topics[topic],
        }

    def generate_script(self, topic: str, tier: str) -> dict:
        # 注入一次失败（工具失败验收场景）：首次调用失败，重试成功，
        # 用于考察"失败步骤续跑且保留已成功步骤"
        if self.inject.get("script_fail_once") and self._script_calls == 0:
            self._script_calls += 1
            return {
                "status": "failed",
                "topic": topic,
                "tier": tier,
                "error": "transient",
                "message": "脚本生成失败：生成服务临时不可用（注入的单次故障）",
            }
        self._script_calls += 1
        topics = _read(self._path("materials")).get("topics", {})
        tier_data = topics.get(topic, {}).get(tier)
        if tier_data is None:
            return {
                "status": "invalid_args",
                "topic": topic,
                "tier": tier,
                "message": f"主题 {topic} 不存在档位 {tier} 的素材",
            }
        script = {
            "title": f"《{topic}》{('标准' if tier == 'standard' else '精简')}档活动脚本",
            "hook": f"用 15 秒点出{topic}的核心看点",
            "sections": tier_data["content_forms"],
            "materials_used": tier_data["items"],
        }
        return {"status": "ok", "topic": topic, "tier": tier, "script": script}

    def estimate_cost(self, tier: str) -> dict:
        costs = _read(self._path("cost_table")).get("plan_costs", {})
        if tier not in costs:
            return {
                "status": "unknown_tier",
                "tier": tier,
                "message": f"费用表中不存在档位 {tier}",
            }
        breakdown = costs[tier]
        return {
            "status": "ok",
            "tier": tier,
            "currency": "模拟单位",
            "total": sum(breakdown.values()),
            "breakdown": breakdown,
        }

    def save_plan(
        self, topic: str, tier: str, estimated_total: float, script_title: str
    ) -> dict:
        path = self._path("plans")
        doc = _read(path)
        plan_key = f"{topic}:{tier}"
        existing = doc.get("plans", {}).get(plan_key)
        if existing:
            # 幂等：相同请求重复保存只产生一份方案
            return {
                "status": "duplicate",
                "plan_id": existing["plan_id"],
                "plan_key": plan_key,
                "plan": existing,
                "message": "相同请求已存在方案，未重复创建",
            }
        record = {
            "plan_id": f"p-{doc.get('next_plan_id', 1)}",
            "plan_key": plan_key,
            "topic": topic,
            "tier": tier,
            "estimated_total": estimated_total,
            "script_title": script_title,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        doc.setdefault("plans", {})[plan_key] = record
        doc["next_plan_id"] = doc.get("next_plan_id", 1) + 1
        _write(path, doc)
        return {
            "status": "saved",
            "plan_id": record["plan_id"],
            "plan_key": plan_key,
            "plan": record,
        }

    # ------------------------------------------------------------------
    # 分发与审计入口
    # ------------------------------------------------------------------

    def execute(self, name: str, args: dict) -> dict:
        """按名称执行工具。任何异常都转为观察结果交回模型（错误恢复），不终止循环。"""
        try:
            if name == "search_materials":
                return self.search_materials(args["topic"])
            if name == "generate_script":
                return self.generate_script(args["topic"], args["tier"])
            if name == "estimate_cost":
                return self.estimate_cost(args["tier"])
            if name == "save_plan":
                return self.save_plan(
                    args["topic"], args["tier"], args["estimated_total"], args["script_title"]
                )
            return {"status": "unknown_tool", "message": f"未知工具：{name}"}
        except KeyError as e:
            return {"status": "invalid_args", "message": f"工具 {name} 缺少必填参数: {e}"}
        except Exception as e:  # noqa: BLE001 - 观察结果化是 Loop 错误恢复的一部分
            return {"status": "tool_error", "message": f"工具 {name} 执行异常: {e}"}
