"""
Stage 5 - IRValidator
Structural and semantic validation of the ExecutablePlan before emission.
"""

from __future__ import annotations

from ..ir import ExecutablePlan, IssueLevel, PlanIssue, StepStatus


class IRValidator:
    def validate(self, plan: ExecutablePlan):
        step_ids = {s.step_id for s in plan.steps}

        for step in plan.steps:
            # Blocked steps → already have ERROR issues from resolver
            if step.status == StepStatus.BLOCKED:
                continue

            # Algorithm missing
            if step.algorithm is None:
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.ERROR,
                        code="NO_ALGORITHM",
                        message=f"Step '{step.step_id}' has no resolved algorithm.",
                        step_id=step.step_id,
                    )
                )
                continue

            # Check child_output references point to existing steps
            for pname, binding in step.parameters.items():
                if binding.source_type == "child_output":
                    if binding.child_id and binding.child_id not in step_ids:
                        plan.issues.append(
                            PlanIssue(
                                level=IssueLevel.ERROR,
                                code="DANGLING_REFERENCE",
                                message=(
                                    f"Step '{step.step_id}' param '{pname}' "
                                    f"references unknown step '{binding.child_id}'."
                                ),
                                step_id=step.step_id,
                                param_name=pname,
                            )
                        )

            # Warn on ASSUMED steps
            if step.status == StepStatus.ASSUMED:
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.WARNING,
                        code="ASSUMED_ALGORITHM",
                        message=(
                            f"Step '{step.step_id}' uses assumed algorithm "
                            f"'{step.algorithm.algorithm_id}' (conf={step.confidence:.2f}). "
                            f"Verify in Designer."
                        ),
                        step_id=step.step_id,
                    )
                )

        # Duplicate step IDs
        seen = set()
        for step in plan.steps:
            if step.step_id in seen:
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.ERROR,
                        code="DUPLICATE_STEP_ID",
                        message=f"Duplicate step_id: '{step.step_id}'.",
                    )
                )
            seen.add(step.step_id)

        # Circular dependency detection (backup for link_repair)
        adj: dict[str, list[str]] = {}
        for step in plan.steps:
            deps = [
                b.child_id
                for b in step.parameters.values()
                if b.source_type == "child_output" and b.child_id
            ]
            adj[step.step_id] = deps

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {s.step_id: WHITE for s in plan.steps}

        def _dfs(node: str, path: list[str]) -> None:
            color[node] = GRAY
            for dep in adj.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    cycle = path + [dep]
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.ERROR,
                            code="CIRCULAR_DEPENDENCY",
                            message=f"Circular dependency: {' → '.join(cycle)}.",
                            step_id=node,
                        )
                    )
                    return
                if color[dep] == WHITE:
                    _dfs(dep, path + [dep])
            color[node] = BLACK

        for step in plan.steps:
            if color[step.step_id] == WHITE:
                _dfs(step.step_id, [step.step_id])
