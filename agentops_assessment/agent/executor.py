from __future__ import annotations

from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import TOOL_PERMISSIONS, ToolRegistry
from agentops_assessment.backend import database
from agentops_assessment.rag.security import detect_prompt_injection
from agentops_assessment.agent.fake_llm import FakeLLM


SENSITIVE_KEYS = {
    "vendor_secret", "unit_cost_usd",
    "debug", "candidate_note",
}


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        state_store: InMemoryRunStateStore | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store or InMemoryRunStateStore()

    def execute(
        self,
        run_id: str,
        plan: list[PlanStep],
        context: dict[str, Any],
        conn: Any = None,
    ) -> RunState:
        """执行计划并持久化步骤状态。
        TODO(candidate/P0): 实现可恢复的多步骤执行、工具入参渲染、
        步骤事件持久化、错误处理和最终业务结果汇总。
        """
        state = RunState(
            run_id=run_id,
            status="running",
            steps=[],
        )
        accumulated = dict(context)
        total_token_cost = 0
        last_error: str | None = None

        for step in plan:
            # Resolve args: replace None values from accumulated context
            args = self._resolve_args(step.input_template, accumulated)

            # Check tool-level permission before executing
            required_perm = TOOL_PERMISSIONS.get(step.tool_name)
            user_permissions = context.get("user_permissions", [])
            if required_perm and required_perm not in user_permissions:
                if conn:
                    database.insert_run_event(
                        conn, run_id, "tool.skipped",
                        payload={"reason": "缺少权限: " + required_perm},
                        tool_name=step.tool_name,
                    )
                # 跳过此步不视为失败，继续执行后续步骤
                continue

            # 提示词注入检测：工具执行路径中的 knowledge.search
            injection_warning = None
            if step.tool_name == "knowledge.search":
                query_text = args.get("query", "")
                injection_matches = detect_prompt_injection(query_text)
                if injection_matches:
                    injection_warning = True

            # Attempt tool call
            try:
                output = self.registry.call(step.tool_name, args)
                total_token_cost += self._estimate_tokens(step.tool_name, output)

                step_state = StepState(
                    step_id=step.id,
                    tool_name=step.tool_name,
                    status="completed",
                    output=output,
                )
                state.steps.append(step_state)

                # Merge output into accumulated context for downstream steps
                if isinstance(output, dict):
                    accumulated.update(output)

                # Persist tool.call event
                if conn:
                    payload = self._sanitize(output)
                    if injection_warning:
                        payload = dict(payload)
                        payload["injection_warning"] = True
                    database.insert_run_event(
                        conn, run_id, "tool.call",
                        payload=payload,
                        tool_name=step.tool_name,
                    )

                # 审计日志：OA 草稿创建
                if step.tool_name == "oa.create_approval_draft" and conn:
                    database.insert_audit_log(
                        conn,
                        actor_id=context.get("user_id", ""),
                        action="approval.draft.create",
                        resource=output.get("approval_draft_id", ""),
                        payload={"sku": args.get("sku", ""), "approval_type": args.get("approval_type", "")},
                    )

            except Exception as exc:
                step_state = StepState(
                    step_id=step.id,
                    tool_name=step.tool_name,
                    status="failed",
                    error=str(exc),
                )
                state.steps.append(step_state)
                last_error = str(exc)

                if conn:
                    database.insert_run_event(
                        conn, run_id, "tool.call",
                        payload={"error": str(exc)},
                        tool_name=step.tool_name,
                    )

                state.status = "failed"
                break

        if not last_error:
            state.status = "completed"
            state.result = self._build_result(accumulated, context)

        state.token_cost = total_token_cost
        self.state_store.save(state)
        return state



    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_args(
        template: dict[str, Any],
        accumulated: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill None-valued template keys from accumulated context."""
        args = {}
        for key, value in template.items():
            if value is None:
                args[key] = accumulated.get(key)
            else:
                args[key] = value
        return args

    @staticmethod
    def _build_result(
        accumulated: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}

        if accumulated.get("sku"):
            result["sku"] = accumulated["sku"]

        if "warehouse" in accumulated:
            result["warehouse"] = accumulated["warehouse"]

        result["stock_gap"] = accumulated.get("stock_gap", 0)
        result["forecast_units_next_14d"] = accumulated.get("forecast_units_next_14d", 0)

        # Build supplier_risk summary
        supplier_risk = {}
        if "supplier_id" in accumulated:
            supplier_risk["supplier_id"] = accumulated["supplier_id"]
        if "risk_level" in accumulated:
            supplier_risk["risk_level"] = accumulated["risk_level"]
        if supplier_risk:
            result["supplier_risk"] = supplier_risk

        result["citations"] = accumulated.get("citations", [])

        if "approval_draft_id" in accumulated:
            result["approval_draft_id"] = accumulated["approval_draft_id"]
            result["recommended_action"] = "create_replenishment_approval"
        else:
            result["recommended_action"] = "analysis_only"

        return result

    @staticmethod
    def _sanitize(output: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(output, dict):
            return output
        return {k: v for k, v in output.items() if k not in SENSITIVE_KEYS}

    @staticmethod
    def _estimate_tokens(tool_name: str, output: Any) -> int:
        fake = FakeLLM()
        result = fake.complete(str(output) if output is not None else "")
        return result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
