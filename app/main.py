"""FastAPI 入口:车辆轨迹匹配 API。

接口一览:
  POST /api/networks            登记带版本的有向路网(版本唯一,登记后不可变)
  GET  /api/networks          列出路网版本
  GET  /api/networks/{v}      查看某版本路网(含路段)
  POST /api/trajectories      提交轨迹(UTC 时刻 + 坐标)
  GET  /api/trajectories/{id} 查看轨迹
  POST /api/match             在指定路网版本上执行匹配,生成连续路径、缺口与接续依据
  GET  /api/analyses          按路网版本 / 轨迹查询历史分析
  GET  /            /api/analyses/{id} 查看分析结果(匹配路段、进入/离开时刻、里程、缺口原因)
  POST /api/analyses/{id}/mileage-review 里程复核:依据已存接续依据计算指定路段集上的最少/最多里程
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import db, review
from .matcher import NetworkIndex, Segment, TrackPoint, node_key_str
from .schemas import MatchRequest, NetworkCreate, ReviewRequest, TrajectoryCreate
from .util import fmt_ts, parse_ts, utcnow


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="车辆轨迹匹配 API", version="1.0.0", lifespan=lifespan)

# 路网版本快照缓存:(db_path, network_id) -> (快照签名, NetworkIndex)
# 路网版本登记后不可变,因此缓存安全;匹配全程只使用同一快照,路网修订不影响进行中的分析。
# 签名用于防御性校验:同一 id 的缓存必须与当前库中的路段快照一致。
_index_cache: dict[tuple[str, int], tuple[tuple, NetworkIndex]] = {}


def _snapshot_signature(seg_rows) -> tuple:
    return tuple(
        (r["segment_id"], r["start_lat"], r["start_lon"], r["end_lat"],
         r["end_lon"], r["length_m"])
        for r in seg_rows
    )


def get_index(network_id: int, seg_rows) -> NetworkIndex:
    key = (db.db_path(), network_id)
    signature = _snapshot_signature(seg_rows)
    cached = _index_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    segments = [
        Segment(r["segment_id"], r["start_lat"], r["start_lon"],
                r["end_lat"], r["end_lon"], r["length_m"])
        for r in seg_rows
    ]
    idx = NetworkIndex(segments)
    _index_cache[key] = (signature, idx)
    return idx


# ------------------------------------------------------------------ 路网
@app.post("/api/networks", status_code=201)
def create_network(body: NetworkCreate):
    """登记一个带版本的有向路网。版本号全局唯一;同版本重复登记返回 409。"""
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM networks WHERE version = ?", (body.version,)).fetchone():
            raise HTTPException(409, f"路网版本 '{body.version}' 已存在;请使用新的版本号登记修订")
        now = fmt_ts(utcnow())
        cur = conn.execute("INSERT INTO networks (version, created_at) VALUES (?, ?)",
                           (body.version, now))
        nid = cur.lastrowid
        conn.executemany(
            "INSERT INTO segments (network_id, segment_id, start_lat, start_lon, "
            "end_lat, end_lon, length_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(nid, s.segment_id, s.start_lat, s.start_lon, s.end_lat, s.end_lon, s.length_m)
             for s in body.segments],
        )
        conn.commit()
        return {"network_id": nid, "version": body.version,
                "segment_count": len(body.segments), "created_at": now}
    finally:
        conn.close()


@app.get("/api/networks")
def list_networks():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT n.id, n.version, n.created_at, COUNT(s.id) AS segment_count "
            "FROM networks n LEFT JOIN segments s ON s.network_id = n.id "
            "GROUP BY n.id ORDER BY n.id"
        ).fetchall()
        return {"networks": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/networks/{version}")
def get_network(version: str):
    conn = db.connect()
    try:
        net = conn.execute("SELECT * FROM networks WHERE version = ?", (version,)).fetchone()
        if not net:
            raise HTTPException(404, f"路网版本 '{version}' 不存在")
        segs = conn.execute(
            "SELECT segment_id, start_lat, start_lon, end_lat, end_lon, length_m "
            "FROM segments WHERE network_id = ? ORDER BY id", (net["id"],)
        ).fetchall()
        return {"network_id": net["id"], "version": net["version"],
                "created_at": net["created_at"],
                "segments": [dict(s) for s in segs]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 轨迹
@app.post("/api/trajectories", status_code=201)
def create_trajectory(body: TrajectoryCreate):
    """提交轨迹。点按 UTC 时刻升序排列后存储。"""
    order = sorted(range(len(body.points)),
                   key=lambda i: (body.points[i].timestamp, i))
    now = fmt_ts(utcnow())
    conn = db.connect()
    try:
        cur = conn.execute("INSERT INTO trajectories (name, created_at) VALUES (?, ?)",
                           (body.name, now))
        tid = cur.lastrowid
        conn.executemany(
            "INSERT INTO trajectory_points (trajectory_id, seq, ts, lat, lon) VALUES (?, ?, ?, ?, ?)",
            [(tid, seq, fmt_ts(body.points[i].timestamp), body.points[i].lat, body.points[i].lon)
             for seq, i in enumerate(order)],
        )
        conn.commit()
        return {"trajectory_id": tid, "name": body.name,
                "point_count": len(body.points), "created_at": now}
    finally:
        conn.close()


@app.get("/api/trajectories/{trajectory_id}")
def get_trajectory(trajectory_id: int):
    conn = db.connect()
    try:
        traj = conn.execute("SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)).fetchone()
        if not traj:
            raise HTTPException(404, f"轨迹 {trajectory_id} 不存在")
        pts = conn.execute(
            "SELECT seq, ts AS timestamp, lat, lon FROM trajectory_points "
            "WHERE trajectory_id = ? ORDER BY seq", (trajectory_id,)
        ).fetchall()
        return {"trajectory_id": traj["id"], "name": traj["name"],
                "created_at": traj["created_at"],
                "points": [dict(p) for p in pts]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 匹配分析
@app.post("/api/match", status_code=201)
def run_match(body: MatchRequest):
    """在指定路网版本上执行轨迹匹配。

    一次分析只读取该版本的路网快照,不混用其他版本;结果连同版本号一起落库,
    之后路网修订不影响本次分析。
    """
    conn = db.connect()
    try:
        traj = conn.execute("SELECT * FROM trajectories WHERE id = ?",
                            (body.trajectory_id,)).fetchone()
        if not traj:
            raise HTTPException(404, f"轨迹 {body.trajectory_id} 不存在")
        net = conn.execute("SELECT * FROM networks WHERE version = ?",
                           (body.network_version,)).fetchone()
        if not net:
            raise HTTPException(404, f"路网版本 '{body.network_version}' 不存在")
        # 一次性读入该版本快照:分析期间不混用版本
        seg_rows = conn.execute(
            "SELECT * FROM segments WHERE network_id = ? ORDER BY id", (net["id"],)
        ).fetchall()
        pt_rows = conn.execute(
            "SELECT ts, lat, lon FROM trajectory_points WHERE trajectory_id = ? ORDER BY seq",
            (traj["id"],)
        ).fetchall()
    finally:
        conn.close()

    index = get_index(net["id"], seg_rows)
    points = [TrackPoint(parse_ts(r["ts"]), r["lat"], r["lon"]) for r in pt_rows]
    result = index.match(points)

    now = fmt_ts(utcnow())
    # 解析接续依据中的路段下标为路段号、图节点键为持久化字符串
    basis_rows = []
    for basis in result["connector_bases"]:
        pieces = [(index.segments[si].segment_id, frm, to)
                  for si, frm, to in basis["pieces"]]
        edges = [(node_key_str(u), node_key_str(v), index.segments[si].segment_id,
                  index.segments[si].length_m)
                 for u, v, si in basis["edges"]]
        basis_rows.append((pieces, edges))

    conn = db.connect()
    try:
        # 分析与接续依据在同一事务写入:依据写入失败则整体回滚,不生成本次分析
        cur = conn.execute(
            "INSERT INTO analyses (trajectory_id, network_id, created_at, total_points, "
            "matched_points, total_distance_m, has_basis) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (traj["id"], net["id"], now, result["total_points"],
             result["matched_points"], result["total_distance_m"]),
        )
        aid = cur.lastrowid
        conn.executemany(
            "INSERT INTO analysis_segments (analysis_id, seq, segment_id, enter_time, "
            "leave_time, distance_m) VALUES (?, ?, ?, ?, ?, ?)",
            [(aid, p["seq"], p["segment_id"], p["enter_time"], p["leave_time"], p["distance_m"])
             for p in result["path"]],
        )
        conn.executemany(
            "INSERT INTO analysis_gaps (analysis_id, seq, start_time, end_time, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            [(aid, g["seq"], g["start_time"], g["end_time"], g["reason"])
             for g in result["gaps"]],
        )
        for cseq, (pieces, edges) in enumerate(basis_rows):
            conn.execute(
                "INSERT INTO analysis_connectors (analysis_id, seq) VALUES (?, ?)",
                (aid, cseq))
            conn.executemany(
                "INSERT INTO analysis_connector_pieces (analysis_id, connector_seq, seq, "
                "segment_id, from_offset_m, to_offset_m) VALUES (?, ?, ?, ?, ?, ?)",
                [(aid, cseq, k, sid, frm, to) for k, (sid, frm, to) in enumerate(pieces)],
            )
            conn.executemany(
                "INSERT INTO analysis_connector_edges (analysis_id, connector_seq, seq, "
                "from_node, to_node, segment_id, length_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(aid, cseq, k, u, v, sid, length)
                 for k, (u, v, sid, length) in enumerate(edges)],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    public = {k: v for k, v in result.items() if k != "connector_bases"}
    return {"analysis_id": aid, "trajectory_id": traj["id"],
            "network_version": net["version"], "created_at": now, **public}


@app.get("/api/analyses")
def list_analyses(network_version: str | None = Query(default=None),
                  trajectory_id: int | None = Query(default=None)):
    """查询历史分析;可按原路网版本过滤(路网修订后旧分析仍可查)。"""
    sql = ("SELECT a.id AS analysis_id, a.trajectory_id, n.version AS network_version, "
           "a.created_at, a.total_points, a.matched_points, a.total_distance_m "
           "FROM analyses a JOIN networks n ON n.id = a.network_id")
    cond, args = [], []
    if network_version is not None:
        cond.append("n.version = ?")
        args.append(network_version)
    if trajectory_id is not None:
        cond.append("a.trajectory_id = ?")
        args.append(trajectory_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY a.id"
    conn = db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        return {"analyses": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int):
    conn = db.connect()
    try:
        a = conn.execute(
            "SELECT a.*, n.version AS network_version FROM analyses a "
            "JOIN networks n ON n.id = a.network_id WHERE a.id = ?", (analysis_id,)
        ).fetchone()
        if not a:
            raise HTTPException(404, f"分析 {analysis_id} 不存在")
        segs = conn.execute(
            "SELECT seq, segment_id, enter_time, leave_time, distance_m "
            "FROM analysis_segments WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
        ).fetchall()
        gaps = conn.execute(
            "SELECT seq, start_time, end_time, reason "
            "FROM analysis_gaps WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
        ).fetchall()
        return {"analysis_id": a["id"], "trajectory_id": a["trajectory_id"],
                "network_version": a["network_version"], "created_at": a["created_at"],
                "total_points": a["total_points"], "matched_points": a["matched_points"],
                "total_distance_m": a["total_distance_m"],
                "path": [dict(s) for s in segs],
                "gaps": [dict(g) for g in gaps]}
    finally:
        conn.close()


@app.post("/api/analyses/{analysis_id}/mileage-review")
def mileage_review(analysis_id: int, body: ReviewRequest):
    """里程复核:传入该版路网的一组有向路段号,返回整份分析在这组路段上
    可能行驶的最少和最多米数。

    - 以原匹配选中的吸附点为准,每对连续采样可各自取任一最短接续路线;
    - 首尾只计实际经过的里程,缺口两侧不接续;
    - 只读取匹配时落库的接续依据(固定片段 + 紧边 DAG),不重跑匹配、
      不重新搜索路网,也不枚举等长路线;换一组路段直接复用同一依据;
    - 旧分析缺少接续依据时返回 409,提示重新匹配。
    """
    conn = db.connect()
    try:
        a = conn.execute(
            "SELECT a.*, n.version AS network_version FROM analyses a "
            "JOIN networks n ON n.id = a.network_id WHERE a.id = ?", (analysis_id,)
        ).fetchone()
        if not a:
            raise HTTPException(404, f"分析 {analysis_id} 不存在")
        if not a["has_basis"]:
            raise HTTPException(
                409,
                f"分析 {analysis_id} 生成时未保存接续依据(历史分析),请在该版路网上重新匹配后再复核",
            )
        # 路段号必须属于该分析使用的路网版本
        valid_ids = {
            r["segment_id"]
            for r in conn.execute(
                "SELECT segment_id FROM segments WHERE network_id = ?", (a["network_id"],)
            ).fetchall()
        }
        unknown = [s for s in body.segment_ids if s not in valid_ids]
        if unknown:
            raise HTTPException(
                400,
                f"路段号不属于路网版本 '{a['network_version']}': {', '.join(unknown)}",
            )
        crows = conn.execute(
            "SELECT seq FROM analysis_connectors WHERE analysis_id = ? ORDER BY seq",
            (analysis_id,),
        ).fetchall()
        connectors = []
        for cr in crows:
            cseq = cr["seq"]
            pieces = [
                review.Piece(r["segment_id"], r["from_offset_m"], r["to_offset_m"])
                for r in conn.execute(
                    "SELECT segment_id, from_offset_m, to_offset_m "
                    "FROM analysis_connector_pieces WHERE analysis_id = ? AND connector_seq = ? "
                    "ORDER BY seq", (analysis_id, cseq),
                ).fetchall()
            ]
            edges = [
                review.DagEdge(r["from_node"], r["to_node"], r["segment_id"], r["length_m"])
                for r in conn.execute(
                    "SELECT from_node, to_node, segment_id, length_m "
                    "FROM analysis_connector_edges WHERE analysis_id = ? AND connector_seq = ? "
                    "ORDER BY seq", (analysis_id, cseq),
                ).fetchall()
            ]
            connectors.append((pieces, edges))
    finally:
        conn.close()

    result = review.review_analysis(connectors, body.segment_ids)
    return {"analysis_id": analysis_id,
            "network_version": a["network_version"],
            "segment_ids": body.segment_ids,
            "reviewed_at": fmt_ts(utcnow()),
            **result}


@app.get("/health")
def health():
    return {"status": "ok"}
