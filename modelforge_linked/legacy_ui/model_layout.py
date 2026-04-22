"""
Auto-layout engine for Model Forge.
Computes a clean left-to-right DAG layout for model components
before writing the final .model3 JSON file.
"""

from collections import defaultdict, deque


# Layout constants (pixels in the Model Designer canvas)
LEVEL_SPACING_X = 250   # horizontal gap between columns
NODE_SPACING_Y = 140    # vertical gap between nodes in the same column
INPUT_OFFSET_X = 50     # starting x for model inputs
INPUT_SPACING_Y = 100   # vertical gap between model inputs
ALGO_START_X = 350      # first algorithm column x


def compute_layout(model_json):
    """
    Takes a model JSON dict with "inputs" and "algorithms" keys.
    Adds/updates "position": {"x": ..., "y": ...} on every input and algorithm.
    Returns the modified model_json.
    """
    inputs = model_json.get("inputs", [])
    algorithms = model_json.get("algorithms", [])

    if not algorithms:
        _layout_inputs_only(inputs)
        return model_json

    # Build adjacency: algorithm id -> list of parent algorithm ids
    algo_ids = [a["id"] for a in algorithms]
    algo_by_id = {a["id"]: a for a in algorithms}
    parents = defaultdict(set)

    for algo in algorithms:
        for _key, val in algo.get("parameters", {}).items():
            if isinstance(val, dict) and val.get("type") == "child_output":
                parent_id = val.get("child_id")
                if parent_id in algo_by_id:
                    parents[algo["id"]].add(parent_id)

    # Topological sort + level assignment (longest path from roots)
    levels = {}
    for aid in algo_ids:
        levels[aid] = _compute_level(aid, parents, levels)

    # Group algorithms by level
    level_groups = defaultdict(list)
    for aid in algo_ids:
        level_groups[levels[aid]].append(aid)

    # Sort within each level by original order for stability
    id_order = {aid: idx for idx, aid in enumerate(algo_ids)}
    for lev in level_groups:
        level_groups[lev].sort(key=lambda a: id_order.get(a, 0))

    # Assign positions to algorithms
    max_level = max(level_groups.keys()) if level_groups else 0
    for lev, aids in level_groups.items():
        x = ALGO_START_X + lev * LEVEL_SPACING_X
        total_height = (len(aids) - 1) * NODE_SPACING_Y
        start_y = max(80, 200 - total_height // 2)  # roughly centered
        for idx, aid in enumerate(aids):
            y = start_y + idx * NODE_SPACING_Y
            algo_by_id[aid]["position"] = {"x": x, "y": y}

    # Layout model inputs in a column to the left
    _layout_inputs_column(inputs)

    return model_json


def _compute_level(node_id, parents, levels):
    """Recursively compute the level (longest path from any root)."""
    if node_id in levels:
        return levels[node_id]
    if not parents[node_id]:
        levels[node_id] = 0
        return 0
    max_parent_level = max(
        _compute_level(p, parents, levels) for p in parents[node_id]
    )
    levels[node_id] = max_parent_level + 1
    return levels[node_id]


def _layout_inputs_only(inputs):
    """If there are no algorithms, just stack the inputs vertically."""
    for idx, inp in enumerate(inputs):
        inp["position"] = {
            "x": INPUT_OFFSET_X,
            "y": 80 + idx * INPUT_SPACING_Y,
        }


def _layout_inputs_column(inputs):
    """Place model inputs in a left column, stacked vertically."""
    for idx, inp in enumerate(inputs):
        inp["position"] = {
            "x": INPUT_OFFSET_X,
            "y": 80 + idx * INPUT_SPACING_Y,
        }
