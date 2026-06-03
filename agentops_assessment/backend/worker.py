from __future__ import annotations

import os

from agentops_assessment.agent.executor import Executor
from agentops_assessment.agent.planner import Planner
from agentops_assessment.agent.tools import TOOL_PERMISSIONS, ToolRegistry
from agentops_assessment.backend import database


def execute_run(run_id: str) -> None:
    """后台执行入口。
    TODO(candidate/P0): 用完整的 Planner -> Executor 流程替换此占位实现。
    预期实现应更新 running/completed/failed 状态，持久化步骤事件，
    通过 ToolRegistry 调用工具，记录 token 成本，并保存最终业务结果。
    """
    with database.connect() as conn:
        database.init_db(conn)
        now = database.now_iso()

        # 1. Load run record
        run_row = conn.execute(
            "SELECT task_id, requested_by FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not run_row:
            return
        task_id = run_row["task_id"]
        user_id = run_row["requested_by"]

        # 2. Load task record
        task_row = conn.execute(
            "SELECT prompt, created_by FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task_row:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", "关联的任务不存在", database.now_iso(), run_id),
            )
            conn.commit()
            return
        prompt = task_row["prompt"]

        # 3. Load requesting user
        user_row = conn.execute(
            "SELECT id, permissions_json FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user_row:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", "请求用户不存在", database.now_iso(), run_id),
            )
            conn.commit()
            return
        permissions = database.decode_json(user_row["permissions_json"], [])

        # 4. Mark run as running
        conn.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", now, run_id),
        )
        database.insert_run_event(
            conn, run_id, "run.started", {"message": "Worker 开始执行 Agent 规划与工具调用流程。"},
        )
        conn.commit()

        # 5. Initialize components
        fixtures = os.getenv("ASSESSMENT_FIXTURES_DIR", "fixtures")
        planner = Planner()
        registry = ToolRegistry.with_default_clients(fixtures_dir=fixtures)
        executor = Executor(registry)

        # 6. Context shared across planner and executor
        context = {
            "user_permissions": permissions,
            "user_id": user_id,
        }

        try:
            # 7. Create plan
            plan = planner.create_plan(prompt, context)

            # 运行前校验工具级权限：若计划中有工具的权限不足，直接失败
            missing_tool_perms = []
            for step in plan:
                req_perm = TOOL_PERMISSIONS.get(step.tool_name)
                if req_perm and req_perm not in permissions:
                    missing_tool_perms.append((step.tool_name, req_perm))
            if missing_tool_perms:
                perm_detail = "; ".join(f"{tool}: {perm}" for tool, perm in missing_tool_perms)
                raise PermissionError(f"缺少工具权限: {perm_detail}")

            # 8. Execute plan
            state = executor.execute(run_id, plan, context, conn=conn)

            finish_now = database.now_iso()
            if state.status == "completed":
                conn.execute(
                    """UPDATE runs SET status = ?, result_json = ?, token_cost = ?,
                       finished_at = ? WHERE id = ?""",
                    (
                        "completed",
                        database.encode_json(state.result),
                        state.token_cost,
                        finish_now,
                        run_id,
                    ),
                )
                database.insert_run_event(
                    conn, run_id, "run.completed",
                    {"status": "completed"},
                )
            else:
                error_msg = (
                    state.steps[-1].error if state.steps else "Agent 执行失败"
                )
                conn.execute(
                    """UPDATE runs SET status = ?, error = ?, token_cost = ?,
                       finished_at = ? WHERE id = ?""",
                    ("failed", error_msg, state.token_cost, finish_now, run_id),
                )
                database.insert_run_event(
                    conn, run_id, "run.failed",
                    {"error": error_msg},
                )
            conn.commit()

        except (Exception, PermissionError) as exc:
            finish_now = database.now_iso()
            error_msg = str(exc)
            conn.execute(
                """UPDATE runs SET status = ?, error = ?, finished_at = ?
                   WHERE id = ?""",
                ("failed", error_msg, finish_now, run_id),
            )
            database.insert_run_event(
                conn, run_id, "run.failed",
                {"error": error_msg},
            )
            conn.commit()
