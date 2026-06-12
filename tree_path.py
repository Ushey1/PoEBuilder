"""
Passive-tree pathfinding.

Given a class starting node and a set of "required" notable nodes, compute
a tree of allocated nodes that connects them all, within a passive-point
budget. The connecting small nodes provide travel (+10 stats, small dmg
cluster nodes) that the calc engine then aggregates.

This is a Steiner tree problem (NP-hard in general). We use a standard
greedy approximation:
  1. Allocated set starts as just the class root.
  2. While required notables remain unreached:
       - Multi-source BFS from the allocated set to all unallocated nodes.
       - Pick the cheapest required notable still unreached.
       - Add the entire path to it (start...notable) to the allocated set.
  3. If the budget runs out, return what's allocated so far — the caller
     gets a partial path and the unmet required list.

Ascendancy nodes are excluded from the traversable graph because they
form a separate allocation system in PoE (you pick ascendancy points
independently, they're not on the main tree).
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache

import pob_data


@dataclass
class PathResult:
    allocated: list[str]          # ordered roughly by allocation (start first)
    unmet_required: list[str]     # notables we couldn't reach within budget
    budget_used: int              # excludes start node


@lru_cache(maxsize=1)
def _build_graph() -> dict[str, set[str]]:
    """Adjacency map for the main passive tree (ascendancies excluded).

    Edges are treated as undirected — `in` and `out` in the raw data
    just describe which side of the connection the node is rendered on,
    not traversal direction.
    """
    nodes = pob_data.load_tree().get("nodes", {})
    graph: dict[str, set[str]] = {}
    for nid, node in nodes.items():
        if node.get("ascendancyName"):
            continue
        neighbors = set()
        for n in node.get("in") or []:
            if str(n) in nodes and not nodes[str(n)].get("ascendancyName"):
                neighbors.add(str(n))
        for n in node.get("out") or []:
            if str(n) in nodes and not nodes[str(n)].get("ascendancyName"):
                neighbors.add(str(n))
        graph[nid] = neighbors
    # Symmetrize — some edges only show up on one side of a pair.
    for nid, ns in list(graph.items()):
        for n in ns:
            graph.setdefault(n, set()).add(nid)
    return graph


def _bfs_from_set(starts: set[str], graph: dict[str, set[str]]
                  ) -> dict[str, tuple[int, str | None]]:
    """Return {node_id: (distance, prev)} for every reachable node.

    Distance from any node already in `starts` is 0 (multi-source BFS).
    `prev` is None for starts; otherwise the neighbor we came from.
    """
    dist: dict[str, tuple[int, str | None]] = {s: (0, None) for s in starts}
    queue = deque(starts)
    while queue:
        cur = queue.popleft()
        d_cur = dist[cur][0]
        for nb in graph.get(cur, ()):
            if nb not in dist:
                dist[nb] = (d_cur + 1, cur)
                queue.append(nb)
    return dist


def _trace_back(target: str, dist: dict[str, tuple[int, str | None]]
                ) -> list[str]:
    """Walk parent pointers from `target` back to its source. Returns the
    full path including target and the source (in source -> target order)."""
    path: list[str] = []
    cur: str | None = target
    while cur is not None:
        path.append(cur)
        cur = dist[cur][1]
    path.reverse()
    return path


def steiner_approximation(
    start: str,
    required: list[str] | set[str],
    budget: int,
    *,
    fill_clusters: bool = True,
) -> PathResult:
    """Greedy nearest-required Steiner approximation.

    `budget` is the number of points beyond the start node that may be
    allocated. The start itself doesn't consume budget.

    With `fill_clusters=True` (default), after connecting all required
    notables, any remaining budget is spent on small nodes adjacent to
    already-allocated nodes (preferring nodes in the same passive-tree
    `group` as an allocated notable, i.e. its cluster). This mimics how
    real builds allocate the 3-5 small filler nodes around each notable.
    """
    graph = _build_graph()
    nodes = pob_data.load_tree().get("nodes", {})
    requested = {str(r) for r in required if str(r) != start}
    invalid = {r for r in requested if r not in graph}
    required_set = requested - invalid
    allocated: set[str] = {start}
    order: list[str] = [start]
    unreachable: set[str] = set(invalid)

    while True:
        remaining = required_set - allocated - unreachable
        if not remaining:
            break
        dist = _bfs_from_set(allocated, graph)

        best: tuple[int, str] | None = None
        for r in remaining:
            d_pair = dist.get(r)
            if d_pair is None:
                continue
            if best is None or d_pair[0] < best[0]:
                best = (d_pair[0], r)

        if best is None:
            unreachable.update(remaining)
            break

        cost, target = best
        path = _trace_back(target, dist)
        new_nodes = [n for n in path if n not in allocated]
        if len(new_nodes) > budget:
            break
        for n in new_nodes:
            allocated.add(n)
            order.append(n)
        budget -= len(new_nodes)

    if fill_clusters and budget > 0:
        # Spend remaining budget on small filler. Priority: small nodes in
        # the same `group` as an already-allocated notable (true cluster
        # fillers), then any frontier small that's only 1 hop from allocated.
        notable_groups: set[int] = set()
        for nid in allocated:
            n = nodes.get(nid) or {}
            if n.get("isNotable") and n.get("group") is not None:
                notable_groups.add(n["group"])

        def is_small(nid: str) -> bool:
            n = nodes.get(nid) or {}
            return not (n.get("isNotable") or n.get("isKeystone")
                        or n.get("isJewelSocket") or n.get("isMastery"))

        # Pass 1: cluster smalls (same group as allocated notables)
        cluster_candidates = [
            nid for nid, n in nodes.items()
            if nid not in allocated
            and n.get("group") in notable_groups
            and is_small(nid)
            and nid in graph
        ]
        # Of those, only keep ones reachable in 1 hop from allocated
        # (avoids allocating disconnected nodes in the same group cluster).
        for nid in cluster_candidates:
            if budget <= 0:
                break
            if any(neighbor in allocated for neighbor in graph.get(nid, ())):
                allocated.add(nid)
                order.append(nid)
                budget -= 1

        # Pass 2: any frontier small until budget exhausted.
        # Repeatedly grow by 1 hop, allocating small nodes adjacent to allocated.
        while budget > 0:
            frontier: list[str] = []
            for nid in allocated:
                for nb in graph.get(nid, ()):
                    if nb in allocated:
                        continue
                    if not is_small(nb):
                        continue
                    frontier.append(nb)
            if not frontier:
                break
            for nb in frontier:
                if budget <= 0:
                    break
                if nb not in allocated:
                    allocated.add(nb)
                    order.append(nb)
                    budget -= 1

    return PathResult(
        allocated=order,
        unmet_required=sorted(unreachable | (required_set - allocated)),
        budget_used=len(order) - 1,
    )


def default_budget(char_level: int) -> int:
    """Total passive points available at this character level.

    1 per level after level 1 (so a lvl-90 char has 89 from levels) plus
    22 quest passives in PoE 1. Doesn't account for jewel sockets or any
    Tattoo/timeless variants — those are additive and tuneable later.
    """
    return max(0, char_level - 1) + 22
