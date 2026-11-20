"""里程复核 API 测试。

覆盖:
- 两条等长最短接续路线下,指定路段集的最少/最多里程(不枚举路线);
- 首尾固定片段只计实际里程、同路段顺向接续、换路段集复用已存依据;
- 缺口两侧不接续,各对接续独立求和;
- 依据与分析同事务落库,依据失败则分析不生成;
- 旧分析缺少依据 -> 409 提示重新匹配;路段号非法 -> 400/422;
- 复核不重跑匹配、不重新搜索路网。
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app, get_index
from app.matcher import NetworkIndex

# 与 test_api 共享同一应用与测试库(conftest 已固定 DATABASE_PATH)
client = TestClient(app)
db.init_db()

REVIEW = "/api/analyses/{aid}/mileage-review"


def post_json(url, payload, expect=201):
    r = client.post(url, json=payload)
    assert r.status_code == expect, r.text
    return r.json()


def make_traj(points, name=None):
    return {"name": name,
            "points": [{"timestamp": t, "lat": la, "lon": lo} for t, la, lo in points]}


# ---------------------------------------------------------------- 测试路网
# 菱形:src --D0--> N1, N1 有两条等长(200m)最短接续到 N3:
#   路线 1: D1(100) + D2(100);路线 2: D3(100) + D4(100);N3 --D5(85)--> sink
# 路段号全局加 D 前缀,避免与其他测试版本(v1 也有 S1..S3)在共享库中撞名。
NET_DIAMOND = {
    "version": "diamond",
    "segments": [
        {"segment_id": "D0", "start_lat": 39.9000, "start_lon": 116.4000,
         "end_lat": 39.9000, "end_lon": 116.4010, "length_m": 85.0},
        {"segment_id": "D1", "start_lat": 39.9000, "start_lon": 116.4010,
         "end_lat": 39.9000, "end_lon": 116.4020, "length_m": 100.0},
        {"segment_id": "D2", "start_lat": 39.9000, "start_lon": 116.4020,
         "end_lat": 39.9000, "end_lon": 116.4030, "length_m": 100.0},
        {"segment_id": "D3", "start_lat": 39.9000, "start_lon": 116.4010,
         "end_lat": 39.8990, "end_lon": 116.4020, "length_m": 100.0},
        {"segment_id": "D4", "start_lat": 39.8990, "start_lon": 116.4020,
         "end_lat": 39.9000, "end_lon": 116.4030, "length_m": 100.0},
        {"segment_id": "D5", "start_lat": 39.9000, "start_lon": 116.4030,
         "end_lat": 39.9000, "end_lon": 116.4040, "length_m": 85.0},
    ],
}

# 沿 D0 -> (菱形,轨迹不偏向任一分支) -> D5 的采样点
TRAJ_DIAMOND = [
    ("2026-02-01T08:00:00Z", 39.9000, 116.4000),   # D0 起点
    ("2026-02-01T08:00:10Z", 39.9000, 116.4010),   # N1:D0 终点
    ("2026-02-01T08:00:20Z", 39.9000, 116.4030),   # N3:两条等长路线汇合点
    ("2026-02-01T08:00:30Z", 39.9000, 116.4035),   # D5 中点(偏移 42.5)
]

# 两个不连通分量(带缺口),路段号与其他版本隔离
NET_GAP = {
    "version": "gapnet",
    "segments": [
        {"segment_id": "G1", "start_lat": 39.9500, "start_lon": 116.5000,
         "end_lat": 39.9500, "end_lon": 116.5010, "length_m": 85.0},
        {"segment_id": "G2", "start_lat": 39.9500, "start_lon": 116.5010,
         "end_lat": 39.9500, "end_lon": 116.5020, "length_m": 85.0},
        {"segment_id": "H1", "start_lat": 39.9600, "start_lon": 116.5100,
         "end_lat": 39.9600, "end_lon": 116.5110, "length_m": 85.0},
        {"segment_id": "H2", "start_lat": 39.9600, "start_lon": 116.5110,
         "end_lat": 39.9600, "end_lon": 116.5120, "length_m": 85.0},
    ],
}


def _ensure_network(payload):
    """路网版本不可变:测试间共享库,已登记则跳过。"""
    r = client.post("/api/networks", json=payload)
    if r.status_code == 409:
        return
    assert r.status_code == 201, r.text


def _setup_diamond():
    _ensure_network(NET_DIAMOND)
    traj = post_json("/api/trajectories", make_traj(TRAJ_DIAMOND, "diamond-trip"))
    res = post_json("/api/match",
                    {"trajectory_id": traj["trajectory_id"], "network_version": "diamond"})
    return res["analysis_id"]


# ---------------------------------------------------------------- 用例
def test_equal_length_shortest_routes_give_min_max_range():
    """两条等长最短接续路线:只能被其中一条经过的路段,里程区间为 0..100。"""
    aid = _setup_diamond()

    # S1 仅在路线 1 上:最少 0(走 S3/S4),最多 100(走 S1/S2)
    r = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D1"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["min_distance_m"] == 0.0
    assert body["max_distance_m"] == 100.0
    # 3 对接续:S0 内同路段接续 + N1->N3 菱形接续 + N3->S5 中点同路段接续
    assert len(body["connectors"]) == 3

    # 对称分支
    body = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D3"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (0.0, 100.0)

    # 一条完整分支:0..200
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D1", "D2"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (0.0, 200.0)
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D3", "D4"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (0.0, 200.0)

    # 两条分支各取一条边:任一最短路线都恰好经过其中一条 -> 恒为 100
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D1", "D3"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (100.0, 100.0)
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D2", "D4"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (100.0, 100.0)

    # 菱形全部中间路段:任一最短路线都走 200m
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D1", "D2", "D3", "D4"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (200.0, 200.0)


def test_fixed_end_pieces_count_only_actual_mileage():
    """首尾固定片段:S0 全程 85;S5 只经过前 42.5m,均与分支选择无关。"""
    aid = _setup_diamond()
    body = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D0"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (85.0, 85.0)

    body = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D5"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (42.5, 42.5)

    # 全部路段:固定 85 + 42.5 + 菱形 200 = 327.5,与普通报告里程一致
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D0", "D1", "D2", "D3", "D4", "D5"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (327.5, 327.5)
    detail = client.get(f"/api/analyses/{aid}").json()
    assert abs(detail["total_distance_m"] - 327.5) < 1e-6

    # 混合:固定首尾 + 可变分支
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["D0", "D2", "D5"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (127.5, 227.5)


def test_review_reuses_stored_basis_without_recompute(monkeypatch):
    """换一组路段直接复用已存接续依据:复核期间不重建路网索引、不重跑最短路。"""
    aid = _setup_diamond()

    calls = {"index": 0, "dijkstra": 0, "dag": 0}
    orig_get_index = get_index

    def spy_get_index(network_id, seg_rows):  # 复核路径根本不应取索引
        calls["index"] += 1
        return orig_get_index(network_id, seg_rows)

    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_index", spy_get_index)
    monkeypatch.setattr(NetworkIndex, "shortest_from",
                        lambda self, src: calls.__setitem__("dijkstra", calls["dijkstra"] + 1))
    monkeypatch.setattr(NetworkIndex, "tight_dag_from",
                        lambda self, src: calls.__setitem__("dag", calls["dag"] + 1))

    for subset in (["D1"], ["D1", "D2"], ["D3", "D4"],
                   ["D0", "D1", "D2", "D3", "D4", "D5"]):
        r = client.post(REVIEW.format(aid=aid), json={"segment_ids": subset})
        assert r.status_code == 200, r.text

    assert calls == {"index": 0, "dijkstra": 0, "dag": 0}


def test_gap_sides_are_not_connected():
    """缺口两侧不接续:两个分量独立计里程,不会跨缺口拼接路线。"""
    _ensure_network(NET_GAP)
    pts = [
        ("2026-03-01T08:00:00Z", 39.9500, 116.5000),
        ("2026-03-01T08:00:10Z", 39.9500, 116.5020),   # G1->G2 末端
        ("2026-03-01T08:00:20Z", 39.9600, 116.5100),   # 跳到另一分量 -> disconnected
        ("2026-03-01T08:00:30Z", 39.9600, 116.5120),   # H1->H2 末端
    ]
    traj = post_json("/api/trajectories", make_traj(pts))
    res = post_json("/api/match",
                    {"trajectory_id": traj["trajectory_id"], "network_version": "gapnet"})
    aid = res["analysis_id"]
    assert [g["reason"] for g in res["gaps"]] == ["disconnected"]

    body = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["G1"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (85.0, 85.0)
    assert len(body["connectors"]) == 2  # 每侧各一条接续,没有跨缺口接续
    body = client.post(REVIEW.format(aid=aid),
                       json={"segment_ids": ["G1", "G2", "H1", "H2"]}).json()
    # 两侧各 G1+G2 / H1+H2 = 170m,合计 340;缺口里程不计,也没有跨缺口接续
    assert (body["min_distance_m"], body["max_distance_m"]) == (340.0, 340.0)

    # 只选第二分量
    body = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["H2"]}).json()
    assert (body["min_distance_m"], body["max_distance_m"]) == (85.0, 85.0)


def test_basis_persisted_and_ordinary_report_unchanged():
    """新分析落库接续依据;普通报告字段与路径保持原样(不含内部依据)。"""
    aid = _setup_diamond()
    conn = db.connect()
    try:
        assert conn.execute(
            "SELECT has_basis FROM analyses WHERE id = ?", (aid,)).fetchone()["has_basis"] == 1
        n_conn = conn.execute(
            "SELECT COUNT(*) AS c FROM analysis_connectors WHERE analysis_id = ?",
            (aid,)).fetchone()["c"]
        assert n_conn == 3
        # 菱形接续的紧边 DAG 含 4 条边(S1,S2,S3,S4),但没有任何路线枚举记录
        n_edges = conn.execute(
            "SELECT COUNT(*) AS c FROM analysis_connector_edges WHERE analysis_id = ?",
            (aid,)).fetchone()["c"]
        assert n_edges == 4
        # 固定片段:S0 全程 + S5 半程
        pieces = conn.execute(
            "SELECT segment_id, from_offset_m, to_offset_m FROM analysis_connector_pieces "
            "WHERE analysis_id = ? ORDER BY connector_seq, seq", (aid,)).fetchall()
        ids = [p["segment_id"] for p in pieces]
        assert "D0" in ids and "D5" in ids
    finally:
        conn.close()

    detail = client.get(f"/api/analyses/{aid}").json()
    assert "connector_bases" not in detail
    assert [p["segment_id"] for p in detail["path"]][0] == "D0"


def test_basis_write_failure_rolls_back_analysis():
    """依据写入失败时整体回滚:不生成分析、不残留路径与依据。"""
    _ensure_network(NET_DIAMOND)
    traj = post_json("/api/trajectories", make_traj(TRAJ_DIAMOND, "rollback-trip"))
    r_conn = db.connect()
    # 取下一个分析 id,用于限定只检查本次失败事务的残留
    next_aid = r_conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM analyses").fetchone()["n"]
    before = r_conn.execute("SELECT COUNT(*) AS c FROM analyses").fetchone()["c"]
    r_conn.execute(
        "CREATE TRIGGER fail_basis_insert BEFORE INSERT ON analysis_connector_edges "
        "BEGIN SELECT RAISE(ABORT, 'injected basis write failure'); END")
    r_conn.commit()
    r_conn.close()

    try:
        # TestClient(raise_server_exceptions=True):服务端未捕获异常直接抛出,
        # 证明依据写入失败向上冒泡且事务回滚
        with pytest.raises(Exception):
            client.post("/api/match",
                        json={"trajectory_id": traj["trajectory_id"], "network_version": "diamond"})

        conn = db.connect()
        try:
            after = conn.execute("SELECT COUNT(*) AS c FROM analyses").fetchone()["c"]
            assert after == before
            # 本次失败的事务不允许留下任何分析与依据残留(全表其他测试的数据不受影响)
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM analyses WHERE id >= ?", (next_aid,)).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM analysis_connectors WHERE analysis_id >= ?",
                (next_aid,)).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM analysis_connector_edges WHERE analysis_id >= ?",
                (next_aid,)).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM analysis_connector_pieces WHERE analysis_id >= ?",
                (next_aid,)).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM analysis_segments WHERE analysis_id >= ?",
                (next_aid,)).fetchone()["c"] == 0
        finally:
            conn.close()
    finally:
        # 无论断言结果如何都必须摘除触发器,避免污染同库的后续测试
        cleanup = db.connect()
        try:
            cleanup.execute("DROP TRIGGER IF EXISTS fail_basis_insert")
            cleanup.commit()
        finally:
            cleanup.close()


def test_legacy_analysis_without_basis_prompts_rematch():
    """旧分析缺少接续依据 -> 409 并提示重新匹配。"""
    _ensure_network(NET_DIAMOND)
    traj = post_json("/api/trajectories", make_traj(TRAJ_DIAMOND, "legacy-trip"))
    aid = post_json("/api/match",
                    {"trajectory_id": traj["trajectory_id"], "network_version": "diamond"})["analysis_id"]
    # 模拟历史分析:清除依据并置 has_basis=0
    conn = db.connect()
    try:
        conn.execute("DELETE FROM analysis_connector_edges WHERE analysis_id = ?", (aid,))
        conn.execute("DELETE FROM analysis_connector_pieces WHERE analysis_id = ?", (aid,))
        conn.execute("DELETE FROM analysis_connectors WHERE analysis_id = ?", (aid,))
        conn.execute("UPDATE analyses SET has_basis = 0 WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    r = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D1"]})
    assert r.status_code == 409
    assert "重新匹配" in r.json()["detail"]

    # 重新匹配后可正常复核
    new_aid = post_json("/api/match",
                        {"trajectory_id": traj["trajectory_id"], "network_version": "diamond"})["analysis_id"]
    r = client.post(REVIEW.format(aid=new_aid), json={"segment_ids": ["D1"]})
    assert r.status_code == 200
    assert (r.json()["min_distance_m"], r.json()["max_distance_m"]) == (0.0, 100.0)


def test_review_validation_errors():
    aid = _setup_diamond()
    # 分析不存在 -> 404
    assert client.post(REVIEW.format(aid=999999), json={"segment_ids": ["D1"]}).status_code == 404
    # 路段不属于该路网版本 -> 400
    r = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["NOPE"]})
    assert r.status_code == 400
    # 重复路段号 -> 422
    r = client.post(REVIEW.format(aid=aid), json={"segment_ids": ["D1", "D1"]})
    assert r.status_code == 422
    # 空路段集 -> 422
    r = client.post(REVIEW.format(aid=aid), json={"segment_ids": []})
    assert r.status_code == 422
