"""
GraphLayoutService
===================
Deterministic Sugiyama-style DAG layout for ExecutablePlan and model JSON dicts.

Three modes:
  compact   - minimise canvas area
  balanced  - optimise readability (default)
  dense     - tight layout for many nodes
  spacious  - extra whitespace for complex graphs
  debug     - maximise whitespace to expose edge routing

The layout is purely coordinate-assignment; no external graph library needed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict, deque


# ─── Config ──────────────────────────────────────────────────────────────────

@dataclass
class LayoutConfig:
    h_spacing: float  = 300.0   # horizontal gap between ranks (px)
    v_spacing: float  = 160.0   # vertical gap between nodes in same rank
    input_x:   float  = 20.0    # x position of model inputs column
    start_x:   float  = 340.0   # x origin of first algorithm rank

    @classmethod
    def compact(cls)  -> "LayoutConfig":
        return cls(h_spacing=240.0, v_spacing=120.0)

    @classmethod
    def balanced(cls) -> "LayoutConfig":
        return cls(h_spacing=300.0, v_spacing=160.0)

    @classmethod
    def dense(cls)    -> "LayoutConfig":
        return cls(h_spacing=210.0, v_spacing=95.0)

    @classmethod
    def spacious(cls) -> "LayoutConfig":
        return cls(h_spacing=520.0, v_spacing=280.0)

    @classmethod
    def debug(cls)    -> "LayoutConfig":
        return cls(h_spacing=420.0, v_spacing=220.0)


# ─── Service ─────────────────────────────────────────────────────────────────

class GraphLayoutService:
    """
    Usage:
        svc = GraphLayoutService()
        svc.layout_plan(plan)                       # mutates pos_x/pos_y on plan
        new_json = svc.layout_model_json(old_json)  # returns updated dict copy
    """

    def __init__(self, config: Optional[LayoutConfig] = None):
        self.config = config or LayoutConfig.balanced()

    # ── Public entry points ───────────────────────────────────────────────

    def layout_plan(self, plan, incremental: bool = False):
        """Assign pos_x / pos_y to all inputs and steps in ExecutablePlan."""
        cfg = self.config

        # Build adjacency: child_id -> list of step_ids that depend on it
        dep_map: Dict[str, List[str]] = defaultdict(list)
        input_ids: Set[str] = {inp.name for inp in plan.inputs}

        for step in plan.steps:
            for binding in step.parameters.values():
                if binding.source_type == "child_output" and binding.child_id:
                    dep_map[binding.child_id].append(step.step_id)

        step_ids = [s.step_id for s in plan.steps]
        ranks = self._assign_ranks(step_ids, dep_map)

        # Position inputs
        for i, inp in enumerate(plan.inputs):
            inp.pos_x = cfg.input_x
            inp.pos_y = 20.0 + i * cfg.v_spacing

        # Position steps by rank
        ranks_by_level: Dict[int, List[str]] = defaultdict(list)
        for sid, rank in ranks.items():
            ranks_by_level[rank].append(sid)

        step_by_id = {s.step_id: s for s in plan.steps}
        for rank, sids in sorted(ranks_by_level.items()):
            x = cfg.start_x + rank * cfg.h_spacing
            for j, sid in enumerate(sorted(sids)):
                step = step_by_id.get(sid)
                if step:
                    step.pos_x = x
                    step.pos_y = 20.0 + j * cfg.v_spacing
                    step.rank  = rank

    def layout_model_json(
        self,
        model_json: Dict[str, Any],
        mode: str = "balanced",
        orientation: str = "horizontal",
        strategy: str = "sugiyama",
    ) -> Dict[str, Any]:
        """
        Apply layout to a model JSON dict (the format used by ModelBuilder).
        Returns a new dict with pos_x / pos_y set on inputs and algorithms.
        """
        import copy
        result = copy.deepcopy(model_json)

        cfg_map = {
            "compact":  LayoutConfig.compact(),
            "balanced": LayoutConfig.balanced(),
            "dense":    LayoutConfig.dense(),
            "spacious": LayoutConfig.spacious(),
            "debug":    LayoutConfig.debug(),
        }
        cfg = cfg_map.get(mode, LayoutConfig.balanced())

        algorithms = result.get("algorithms", [])
        inputs     = result.get("inputs", [])

        # Build dependency map
        dep_map: Dict[str, List[str]] = defaultdict(list)
        for alg in algorithms:
            alg_id = alg.get("id", "")
            for pval in alg.get("parameters", {}).values():
                if isinstance(pval, dict) and pval.get("type") == "child_output":
                    child = pval.get("child_id")
                    if child:
                        dep_map[child].append(alg_id)

        alg_ids = [a.get("id", "") for a in algorithms]
        ranks = self._assign_layout_levels(alg_ids, dep_map, strategy=strategy)

        self._apply_orientation_layout(
            inputs=inputs,
            algorithms=algorithms,
            ranks=ranks,
            cfg=cfg,
            orientation=orientation,
        )

        return result

    def _apply_orientation_layout(
        self,
        inputs: List[Dict[str, Any]],
        algorithms: List[Dict[str, Any]],
        ranks: Dict[str, int],
        cfg: LayoutConfig,
        orientation: str = "horizontal",
    ) -> None:
        orientation = (orientation or "horizontal").lower()

        if orientation == "vertical":
            for i, inp in enumerate(inputs):
                inp["pos_x"] = 20.0 + i * cfg.h_spacing
                inp["pos_y"] = cfg.input_x

            ranks_by_level: Dict[int, List] = defaultdict(list)
            for alg in algorithms:
                rank = ranks.get(alg.get("id", ""), 0)
                ranks_by_level[rank].append(alg)

            for rank, algs in sorted(ranks_by_level.items()):
                y = cfg.start_x + rank * cfg.h_spacing
                for j, alg in enumerate(algs):
                    alg["pos_x"] = 20.0 + j * cfg.h_spacing
                    alg["pos_y"] = y
            return

        if orientation == "axis":
            for i, inp in enumerate(inputs):
                inp["pos_x"] = cfg.input_x + (i * cfg.h_spacing * 0.35)
                inp["pos_y"] = 20.0 + i * cfg.v_spacing

            ranks_by_level: Dict[int, List] = defaultdict(list)
            for alg in algorithms:
                rank = ranks.get(alg.get("id", ""), 0)
                ranks_by_level[rank].append(alg)

            for rank, algs in sorted(ranks_by_level.items()):
                for j, alg in enumerate(algs):
                    alg["pos_x"] = cfg.start_x + rank * cfg.h_spacing
                    alg["pos_y"] = 20.0 + j * cfg.v_spacing + rank * (cfg.v_spacing * 0.5)
            return

        # horizontal (default)
        for i, inp in enumerate(inputs):
            inp["pos_x"] = cfg.input_x
            inp["pos_y"] = 20.0 + i * cfg.v_spacing

        ranks_by_level: Dict[int, List] = defaultdict(list)
        for alg in algorithms:
            rank = ranks.get(alg.get("id", ""), 0)
            ranks_by_level[rank].append(alg)

        for rank, algs in sorted(ranks_by_level.items()):
            x = cfg.start_x + rank * cfg.h_spacing
            for j, alg in enumerate(algs):
                alg["pos_x"] = x
                alg["pos_y"] = 20.0 + j * cfg.v_spacing

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
            # Keep DAG-level ranks but use this selector as a stable strategy switch.
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

        return {nid: idx for idx, nid in enumerate(order)}

    def _assign_radial_shell_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        """
        Custom strategy:
        1. Treat graph edges as undirected.
        2. Start from source nodes (zero in-degree), or first node fallback.
        3. Assign shell/ring level by BFS distance.
        """
        if not node_ids:
            return {}

        neighbors: Dict[str, Set[str]] = {nid: set() for nid in node_ids}
        in_degree: Dict[str, int] = {nid: 0 for nid in node_ids}
        for src, targets in dep_map.items():
            for tgt in targets:
                if src in neighbors and tgt in neighbors:
                    neighbors[src].add(tgt)
                    neighbors[tgt].add(src)
                if tgt in in_degree:
                    in_degree[tgt] += 1

        seeds = [nid for nid in node_ids if in_degree.get(nid, 0) == 0]
        if not seeds:
            seeds = [node_ids[0]]

        ranks: Dict[str, int] = {}
        queue: deque = deque()
        for s in seeds:
            ranks[s] = 0
            queue.append(s)

        while queue:
            nid = queue.popleft()
            for nxt in neighbors.get(nid, set()):
                if nxt in ranks:
                    continue
                ranks[nxt] = ranks[nid] + 1
                queue.append(nxt)

        for nid in node_ids:
            if nid not in ranks:
                ranks[nid] = 0
        return ranks

    def _assign_ancestor_weighted_levels(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],
    ) -> Dict[str, int]:
        """
        Custom strategy:
        Rank by number of unique upstream ancestors.
        More dependent nodes are pushed further away.
        """
        predecessors: Dict[str, Set[str]] = {nid: set() for nid in node_ids}
        for src, targets in dep_map.items():
            for tgt in targets:
                if tgt in predecessors and src in predecessors:
                    predecessors[tgt].add(src)

        memo: Dict[str, Set[str]] = {}

        def ancestors(nid: str, trail: Set[str]) -> Set[str]:
            if nid in memo:
                return memo[nid]
            if nid in trail:
                return set()
            trail = set(trail)
            trail.add(nid)
            result: Set[str] = set(predecessors.get(nid, set()))
            for pred in predecessors.get(nid, set()):
                result.update(ancestors(pred, trail))
            memo[nid] = result
            return result

        ranks: Dict[str, int] = {}
        for nid in node_ids:
            ranks[nid] = len(ancestors(nid, set()))
        return ranks

    # ── Rank assignment (longest-path layering) ───────────────────────────

    def _assign_ranks(
        self,
        node_ids: List[str],
        dep_map: Dict[str, List[str]],  # node -> list of nodes that depend on it
    ) -> Dict[str, int]:
        """
        Assigns a topological depth (rank) to each node.
        Nodes with no predecessors get rank 0.
        """
        # Build in-degree map and reverse adjacency
        in_degree: Dict[str, int] = {nid: 0 for nid in node_ids}
        rev_adj: Dict[str, List[str]] = defaultdict(list)  # nid -> nodes it provides input to
        for src, targets in dep_map.items():
            for tgt in targets:
                if tgt in in_degree:
                    in_degree[tgt] += 1
                if src in in_degree:
                    rev_adj[src].append(tgt)

        # Kahn's algorithm with rank tracking
        ranks: Dict[str, int] = {}
        queue: deque = deque()
        for nid in node_ids:
            if in_degree.get(nid, 0) == 0:
                queue.append(nid)
                ranks[nid] = 0

        while queue:
            nid = queue.popleft()
            current_rank = ranks.get(nid, 0)
            for child in rev_adj.get(nid, []):
                in_degree[child] -= 1
                ranks[child] = max(ranks.get(child, 0), current_rank + 1)
                if in_degree[child] == 0:
                    queue.append(child)

        # Any nodes not reached (cycles or disconnected) get rank 0
        for nid in node_ids:
            if nid not in ranks:
                ranks[nid] = 0

        return ranks
