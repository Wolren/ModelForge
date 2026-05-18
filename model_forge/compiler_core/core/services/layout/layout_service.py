"""
GraphLayoutService - layout algorithms for models without external dependencies.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Set, Any
from collections import defaultdict, deque

from .layout_config import LayoutConfig


class GraphLayoutService:
    """
    Usage:
        svc = GraphLayoutService()
        svc.layout_plan(plan)                       # mutates pos_x/pos_y on plan
        new_json = svc.layout_model_json(old_json)  # returns updated dict copy
    """

    def __init__(self, config: Optional[LayoutConfig] = None):
        self.config = config or LayoutConfig.balanced()

    def layout_plan(self, plan, incremental: bool = False):
        """Assign pos_x / pos_y to all inputs and steps in ExecutablePlan."""
        cfg = self.config

        dep_map: Dict[str, List[str]] = defaultdict(list)
        input_ids: Set[str] = {inp.name for inp in plan.inputs}

        for step in plan.steps:
            for binding in step.parameters.values():
                if binding.source_type == "child_output" and binding.child_id:
                    dep_map[binding.child_id].append(step.step_id)

        step_ids = [s.step_id for s in plan.steps]
        ranks = self._assign_ranks(step_ids, dep_map)

        y_cursor = cfg.input_x
        for i, inp in enumerate(plan.inputs):
            inp.pos_x = cfg.input_x
            inp.pos_y = y_cursor
            y_cursor += self._plan_node_span(inp.label, cfg)

        ranks_by_level: Dict[int, List[str]] = defaultdict(list)
        for sid, rank in ranks.items():
            ranks_by_level[rank].append(sid)

        step_by_id = {s.step_id: s for s in plan.steps}
        for rank, sids in sorted(ranks_by_level.items()):
            x = cfg.start_x + rank * cfg.h_spacing
            y = cfg.input_x
            for sid in sorted(sids):
                step = step_by_id.get(sid)
                if step:
                    step.pos_x = x
                    step.pos_y = y
                    step.rank = rank
                    y += self._plan_node_span(step.label, cfg)

    def layout_model_json(
        self,
        model_json: Dict[str, Any],
        mode: str = "balanced",
        orientation: str = "horizontal",
        strategy: str = "sugiyama",
    ) -> Dict[str, Any]:
        """Assign coordinates to model_json dict, returns updated copy."""
        import copy

        result = copy.deepcopy(model_json or {})
        inputs = result.get("inputs", [])
        algorithms = result.get("algorithms", [])

        cfg = self._config_for_mode(mode)
        self.config = cfg

        if orientation == "vertical":
            self._apply_vertical_layout(result, cfg, strategy)
            return result
        
        if orientation == "axis":
            self._apply_axis_layout(result, cfg, strategy)
            return result

        ranks = self._assign_json_ranks(algorithms, strategy)
        self._apply_horizontal_layout(result, algorithms, ranks, cfg)
        return result

    def _config_for_mode(self, mode: str) -> LayoutConfig:
        mode = (mode or "balanced").lower()
        return {
            "compact": LayoutConfig.compact(),
            "balanced": LayoutConfig.balanced(),
            "dense": LayoutConfig.dense(),
            "spacious": LayoutConfig.spacious(),
            "debug": LayoutConfig.debug(),
        }.get(mode, LayoutConfig.balanced())

    def _apply_vertical_layout(
        self,
        result: Dict[str, Any],
        cfg: LayoutConfig,
        strategy: str,
    ):
        inputs = result.get("inputs", [])
        algorithms = result.get("algorithms", [])

        top_margin = self.config.input_x
        left_margin = self.config.input_x

        # Inputs: same y, x increments right
        x = left_margin
        for inp in inputs:
            inp["pos_x"] = x
            inp["pos_y"] = top_margin
            x += max(cfg.h_spacing * 0.9, 280.0)

        ranks_by_level: Dict[int, List] = defaultdict(list)
        ranks = self._assign_json_ranks(algorithms, strategy)
        for alg in algorithms:
            rank = ranks.get(alg.get("id", ""), 0)
            ranks_by_level[rank].append(alg)

        # Algorithms: y by rank, x increments within rank
        for rank, algs in sorted(ranks_by_level.items()):
            y = cfg.start_x + rank * cfg.h_spacing
            x = left_margin
            for alg in algs:
                alg["pos_x"] = x
                alg["pos_y"] = y
                x += max(cfg.h_spacing * 0.9, 280.0)

    def _apply_axis_layout(
        self,
        result: Dict[str, Any],
        cfg: LayoutConfig,
        strategy: str,
    ):
        inputs = result.get("inputs", [])
        algorithms = result.get("algorithms", [])

        top_margin = self.config.input_x
        left_margin = self.config.input_x

        # Inputs: stagger right and down
        y = top_margin
        for i, inp in enumerate(inputs):
            inp["pos_x"] = left_margin + (i * cfg.h_spacing * 0.35)
            inp["pos_y"] = y
            y += self._json_node_span(inp, cfg)

        ranks = self._assign_json_ranks(algorithms, strategy)
        
        rank_groups: Dict[int, List[Dict]] = defaultdict(list)
        for alg in algorithms:
            r = ranks.get(alg.get("id", ""), 0)
            rank_groups[r].append(alg)

        # Algorithms: x by rank, y staggered within rank
        for rank, algs in sorted(rank_groups.items()):
            y = top_margin + rank * (cfg.v_spacing * 0.5)
            for alg in algs:
                alg["pos_x"] = cfg.start_x + rank * cfg.h_spacing
                alg["pos_y"] = y
                y += self._json_node_span(alg, cfg)

    def _apply_horizontal_layout(
        self,
        result: Dict[str, Any],
        algorithms: List[Dict],
        ranks: Dict[str, int],
        cfg: LayoutConfig,
    ):
        inputs = result.get("inputs", [])
        top_margin = self.config.input_x

        # Inputs: fixed x, stagger down
        y = top_margin
        for inp in inputs:
            inp["pos_x"] = top_margin
            inp["pos_y"] = y
            y += self._json_node_span(inp, cfg)

        ranks_by_level: Dict[int, List[Dict]] = defaultdict(list)
        for alg in algorithms:
            rank = ranks.get(alg.get("id", ""), 0)
            ranks_by_level[rank].append(alg)

        # Algorithms: x by rank, y staggered within rank
        for rank, algs in sorted(ranks_by_level.items()):
            x = cfg.start_x + rank * cfg.h_spacing
            y = top_margin
            for alg in algs:
                alg["pos_x"] = x
                alg["pos_y"] = y
                y += self._json_node_span(alg, cfg)

    def _estimate_wrap_lines(self, text: str, wrap_width: int = 28) -> int:
        txt = str(text or "").strip()
        if not txt:
            return 1
        logical_lines = txt.splitlines() or [txt]
        lines = 0
        for line in logical_lines:
            line_len = max(1, len(line.strip()))
            wrapped = max(1, (line_len + wrap_width - 1) // wrap_width)
            lines += wrapped
        result = max(1, lines)
        print(f"[Layout] wrap: '{txt[:25]}...' -> {len(logical_lines)} lines -> {result} wrapped")
        return result

    def _json_node_span(self, entry: Dict[str, Any], cfg: LayoutConfig) -> float:
        text = entry.get("description") or entry.get("label") or entry.get("id") or entry.get("name") or ""
        lines = self._estimate_wrap_lines(text, wrap_width=30)
        extra = max(0, lines - 1) * 28.0
        span = cfg.v_spacing + extra
        # Debug: print node height calculation
        print(f"[Layout] id={entry.get('id','')[:20]} text={text[:30]!r} lines={lines} span={span:.0f}")
        return span

    def _plan_node_span(self, label: str, cfg: LayoutConfig) -> float:
        lines = self._estimate_wrap_lines(label, wrap_width=30)
        extra = max(0, lines - 1) * 28.0
        return cfg.v_spacing + extra

    def _assign_layout_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
        strategy: str = "sugiyama",
    ) -> Dict[str, int]:
        strategy = (strategy or "sugiyama").lower()
        if strategy == "topological":
            return self._assign_topological_levels(node_ids, dep_map)
        if strategy == "axis_pack":
            return self._assign_ranks(node_ids, dep_map)
        if strategy == "radial_shell":
            return self._assign_radial_shell_levels(node_ids, dep_map)
        if strategy == "ancestor_weighted":
            return self._assign_ancestor_weighted_levels(node_ids, dep_map)
        return self._assign_ranks(node_ids, dep_map)

    def _assign_topological_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        in_degree: Dict[str, int] = {nid: 0 for nid in node_ids}
        rev_adj: Dict[str, List[str]] = defaultdict(list)
        for src, targets in dep_map.items():
            for tgt in targets:
                if tgt in in_degree:
                    in_degree[tgt] += 1
                if src in in_degree:
                    rev_adj[src].append(tgt)

        queue: deque = deque()
        for nid in node_ids:
            if in_degree.get(nid, 0) == 0:
                queue.append(nid)

        order: List[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for child in rev_adj.get(nid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        for nid in node_ids:
            if nid not in order:
                order.append(nid)

        return {nid: i for i, nid in enumerate(order)}

    def _assign_radial_shell_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        levels = self._assign_topological_levels(node_ids, dep_map)
        max_level = max(levels.values()) if levels else 1
        for nid in node_ids:
            if nid not in levels:
                levels[nid] = max_level
        return levels

    def _assign_ancestor_weighted_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        ancestors = {nid: self.ancestors(nid, set()) for nid in node_ids}
        weights = {nid: len(a) for nid, a in ancestors.items()}
        sorted_nodes = sorted(node_ids, key=lambda n: weights.get(n, 0))
        return {nid: i for i, nid in enumerate(sorted_nodes)}

    def ancestors(self, nid: str, trail: Set[str]) -> Set[str]:
        if nid in trail:
            return trail
        trail = trail | {nid}
        return trail

    def _assign_ranks(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        in_degree = {nid: 0 for nid in node_ids}
        adj = defaultdict(list)

        for src, targets in dep_map.items():
            for tgt in targets:
                if tgt in in_degree:
                    in_degree[tgt] += 1
                if src in in_degree:
                    adj[src].append(tgt)

        queue = deque(nid for nid, d in in_degree.items() if d == 0)
        rank = {}
        current_rank = 0

        while queue:
            next_queue = deque()
            while queue:
                nid = queue.popleft()
                rank[nid] = current_rank
                for child in adj.get(nid, []):
                    if child in in_degree:
                        in_degree[child] -= 1
                        if in_degree[child] == 0:
                            next_queue.append(child)
            queue = next_queue
            current_rank += 1

        for nid in node_ids:
            if nid not in rank:
                rank[nid] = current_rank
        return rank

    def _assign_json_ranks(
        self,
        algorithms: List[Dict[str, Any]],
        strategy: str,
    ) -> Dict[str, int]:
        node_ids = [a.get("id", "") for a in algorithms if a.get("id")]
        dep_map: Dict[str, List[str]] = defaultdict(list)

        for alg in algorithms:
            alg_id = alg.get("id", "")
            params = alg.get("parameters", {})
            for pname, pbind in params.items():
                if isinstance(pbind, dict) and pbind.get("type") == "child_output":
                    src = pbind.get("child_id", "")
                    if src:
                        dep_map[src].append(alg_id)

        return self._assign_layout_levels(node_ids, dep_map, strategy)