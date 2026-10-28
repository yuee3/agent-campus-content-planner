"""模型接入层：离线确定性 Mock 与 OpenAI 兼容 API 双入口。

边界声明（试卷要求标明）：
- Mock 模型以"任务 + 历史观察"为输入的策略函数模拟模型决策，只验证 ReAct
  流程与工具协议，不证明真实模型效果；它不是按场景硬编码的脚本——超预算换档、
  失败续跑、缺口计算都由同一策略根据观察结果推导出来。
- API 模式走 OpenAI 兼容 /chat/completions（标准库 urllib 实现），与 Mock
  共用同一 ReAct 循环。本仓库交付时未配置真实 Key，该路径未经过真实模型验证。
"""
from __future__ import annotations

import json
import os
import urllib.request

from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS


def _last(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["observation"]
    return None


def _last_action(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["action"]
    return None


def _count(history: list, tool: str) -> int:
    return sum(1 for h in history if h["action"].get("tool") == tool)


def _steps_from_history(history: list) -> list:
    """步骤状态按实际观察记录：成功即 success，失败即 failed，重试后成功保留两条记录。

    成功状态：查询类工具返回 ok；save_plan 返回 saved / duplicate。
    """
    success_statuses = {"ok", "saved", "duplicate"}
    steps = []
    for h in history:
        obs = h["observation"]
        steps.append(
            {
                "tool": h["action"].get("tool"),
                "tier": obs.get("tier"),
                "status": "success" if obs.get("status") in success_statuses else "failed",
            }
        )
    return steps


class MockModelClient:
    """离线确定性 Mock 模型（AGENT_MODEL=mock，默认）。"""

    def decide(self, task: dict, history: list) -> dict:
        topic = task.get("topic")
        raw_budget = task.get("budget")
        deadline = task.get("deadline")

        # 1) 信息缺失检查（验收点：缺预算或截止时间）——缺口由模型给出，不编造
        try:
            budget = float(raw_budget)
        except (TypeError, ValueError):
            budget = None
        missing = []
        if not topic:
            missing.append("主题 topic")
        if budget is None:
            missing.append("预算 budget（数字）")
        if not deadline:
            missing.append("截止时间 deadline")
        if missing:
            return {
                "type": "finish",
                "payload": {
                    "final_status": "missing_input",
                    "message": f"缺少必填信息：{'、'.join(missing)}，无法开始规划",
                    "plan": {"steps": _steps_from_history(history)},
                    "cost": None,
                    "saved_plan_id": None,
                    "incomplete_reason": f"缺口：{'、'.join(missing)}",
                },
                "basis": "任务缺少规划所需的关键信息，直接返回未完成与缺口",
            }

        # 2) 已有保存结果：依据观察如实收尾
        save = _last(history, "save_plan")
        if save is not None:
            est = _last(history, "estimate_cost")
            cost = self._cost_block(task, budget, est)
            if save.get("status") == "saved":
                return {
                    "type": "finish",
                    "payload": {
                        "final_status": "completed",
                        "message": "方案已保存（仅保存方案，未执行真实投放）",
                        "plan": {"steps": _steps_from_history(history)},
                        "cost": cost,
                        "saved_plan_id": save["plan_id"],
                        "incomplete_reason": None,
                    },
                    "basis": "方案保存成功，依据观察结束任务",
                }
            if save.get("status") == "duplicate":
                return {
                    "type": "finish",
                    "payload": {
                        "final_status": "duplicate",
                        "message": save.get("message", "相同请求已存在方案"),
                        "plan": {"steps": _steps_from_history(history)},
                        "cost": cost,
                        "saved_plan_id": save["plan_id"],
                        "incomplete_reason": None,
                    },
                    "basis": "观察到 duplicate：相同请求重复保存只产生一份方案",
                }
            return {
                "type": "finish",
                "payload": {
                    "final_status": "save_failed",
                    "message": save.get("message", "方案保存失败"),
                    "plan": {"steps": _steps_from_history(history)},
                    "cost": cost,
                    "saved_plan_id": None,
                    "incomplete_reason": save.get("message", "方案保存失败"),
                },
                "basis": "保存失败，如实上报",
            }

        # 3) 工具失败处理：重试一次（失败步骤续跑），仍失败则终止并保留已成功步骤
        for tool in ("search_materials", "generate_script", "estimate_cost"):
            last = _last(history, tool)
            if last is not None and last.get("status") != "ok":
                if tool == "search_materials" and last.get("status") == "topic_not_found":
                    return {
                        "type": "finish",
                        "payload": {
                            "final_status": "incomplete",
                            "message": last.get("message", "主题无素材"),
                            "plan": {"steps": _steps_from_history(history)},
                            "cost": None,
                            "saved_plan_id": None,
                            "incomplete_reason": f"主题 {topic} 在素材库中无合成素材，无法规划",
                        },
                        "basis": "检索不到素材，重试无意义，如实终止",
                    }
                if _count(history, tool) < 2:
                    prev = _last_action(history, tool)
                    return {
                        "type": "tool_call",
                        "tool": tool,
                        "args": prev.get("args") or {},
                        "basis": f"{tool} 上次返回失败（{last.get('message')}），"
                        "按失败处理策略重试一次，保留已成功步骤",
                    }
                return {
                    "type": "finish",
                    "payload": {
                        "final_status": "incomplete",
                        "message": f"{tool} 重试后仍失败，任务终止",
                        "plan": {"steps": _steps_from_history(history)},
                        "cost": None,
                        "saved_plan_id": None,
                        "incomplete_reason": f"{tool} 连续两次失败；已成功步骤的结果已保留，"
                        "失败步骤未被记为成功",
                    },
                    "basis": "重试仍失败，按边界终止并如实报告",
                }

        # 4) 正常链路：检索 -> 脚本（初始 standard 档） -> 估算 -> 预算判断
        search = _last(history, "search_materials")
        if search is None:
            return {
                "type": "tool_call",
                "tool": "search_materials",
                "args": {"topic": topic},
                "basis": f"尚未检索主题 {topic} 的素材与可用档位",
            }

        script = _last(history, "generate_script")
        if script is None:
            return {
                "type": "tool_call",
                "tool": "generate_script",
                "args": {"topic": topic, "tier": "standard"},
                "basis": "先用初始方案档位 standard 生成脚本大纲",
            }

        est = _last(history, "estimate_cost")
        if est is None or est.get("tier") != script.get("tier"):
            return {
                "type": "tool_call",
                "tool": "estimate_cost",
                "args": {"tier": script.get("tier")},
                "basis": "按当前脚本的档位调用费用表估算成本（费用事实由工具给出）",
            }

        # 5) 预算判断（模型决策，不是工具）：超预算 -> 改选替代档位或返回缺口
        if est.get("total") > budget:
            if est.get("tier") == "standard":
                return {
                    "type": "tool_call",
                    "tool": "generate_script",
                    "args": {"topic": topic, "tier": "lite"},
                    "basis": f"观察到 standard 档估算 {est.get('total')} 超出预算 {budget:g}，"
                    "改选 lite 档重新生成脚本并再估算",
                }
            return {
                "type": "finish",
                "payload": {
                    "final_status": "incomplete",
                    "message": "没有任何可行档位，返回未完成及缺口",
                    "plan": {"steps": _steps_from_history(history)},
                    "cost": self._cost_block(task, budget, est),
                    "saved_plan_id": None,
                    "incomplete_reason": f"无可行方案：最低档位 {est.get('tier')} 估算 "
                    f"{est.get('total')} 模拟单位，超出预算 {budget:g}，缺口 "
                    f"{est.get('total') - budget:g} 模拟单位；不保存超预算方案",
                },
                "basis": "替代档位仍超预算，按边界停止并给出缺口",
            }

        # 6) 预算内：保存方案
        script_title = (script.get("script") or {}).get("title", f"{topic} 活动脚本")
        return {
            "type": "tool_call",
            "tool": "save_plan",
            "args": {
                "topic": topic,
                "tier": est.get("tier"),
                "estimated_total": est.get("total"),
                "script_title": script_title,
            },
            "basis": f"{est.get('tier')} 档估算 {est.get('total')} 在预算 {budget:g} 内，"
            "保存该方案",
        }

    @staticmethod
    def _cost_block(task: dict, budget: float, est) -> dict | None:
        if est is None or est.get("status") != "ok":
            return None
        return {
            "budget": task.get("budget"),
            "estimated_total": est.get("total"),
            "breakdown": est.get("breakdown"),
            "within_budget": est.get("total", 0) <= budget,
        }


class ApiModelClient:
    """OpenAI 兼容 /chat/completions 客户端（标准库实现；未随本仓库验证）。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def decide(self, task: dict, history: list) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"task": task, "tool_schemas": TOOL_SCHEMAS}, ensure_ascii=False
                ),
            },
        ]
        for i, h in enumerate(history):
            a = h["action"]
            messages.append(
                {
                    "role": "assistant",
                    "content": a.get("basis", ""),
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": a["tool"],
                                "arguments": json.dumps(
                                    a.get("args") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": json.dumps(h["observation"], ensure_ascii=False),
                }
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s["description"],
                        "parameters": {"type": "object", "properties": s["params"]},
                    },
                }
                for s in TOOL_SCHEMAS
            ],
            "tool_choice": "auto",
        }
        resp = self._chat(payload)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if calls:
            fn = calls[0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            return {
                "type": "tool_call",
                "tool": fn["name"],
                "args": args,
                "basis": msg.get("content") or "模型发起工具调用",
            }
        content = msg.get("content") or ""
        try:
            payload_out = json.loads(content)
            if not isinstance(payload_out, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            payload_out = {"final_status": "incomplete", "message": content or "模型未返回可解析结果"}
        return {"type": "finish", "payload": payload_out, "basis": "模型直接给出最终结论"}


def build_client():
    """按 AGENT_MODEL 环境变量构建模型客户端，默认 mock。"""
    mode = os.environ.get("AGENT_MODEL", "mock").strip().lower()
    if mode == "mock":
        return MockModelClient()
    if mode == "api":
        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL")
        if not (base and key and model):
            raise RuntimeError(
                "AGENT_MODEL=api 需要设置 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL"
                "（参见 .env.example）"
            )
        return ApiModelClient(base, key, model)
    raise ValueError(f"未知 AGENT_MODEL: {mode}（可选 mock / api）")
