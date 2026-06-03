from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。
        TODO(candidate/P0): 推断 SKU 和业务意图，选择必要工具，并返回一个
        确定性的计划。计划应覆盖 ERP、BI、知识库、必要的供应商风险
        和可能的 OA 审批步骤，不能写死单个用户、SKU 或样例 prompt。
        """
        ctx = context or {}
        sku = self._extract_sku(prompt)
        intent = self._detect_intent(prompt)
        user_permissions = ctx.get("user_permissions", [])

        # Record LLM usage for token tracking
        llm_result = self.llm.complete(prompt)
        self._last_prompt_tokens = llm_result.get("prompt_tokens", 0)
        self._last_completion_tokens = llm_result.get("completion_tokens", 0)

        plan: list[PlanStep] = []
        seq = 0

        # Step 1: ERP inventory
        seq += 1
        plan.append(PlanStep(
            id=f"step-{seq}",
            tool_name="erp.get_inventory",
            description=f"获取 SKU {sku} 的库存数据",
            input_template={"sku": sku} if sku else {},
        ))

        # Step 2: BI sales & forecast
        seq += 1
        plan.append(PlanStep(
            id=f"step-{seq}",
            tool_name="bi.get_sales",
            description=f"获取 SKU {sku} 的销售和预测数据",
            input_template={"sku": sku} if sku else {},
        ))

        # Step 3: Knowledge base search
        seq += 1
        plan.append(PlanStep(
            id=f"step-{seq}",
            tool_name="knowledge.search",
            description="查询知识库中的库存处理规则",
            input_template={
                "query": prompt[:500],
                "user_permissions": user_permissions,
                "top_k": 3,
            },
        ))

        # Step 4: Supplier risk
        seq += 1
        plan.append(PlanStep(
            id=f"step-{seq}",
            tool_name="supplier.get_risk",
            description="查询供应商风险",
            # supplier_id will be resolved from accumulated ERP output by executor
            input_template={"supplier_id": None},
        ))

        # Step 5: OA approval draft (only when intent is replenishment and user has permission)
        has_oa_permission = "oa:approval:write" in user_permissions
        if intent == "replenishment" and has_oa_permission:
            seq += 1
            plan.append(PlanStep(
                id=f"step-{seq}",
                tool_name="oa.create_approval_draft",
                description="创建补货审批草稿",
                input_template={
                    "sku": sku,
                    "approval_type": "inventory_replenishment",
                    "user_id": ctx.get("user_id"),
                    # Resolved from accumulated outputs:
                    "stock_gap": None,
                    "forecast_units_next_14d": None,
                },
            ))

        return plan

    @staticmethod
    def _extract_sku(prompt: str) -> str | None:
        match = re.search(r'SKU-\d+', prompt)
        return match.group(0) if match else None

    @staticmethod
    def _detect_intent(prompt: str) -> str:
        """Detect whether the prompt is analysis-only or a replenishment request.
        Checks analysis-only patterns first (explicit opt-out), then replenishment patterns.
        Defaults to analysis_only when neither matches.
        """
        analysis_only = [
            r"只分析", r"仅分析", r"分析结论",
            r"不创建", r"不生成", r"不提交",
            r"analysis\s+only",
        ]
        for p in analysis_only:
            if re.search(p, prompt):
                return "analysis_only"

        replenishment = [
            r"补货.*审批", r"创建.*审批", r"生成.*审批",
            r"创建.*草稿", r"生成.*草稿",
            r"replenishment", r"approval",
        ]
        for p in replenishment:
            if re.search(p, prompt):
                return "replenishment"

        return "analysis_only"
