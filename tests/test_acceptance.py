"""验收测试：题目二「内容任务规划 Agent」的必须验收点 + 红线。

运行：python -m unittest discover -s tests -v

覆盖：正常完成 / 缺预算 / 工具失败（含失败步骤续跑加分项）/ 预算超限（参考实例
12 -> 8 换档）/ 无可行方案返回缺口 / 重复执行幂等 / 步数上限 / CLI 边界。
红线（对应试卷审核要点）在 assert_red_lines 统一断言：
1. 已保存方案的费用合计必须 <= 预算且等于估算（预算计算核对）；
2. 失败步骤不得被记为成功（步骤状态按实际观察记录）；
3. 未完成必须给出原因与缺口；预算字段原样回显未被更改。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from src.agent import run_agent  # noqa: E402
from src.model_client import MockModelClient  # noqa: E402
from src.tools import ToolRegistry  # noqa: E402


def make_data_dir(tmp_dir: str) -> str:
    """构造隔离数据目录：合成数据复制，方案箱清空。"""
    data_dir = os.path.join(tmp_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    for name in ("materials.json", "cost_table.json"):
        shutil.copy(os.path.join(REPO_ROOT, "data", name), os.path.join(data_dir, name))
    with open(os.path.join(data_dir, "plans.json"), "w", encoding="utf-8") as f:
        json.dump({"plans": {}, "next_plan_id": 1}, f, ensure_ascii=False)
    return data_dir


def run_task(task: dict, data_dir: str, max_steps: int = 12):
    registry = ToolRegistry(data_dir, inject=task.get("inject") or {})
    return run_agent(task, MockModelClient(), registry, max_steps=max_steps)


def base_task(**kw) -> dict:
    task = {
        "topic": "校园咖啡节",
        "budget": 20,
        "deadline": "2026-11-05",
        "inject": {"script_fail_once": False},
    }
    task.update(kw)
    return task


def assert_red_lines(test: unittest.TestCase, result: dict, task: dict) -> None:
    # 红线 1：已保存方案的费用合计必须 <= 预算且等于估算（不得虚报在预算内）
    if result.get("saved_plan_id"):
        test.assertTrue(result["cost"]["within_budget"])
        test.assertLessEqual(result["cost"]["estimated_total"], task["budget"])
    # 红线 2：步骤状态只能是 success/failed，失败步骤保留 failed 记录（不被改写）
    for step in result["plan"]["steps"]:
        test.assertIn(step["status"], ("success", "failed"))
    # 红线 3：未完成必须给出原因与缺口
    if result["final_status"] in ("incomplete", "missing_input"):
        test.assertTrue(result.get("incomplete_reason"))
    # 预算字段原样回显，未被更改
    test.assertEqual(result["input_summary"]["budget"], task.get("budget"))


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = make_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _plans(self) -> dict:
        with open(os.path.join(self.data_dir, "plans.json"), encoding="utf-8") as f:
            return json.load(f)["plans"]

    def test_1_normal_complete(self):
        """验收点：正常完成（预算 20 足够 standard 档 12）。"""
        task = base_task()
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task)
        self.assertEqual(result["final_status"], "completed")
        self.assertEqual(result["cost"]["estimated_total"], 12)
        self.assertTrue(result["cost"]["within_budget"])
        self.assertIsNotNone(result["saved_plan_id"])
        steps = result["plan"]["steps"]
        self.assertEqual(
            [s["tool"] for s in steps],
            ["search_materials", "generate_script", "estimate_cost", "save_plan"],
        )
        self.assertTrue(all(s["status"] == "success" for s in steps))
        plans = self._plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans["校园咖啡节:standard"]["estimated_total"], 12)

    def test_2_budget_overrun_replan(self):
        """验收点：预算超限（参考实例：预算 10，standard 估算 12 超限，改选 lite 8）。"""
        task = base_task(budget=10)
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task)
        self.assertEqual(result["final_status"], "completed")
        # 工具序列体现重规划：standard 估算 -> 换 lite 再生成、再估算 -> 保存
        steps = result["plan"]["steps"]
        self.assertEqual(
            [s["tool"] for s in steps],
            [
                "search_materials",
                "generate_script",
                "estimate_cost",
                "generate_script",
                "estimate_cost",
                "save_plan",
            ],
        )
        tiers = [s["tier"] for s in steps if s["tool"] == "generate_script"]
        self.assertEqual(tiers, ["standard", "lite"])
        # 第一次估算 12 超预算 10 的观察真实存在
        trace_dict = trace.to_dict()
        est_obs = next(
            s["observation"] for s in trace_dict["steps"] if s["tool"] == "estimate_cost"
        )
        self.assertEqual(est_obs["total"], 12)
        replan_step = next(
            s for s in trace_dict["steps"]
            if s["tool"] == "generate_script" and s["step"] > 3
        )
        self.assertIn("超出预算", replan_step["decision_basis"])
        # 最终保存 lite 档 8，费用合计 <= 预算
        self.assertEqual(result["cost"]["estimated_total"], 8)
        self.assertEqual(self._plans()["校园咖啡节:lite"]["estimated_total"], 8)

    def test_3_no_feasible_plan_gap(self):
        """参考实例：没有可行方案时返回未完成及缺口，不保存超预算方案。"""
        task = base_task(budget=5)
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task)
        self.assertEqual(result["final_status"], "incomplete")
        self.assertIsNone(result["saved_plan_id"])
        self.assertIn("缺口", result["incomplete_reason"])
        self.assertIn("3", result["incomplete_reason"])  # lite 8 - 预算 5 = 缺口 3
        self.assertFalse(result["cost"]["within_budget"])
        self.assertEqual(self._plans(), {})

    def test_4_missing_budget(self):
        """验收点：缺预算 -> 未完成并给出缺口，不编造、不调用工具。"""
        task = base_task()
        del task["budget"]
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task)
        self.assertEqual(result["final_status"], "missing_input")
        self.assertIn("预算", result["incomplete_reason"])
        self.assertEqual(result["plan"]["steps"], [])
        self.assertEqual([s["tool"] for s in trace.to_dict()["steps"]], [])
        self.assertEqual(self._plans(), {})

    def test_5_tool_failure_retry_keeps_success(self):
        """验收点+加分项：注入一次脚本生成失败 -> 重试成功；失败步骤保留 failed 记录，
        已成功步骤结果保留（失败步骤续跑）。"""
        task = base_task(inject={"script_fail_once": True})
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task)
        self.assertEqual(result["final_status"], "completed")
        steps = result["plan"]["steps"]
        self.assertEqual(
            [(s["tool"], s["status"]) for s in steps],
            [
                ("search_materials", "success"),
                ("generate_script", "failed"),
                ("generate_script", "success"),
                ("estimate_cost", "success"),
                ("save_plan", "success"),
            ],
        )
        self.assertEqual(result["cost"]["estimated_total"], 12)
        self.assertEqual(len(self._plans()), 1)

    def test_6_duplicate_execution(self):
        """验收点：重复执行 -> 相同请求只产生一份方案，第二次返回 duplicate。"""
        task = base_task()
        first, _ = run_task(task, self.data_dir)
        second, _ = run_task(task, self.data_dir)
        assert_red_lines(self, second, task)
        self.assertEqual(first["final_status"], "completed")
        self.assertEqual(second["final_status"], "duplicate")
        self.assertEqual(second["saved_plan_id"], first["saved_plan_id"])
        self.assertEqual(len(self._plans()), 1)

    def test_7_max_steps_incomplete(self):
        """边界：达到步数上限时返回明确的未完成状态。"""
        task = base_task()
        result, _ = run_task(task, self.data_dir, max_steps=2)
        self.assertEqual(result["final_status"], "incomplete")
        self.assertIn("上限", result["message"])
        self.assertEqual(result["steps_used"], 2)


class CliTests(unittest.TestCase):
    """CLI 运行方式与输入边界。"""

    def test_cli_normal_exit_zero_and_trace(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            data_dir = make_data_dir(tmp.name)
            trace_out = os.path.join(tmp.name, "out.trace.json")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--input",
                    os.path.join(REPO_ROOT, "examples", "normal.input.json"),
                    "--data-dir",
                    data_dir,
                    "--trace-out",
                    trace_out,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["final_status"], "completed")
            with open(trace_out, encoding="utf-8") as f:
                trace = json.load(f)
            self.assertTrue(trace["steps"])
        finally:
            tmp.cleanup()

    def test_cli_invalid_input_nonzero(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            bad_json = os.path.join(tmp.name, "bad.json")
            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("{not json")
            proc = subprocess.run(
                [sys.executable, "-m", "src.main", "--input", bad_json],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
