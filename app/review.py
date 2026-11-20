"""里程复核:在已落库的接续依据上计算指定路段集上的可能行驶里程区间。

复核只读取匹配时生成的依据(首尾固定片段 + 紧边 DAG),不重跑匹配、
不重新搜索路网,也**不逐条枚举等长最短路线**:

- 每对接续点可在其全部最短接续路线中任取一条,覆盖这些路线的紧边 DAG
  在匹配时已一次性入库;
- 单个接续在路段集 S 上的最小/最大里程 = 固定片段贡献 + DAG 上的
  最短/最长加权路径(Kahn 拓扑序 DP,边权为该边路段在 S 中的长度,否则 0);
- 各对接续相互独立,全局最小/最大值为各对接续最小/最大值之和
  (独立选择,无需做路线组合的笛卡尔积);
- 缺口两侧不属于同一接续,天然不接续;首尾路段只按固定片段中的实际偏移计里程。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# 与 matcher.TIGHT_EPS_M 对应:结果兜底容差
_EPS_M = 1e-9


@dataclass
class Piece:
    segment_id: str
    from_offset_m: float
    to_offset_m: float


@dataclass
class DagEdge:
    from_node: str
    to_node: str
    segment_id: str
    length_m: float


def _fixed_contribution(pieces: list[Piece], selected: frozenset[str]) -> float:
    """首尾固定片段在指定路段集上的实际里程(只计经过的偏移区间)。"""
    total = 0.0
    for p in pieces:
        if p.segment_id in selected:
            total += max(0.0, p.to_offset_m - p.from_offset_m)
    return total


def _dag_extremes(edges: list[DagEdge], selected: frozenset[str]) -> tuple[float, float]:
    """紧边 DAG 上指定路段集的最短/最长加权路径长度(拓扑 DP,不枚举路线)。"""
    if not edges:
        return 0.0, 0.0
    nodes: set[str] = set()
    adj: dict[str, list[tuple[str, float]]] = {}
    indeg: dict[str, int] = {}
    for e in edges:
        nodes.add(e.from_node)
        nodes.add(e.to_node)
        w = e.length_m if e.segment_id in selected else 0.0
        adj.setdefault(e.from_node, []).append((e.to_node, w))
        indeg[e.to_node] = indeg.get(e.to_node, 0) + 1
        indeg.setdefault(e.from_node, indeg.get(e.from_node, 0))
    # 源点 = 入度为 0 的节点(裁剪后的紧边 DAG 中唯一);汇点 = 出度为 0 的节点
    sources = [n for n in nodes if indeg[n] == 0]
    dp_min = {n: float("inf") for n in nodes}
    dp_max = {n: float("-inf") for n in nodes}
    for s in sources:
        dp_min[s] = 0.0
        dp_max[s] = 0.0
    queue = deque(sources)
    while queue:
        u = queue.popleft()
        for v, w in adj.get(u, ()):
            cand_min, cand_max = dp_min[u] + w, dp_max[u] + w
            if cand_min < dp_min[v]:
                dp_min[v] = cand_min
            if cand_max > dp_max[v]:
                dp_max[v] = cand_max
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    sinks = [n for n in nodes if not adj.get(n)]
    lo = min(dp_min[n] for n in sinks)
    hi = max(dp_max[n] for n in sinks)
    return lo, hi


def review_connector(pieces: list[Piece], edges: list[DagEdge],
                     selected: frozenset[str]) -> tuple[float, float]:
    """单个接续在指定路段集上的(最小里程, 最大里程)。"""
    fixed = _fixed_contribution(pieces, selected)
    lo, hi = _dag_extremes(edges, selected)
    return fixed + lo, fixed + hi


def review_analysis(connectors: list[tuple[list[Piece], list[DagEdge]]],
                    segment_ids: list[str]) -> dict:
    """整份分析在一组有向路段上可能行驶的最少/最多米数。

    connectors: 已落库的全部接续(固定片段, 紧边 DAG 边),按接续顺序;
    segment_ids: 该版路网的一组有向路段号(调用方负责校验归属与去重)。
    """
    selected = frozenset(segment_ids)
    total_min = total_max = 0.0
    details = []
    for seq, (pieces, edges) in enumerate(connectors):
        lo, hi = review_connector(pieces, edges, selected)
        total_min += lo
        total_max += hi
        details.append({"seq": seq, "min_m": round(lo, 3), "max_m": round(hi, 3)})
    # 浮点兜底:min 不得大于 max
    if total_min > total_max and total_min - total_max <= _EPS_M:
        total_min = total_max
    return {"min_distance_m": round(total_min, 3),
            "max_distance_m": round(total_max, 3),
            "connectors": details}
