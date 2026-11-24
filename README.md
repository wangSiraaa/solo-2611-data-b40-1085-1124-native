# 独立推车轮廓目录与连续动作核查库

仅处理**合成平面轨迹**：轴对齐矩形轮廓（正有理数半宽/半高），姿态固定，
推车中心随时间分段线性平移。不做旋转、加速度、避障、自动改轨，不控制
任何实体设备。所有数值按精确有理数（`fractions.Fraction`）处理，拒绝
二进制浮点与非法输入。

## 严格合同

两辆车在时刻 t 有**正面积重叠**，当且仅当两个方向同时严格满足：

- `|x_a(t) - x_b(t)| < half_width_a  + half_width_b`
- `|y_a(t) - y_b(t)| < half_height_a + half_height_b`

只碰边（一个方向取等号）或只碰角（两个方向取等号）**不算重叠**，根点
一律按开端点排除。核查域是半开区间 `[h0, h1)`，每辆轨迹必须恰好覆盖
该域（首结点 = h0、末结点 = h1），无外推、缺段、瞬移；时间结点严格递增。

## 模块

| 文件 | 职责 |
| --- | --- |
| `cart_audit/rational.py` | 精确有理数解析；JSON 浮点字面量直接拒绝 |
| `cart_audit/models.py` | `Profile / Knot / Trajectory / MotionSegment` 与合法性校验 |
| `cart_audit/intervals.py` | 带开闭端点的时间区间、求交、同对区间并集合并 |
| `cart_audit/catalog.py` | SQLite 版本化轮廓目录 + 当前轨迹存储（修订冻结、异文拒绝） |
| `cart_audit/service.py` | **独立轮廓服务**：单对线性动作段精确求交，无采样、无浮点 |
| `cart_audit/library.py` | 共同细分核查、证据保存、批次报告、幂等与失败不发布 |
| `cart_audit/verifier.py` | 用报告冻结快照独立重算并比对（篡改可检出） |
| `cart_audit/runner.py` | `operations.jsonl` 操作流（audit / revise_profiles / read_report） |
| `cart_audit/exhibit.py` | **展览地面保洁交接**：区/廊/场单、色粉互沾精确推演、实物巾调度、整窗交接判定与领队签收 |
| `cart_audit/exhibit_store.py` | 交接报告冻结存储、同号幂等/异文拒绝、旧轨首次来源与套号查询、独立复核 |
| `demo.py` | materials/728 主场景 + 六类边界场景端到端演示 |
| `demo_exhibit.py` | 四区两刷三场 + 东西廊换道 + 甲/乙三变体等保洁交接端到端演示 |
| `tests/test_contract.py` | 27 个契约级测试（含 20 车 × 100 段规模用例） |
| `tests/test_exhibit.py` | 22 个保洁交接契约测试（全部题面点名反例） |

## 核查方法

按车对逐段归并（题目允许的两种方案之一；扫描线候选筛选不改变严格接触
合同，本实现选择在稀疏轨迹下边界组织更直接的共同细分方案）：

1. 取两车落在请求时段内的全部时间结点并集，形成共同细分网格；
2. 对每个细分 cell 各定位一辆车的一个线性动作段，调用轮廓服务精确求交，
   结果再与半开 cell `[u, v)` 裁剪，防止越出请求时段；
3. 合并同对车重复/相邻区间（两端皆开的共享点不合并），累计重叠时长；
4. 每个合并区间保存贡献它的 cell、两车动作段下标与原始区间作为可独立
   复核的证据。

完全静止、零相对速度（同速恒距）走 `slope = 0` 精确分支；只在一个方向
接触时该方向解集为空，整体即不冲突。不使用固定采样步长，也不凭端点
分离判无冲突——A/B 两端分离但途中 `(4, 6)` 重叠即材料中的反例。

## 报告与版本

- 报告保存：轮廓版本及完整修订载荷、轨迹快照、冲突区间（含开闭端点）、
  每对累计重叠时长、可通过结论、逐 cell 证据、输入指纹与 UTC 时间。
- 同 `report_id` 同内容复读返回原报告；异文（不同车对、不同修订或不同
  快照）抛 `AuditRejected`，绝不覆盖。
- 轮廓修订只影响新报告；旧报告的快照保留原尺寸，`read_report` 与
  `verify_report` 均按冻结内容工作。
- 核查失败抛业务异常、整笔回滚并在 `audit_attempt` 留痕，`report` 表
  不产生任何“可通过”外观记录。

## 运行

```bash
python3 -m unittest tests.test_contract -v   # 27 个测试
python3 demo.py                               # 端到端演示，生成 cart_audit_demo.sqlite3

python3 -m unittest tests.test_exhibit -v    # 22 个保洁交接契约测试
python3 demo_exhibit.py                       # 保洁交接演示，生成 cart_audit_exhibit_demo.sqlite3
```

主场景（materials/728）手算对应关系：

- r1：A/B 横向中心差 `|10 - 2t| < 2` ⇒ `(4, 6)` 两端开，时长 2；
  A/C 的 y 向恒距 3 > 半高和 2，不冲突；
- r2（half_height 1→2）：A/C 半高和 4 > 3，整个 `[0, 10)` 冲突，时长 10；
  R1 仍保留 half_height=1 的原结论。

## 独立查询接口

- `library.query_profile(revision, profile_id)`：查冻结轮廓尺寸；
- `library.query_pair_segment(revision, a, b, seg_a, seg_b)`：一对单段
  在共同线性时段上的精确重叠；
- `library.audit_pair(revision, a, b, lo?, hi?)`：整车对（或子时段）核查；
- `library.read_report(report_id)` / `verify_report(library, report_id)`：
  读取整批车对结论、来源证据并独立复核。

## 展览地面保洁交接（`exhibit.py` / `exhibit_store.py`）

在同一套精确有理数与严格正面积合同上，为离线动作回放增加的地面保洁交接层。
全部判定都是**整段时间窗**判定，不做逐拍采样。

### 领域规则与实现位置

| 题面规则 | 实现 |
| --- | --- |
| 固定矩形分区；刷头是已有轮廓的矩形 | `Zone`（自带 `Profile`/静止段）+ 既有 `ProfileOverlapService`，接触区间即 `brush_zone_intervals` |
| 只贴边不沾 | 与车对核查同一条严格不等式合同，触边根点排除；有实轨触边零互沾示例 |
| 连续正面积接触累计达到“该区规定时长”，第一次达到时互沾双方全部现色 | 每个合并接触 episode 求 `τ = episode.left + zone.required`，`τ <= right` 才办（恰好够整拍也算） |
| 同次接触只办一次；离开重进重计 | 一个合并区间 = 一个 episode，只产生一个到期事件；断开后的新区间另计 |
| 跨断点未分开的接触接着算 | 共同细分 + `merge_union` 把跨结点相邻区间并回一个 episode（有专测） |
| 同一时刻数笔互沾，全部从该拍开始前颜色结算 | 到期事件按时刻分组，先对组内刷/区颜色做快照，再统一求并集落账；禁止链式传色 |
| 换刷套的实际时点清刷、套号 +1、截断此前接触 | `BrushPlan.sleeve_changes`；接触区间先在换套点切分再计 episode |
| 吸附巾有实物号、同块全局一次、擦一次清一区 | `Wipe(wipe_id, zone_id, t)`；`Desk.inventory/used_ids`，重复号直接拒绝；`external_consumed` 表示已借别厅 |
| 清扫岗对旧版轨迹查每区首次来源与套号 | 每次受色写 `provenance[zone][color] = {t, brush, sleeve}`，首条不被后续擦净/再沾改写；`ExhibitLibrary.provenance(report_id)` 读冻结旧轨 |
| 固定路线场从交给领队到离场整条路合格 | `SimResult.is_clean(z, color, open_t, depart_t)`，左闭右开整窗检查 |
| 多个场次争同一块巾；前场实巾/路线不撤销 | `Desk.run_all` 按场单次序占号、`committed_wipes` 只增；稀缺巾有专测与演示 |
| 指定等候区换一次、停足交接时长 | `SessionSheet(waiting_zone, arrive_waiting, handover, …)` |
| 旧廊保洁到交接结束；新廊从交接开始干净；交接期间两侧禁擦 | `judge_switch`：旧廊窗 `[open, t_arr+d)`、新廊窗 `[t_arr, depart)`、禁擦点严格落在 `(t_arr, t_arr+d)`；恰在两端的擦巾按截点场单次序先办，不违规 |
| 可选事先保整场干净的路，或等实际条件满足再换道 | 前者 `Desk.run_fixed`（`strategy="secure_whole_route"`，子集搜索允许擦点）；后者 `Desk.run_switch` + `earliest_switch_start`（只在廊区状态变化点上找最早可行 `s*`） |
| 只在门口交新图、仍有人处脏走廊按失败 | `Desk.sign_off` 用当前已承诺巾**重新推演**复核整窗；不合格/原决策失败都出 `verdict="failed"` 留痕 |

判定办法（“由你决定怎么判断交接能否兑现”）：把实轨、换套点、真实擦巾与
调度选中的擦巾合并成一条确定时间线，精确求出所有接触 episode 与到期时刻，
按截点次序（换套→擦巾→成组互沾）结算颜色；随后所有路线/换道问题都归约为
“某区在某个左闭右开窗内是否始终不含禁色”。洁净状态只在时间线变化点改变，
因此换道的最早可行起点只需在这些变化点上枚举。规划器在“场单允许的（区, 时点）
格”里按用巾数从少到多做确定性子集搜索，并按实物号序派号。

### 题面三个点名场面的结论（`python3 demo_exhibit.py` 复现）

- **四区两刷三场（只用红/蓝本地标签）**：刷1 自带一粒红，t=1 染 甲、
  t=7/2 染 乙；刷2 从展前铺蓝的 丙 取蓝，t=11/2 染 丁。场1 `[4,6)` 禁红时
  原始回放两条廊都不合格（共用入口 甲已红）；把 **巾01 占住 甲@3**（开放前
  擦点，同次接触不重办）后可交 **甲→丙→丁**。场2 用 **巾02 占住 丁@6** 走
  **甲→乙→丁**；场3 继承丁的持久洁净，不耗新巾仍走 甲→乙→丁——这就是
  “给后场留下的路”。若全台只有一块巾，场1 领走后场2、场3 都交不成。
- **东西廊反例**：东廊干净到 t=3 前、西廊 t=3 才擦净、队伍 t=2 到等候区、
  交接需一拍。朴素逐拍在 t=2 找得到东廊、t=3 找得到西廊，但连续窗
  `[2,3)` 要求新廊从 t=2 干净，故换道不成立、`s*=None`。把西廊真实擦净提前
  到 **t=2 的进场前动作**（截点先擦后算）并维持到离场，`[2,3)` 才成立。
  `EW-甲轨`/`EW-乙轨` 两份冻结报告的 X 时间线分别是
  `[0,红],[3,空]` 与 `[0,红],[2,空]`，领队签收分别为 `failed`/`switched`，
  独立复核均 `verified=true`。
- **单通道过乙（甲有红粉）**：原轨 甲@1、乙@2 够拍传色；开场前 乙@3 一巾后
  `[4,6)` 合格、签收 `handed_over`。把该巾提前借别厅（`external_consumed`）
  则无巾可派、交不成；补正轨迹为擦后刷重进乙且再够一拍，新 episode 在
  `15/2` 再染乙，t=8 开场又红，仍按失败签收。
- **触边未沾**：刷与区半尺寸和为 2，实轨在 t=5 中心距恰为 2，接触区间为空、
  零互沾。**擦源改远**：甲源区在到期前 t=1/2 被擦，刷全程空色，远端 丙
  原定 t=3 受红改为不受色。

### 冻结报告

`ExhibitLibrary.publish(report_id, payload)` 用 `build_payload(...)` 冻结：
小厅/通路/两标签、horizon、各刷轨迹快照与轮廓、初始粉、刷套计划、实物巾册、
场外消费、**实轨已发生擦巾** `actual_wipes`、场单与签收请求。同号同 payload
幂等返回，异文抛 `ExhibitRejected`；`verify_exhibit` 只从冻结 payload 重算并
逐字节比对结果。交接失败也会发布 `ok=false` 报告作为失败证据（与核查库
“失败不产生可通过报告”不矛盾：这里留下的是失败签收，而不是可交接外观）。

与旧目录/轨迹存储衔接时，轨迹和轮廓不必另造：

```python
traj = store.get_trajectory(cart_id)                 # MotionStore 的旧版/当前轨迹
prof = catalog.get_profile(revision, traj.profile_id) # 指定修订的冻结轮廓
payload = build_payload(..., trajectories={cart_id: traj},
                        brush_profiles={cart_id: prof})
ExhibitLibrary(conn=same_conn).publish("EX-1", payload)
ExhibitLibrary(conn=same_conn).provenance("EX-1")     # 清扫岗：每区首次来源+套号
```

## 存储

SQLite 表：`profile_revision / profile / horizon / cart / trajectory_knot`
（目录与轨迹），`report / report_pair / conflict / conflict_evidence /
audit_attempt`（报告与留痕）。有理数以规范分数字符串 `p/q` 落库，读出
还原为 `Fraction`，全程无浮点。
