# 车辆轨迹匹配 API

基于 **FastAPI + SQLite** 的地图匹配(Map Matching)服务:登记带版本的有向路网,提交带 UTC 时刻的轨迹,将轨迹点吸附到 50 米内的候选路段,结合路网连通性用 HMM + Viterbi 生成全局最优的连续路径,无法连通或超距的区间标记为缺口。匹配时同步落库**接续依据**(覆盖全部等长最短接续路线的紧边 DAG),并提供**里程复核 API**:传入一组路段号,返回整份分析在这组路段上可能行驶的最少/最多米数。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`,SQLite 数据持久化在命名卷 `traj-data`(`/data/app.db`)。交互式文档见 `http://localhost:8000/docs`。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试:

```bash
.venv/bin/python -m pytest tests/ -q
```

## 匹配算法

1. **候选生成**:每个 GPS 点投影到 50 米(`MATCH_RADIUS_M`)以内的有向路段,取投影点作为吸附位置(每点最多保留 8 个最近候选)。
2. **连通性**:候选 a → b 的路径距离 = a 沿路段到终点 + 有向图最短路径(Dijkstra,按源点缓存)+ b 在目标路段上的偏移。端点坐标一致(1e-6 度内)即视为同一图节点;方向不可逆行。
3. **全局最优(HMM + Viterbi)**:
   - 发射概率:点到路段距离的高斯似然(σ = 15m);
   - 转移概率:`exp(-|路网路径距离 − 两点球面直线距离| / 30m)`,不连通则禁止。
   - 同一时刻存在多个候选时,取全局概率最大的路径而非逐点最近邻,保证路径连续。
4. **连续路径展开**:相邻匹配点之间按最短路径补全中间路段,生成完整连续路径;进入/离开时刻按里程比例在相邻采样点时刻间插值。
5. **缺口**:
   - `out_of_range` — 点距所有路段超过 50 米,无候选;
   - `disconnected` — 前后点均有候选,但有向路网不连通,无法生成连续路径。

## 里程复核

两个采样点之间存在多条等长最短路线时,普通报告只展示其中一条;里程复核给出整份分析在指定路段集上的**可能行驶里程区间**。

### 接续依据(匹配时一次性生成,不枚举路线)

以原匹配选中的吸附点为准,对每对连续采样(同一连续匹配段内;缺口两侧不接续)保存:

- **固定片段**:首路段从吸附点到终点、末路段从起点到吸附点的实际经过偏移区间——首尾只计实际经过的里程;
- **紧边 DAG**:从首路段终点跑一次 Dijkstra 得到最短距离,满足 `dist[x] + w(x→y) = dist[y]` 的边(紧边)恰好是全部最短路线的并集;再反向裁剪出能到达末路段吸附点的部分。只存边,**不枚举任何等长路线**。

分析与依据在**同一事务**落库:依据写入失败时整体回滚,不生成本次分析。

### 复核计算(只读取依据,不重跑匹配)

- 单个接续在路段集 S 上的最小/最大里程 = 固定片段贡献 + 紧边 DAG 上的最短/最长加权路径(Kahn 拓扑序 DP,边属于 S 时权值为路段长度,否则为 0),同样不枚举路线;
- 各对接续可独立选择任一最短接续路线,全局最少/最多值为各对接续最少/最大值之和(无需路线组合);
- **换一组路段时直接复用已存依据**,不重跑匹配、不重新搜索路网;
- 旧分析没有接续依据(`has_basis=0`)时复核返回 `409`,提示在该版路网上重新匹配。

### 复核示例

```bash
curl -X POST localhost:8000/api/analyses/1/mileage-review \
  -H 'Content-Type: application/json' \
  -d '{"segment_ids": ["S1", "S2"]}'
```

```json
{
  "analysis_id": 1,
  "network_version": "v1",
  "segment_ids": ["S1", "S2"],
  "reviewed_at": "2026-01-01T08:05:00.000Z",
  "min_distance_m": 0.0,
  "max_distance_m": 200.0,
  "connectors": [
    {"seq": 0, "min_m": 0.0, "max_m": 0.0},
    {"seq": 1, "min_m": 0.0, "max_m": 200.0}
  ]
}
```

路段号必须属于该分析使用的路网版本,否则返回 `400`;路段号重复或为空返回 `422`。

## 版本语义

- 路网按版本登记,版本号全局唯一,登记后**不可变**;修订路网 = 登记新版本。
- 每次匹配显式指定 `network_version`,分析全程只读取该版本的快照,**不混用版本**;分析期间登记新版本不影响进行中的匹配。
- 分析结果连同版本号一起落库,路网修订后旧分析仍可按原版本查询(`GET /api/analyses?network_version=...`)。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/networks` | 登记带版本的有向路网(路段编号、起终点坐标、长度) |
| GET | `/api/networks` | 列出路网版本 |
| GET | `/api/networks/{version}` | 查看某版本路网(含全部路段) |
| POST | `/api/trajectories` | 提交轨迹(UTC 时刻 + 坐标,按时间排序存储) |
| GET | `/api/trajectories/{id}` | 查看轨迹 |
| POST | `/api/match` | 在指定版本上匹配,返回匹配路段、进入/离开时刻、里程、缺口,并落库接续依据 |
| GET | `/api/analyses` | 查询历史分析(可按 `network_version`、`trajectory_id` 过滤) |
| GET | `/api/analyses/{id}` | 查看分析结果(普通报告,沿用原路径) |
| POST | `/api/analyses/{id}/mileage-review` | 里程复核:传入该版路网的一组有向路段号,返回在这组路段上的最少/最多行驶米数 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 1. 登记路网 v1
curl -X POST localhost:8000/api/networks -H 'Content-Type: application/json' -d '{
  "version": "v1",
  "segments": [
    {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
     "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
    {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
     "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0}
  ]
}'

# 2. 提交轨迹(时刻必须为带时区的 UTC ISO8601)
curl -X POST localhost:8000/api/trajectories -H 'Content-Type: application/json' -d '{
  "name": "trip-1",
  "points": [
    {"timestamp": "2026-01-01T08:00:00Z", "lat": 39.9000, "lon": 116.4000},
    {"timestamp": "2026-01-01T08:00:10Z", "lat": 39.9000, "lon": 116.4008},
    {"timestamp": "2026-01-01T08:00:20Z", "lat": 39.9008, "lon": 116.4010}
  ]
}'

# 3. 匹配(轨迹 1,路网版本 v1)
curl -X POST localhost:8000/api/match -H 'Content-Type: application/json' \
  -d '{"trajectory_id": 1, "network_version": "v1"}'
```

响应示例:

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "total_points": 7,
  "matched_points": 6,
  "total_distance_m": 183.0,
  "path": [
    {"seq": 0, "segment_id": "S1", "enter_time": "2026-01-01T08:00:00.000Z",
     "leave_time": "2026-01-01T08:00:20.000Z", "distance_m": 85.0}
  ],
  "gaps": [
    {"seq": 0, "start_time": "2026-01-01T08:00:30.000Z",
     "end_time": "2026-01-01T08:00:50.000Z", "reason": "out_of_range"}
  ]
}
```

## 项目结构

```
app/
  main.py      FastAPI 路由与持久化(分析与依据同事务写入、里程复核端点)
  matcher.py   HMM + Viterbi 匹配引擎(候选、有向图最短路、紧边 DAG 接续依据、缺口检测)
  review.py    里程复核:紧边 DAG 拓扑 DP 求最少/最多里程(不枚举等长路线)
  geo.py       球面距离、点到线段投影
  db.py        SQLite 连接、建表与旧库迁移
  schemas.py   请求校验(Pydantic)
  util.py      UTC 时间处理
tests/test_api.py     端到端测试(连续路径、两类缺口、多候选全局最优、版本隔离)
tests/test_review.py  里程复核测试(等长路线区间、首尾里程、缺口不接续、事务回滚、旧分析 409)
Dockerfile / docker-compose.yml
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/app.db`(容器内 `/data/app.db`) | SQLite 数据库文件路径 |

匹配参数(`app/matcher.py` 顶部常量):吸附半径 50m、发射 σ 15m、转移衰减 β 30m、每点最大候选数 8。
