from __future__ import annotations

import sqlite3
from collections import Counter

from agentops_assessment.backend import database


def build_dashboard(conn: sqlite3.Connection) -> dict:
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    failed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'failed'"
    ).fetchone()["c"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed'"
    ).fetchone()["c"]
    token_cost = conn.execute("SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs").fetchone()[
        "c"
    ]
    events = conn.execute("SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL").fetchall()
    tool_counts = Counter(row["tool_name"] for row in events)

    # TODO(candidate/P2): 补充平均耗时、最近失败、按工具拆分的成本和队列健康度。

    # 平均耗时（已结束的 run，finished_at - started_at）
    avg_row = conn.execute(
        "SELECT AVG((julianday(finished_at) - julianday(started_at)) * 86400) AS avg_seconds "
        "FROM runs WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
    ).fetchone()
    average_run_seconds = round(avg_row["avg_seconds"], 2) if avg_row["avg_seconds"] is not None else 0

    # 最近失败（最近 5 条）
    recent_rows = conn.execute(
        """SELECT r.id AS run_id, r.task_id, r.error, r.created_at
           FROM runs r
           WHERE r.status = 'failed' AND r.error IS NOT NULL
           ORDER BY r.created_at DESC
           LIMIT 5"""
    ).fetchall()
    recent_failures = [
        {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "error": row["error"],
            "created_at": row["created_at"],
        }
        for row in recent_rows
    ]

    # 队列健康度：待处理 run 数量
    pending_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status IN ('queued', 'running')"
    ).fetchone()["c"]

    # 权限拒绝线索
    permission_denied_count = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE decision = 'deny'"
    ).fetchone()["c"]

    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": dict(tool_counts),
        "average_run_seconds": average_run_seconds,
        "recent_failures": recent_failures,
        "pending_count": pending_count,
        "permission_denied_count": permission_denied_count,
        "generated_at": database.now_iso(),
    }

