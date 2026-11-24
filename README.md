# 合并书目下的预约先后与副本兑现

Python 3.11 + 标准库 SQLite，无第三方依赖。全部数据合成，材料输入在
`materials/765/input.json`。

## 运行

```bash
python3.11 demo.py                      # 端到端演示，内置断言，退出码 0 即全部兑现
python3.11 -m unittest tests -v         # 32 个测试（含 500 副本/1000 票据规模用例）
```

## 两个独立子系统

### 目录（`library/catalog.py`）

- 每个修订是**不可变全量快照**：稳定书目 ID、单向别名边、各副本的原始书目 ID、
  取件馆、装帧。副本 `copy_id` 跨修订稳定。
- 边终点必须存在；每书目至多一条出边；允许多对一与多跳；**含环（含自环）整批拒绝**，
  库内不留半截数据。
- 规范书目在查询时沿边多跳现算（`canonical_map`）；目录里没有读者、票据或顺序。
- 错误别名只能由后续修订“不再声明该边”拆除；任一旧修订仍可完整查询，
  成员原 ID 与每副本原始身份不被揉掉。

### 台账（`library/ledger.py`）

- 等待票据永久保留**原书目 ID**、到达序号、允许馆/装帧集合；同序号按票据 ID 固定排序。
- 挑选只在 `waiting` 票据中进行：票据原书目经**当前头修订**解析到的规范作品与副本一致、
  馆/装帧在允许集合内，取 `(arrival_seq, ticket_id)` 最早者；运营方无法跳过较早合格票。
- 保留建立即锁定当时票据、读者、馆、副本与修订；之后目录更正不改派已保留副本。
  部分唯一索引保证一票据/一副本同时至多一条未释放保留。
- 两步式 `prepare`/`commit`：准备后若头修订变化或等待版本变化（新票/提交/释放），
  提交旧准备一律拒绝；也提供一步式 `reserve`。
- 取件只兑现保留给定的票据/读者/副本，错误读者拒绝且不消耗保留；
  过期/取消先释放（票据带原序号回队），再按当前目录重新挑人，旧回执保留。
- 操作 ID + 请求指纹：同文重放原样返回，异文抛 `IdempotencyConflict` 且不写库；
  业务拒绝落 `rejected` 回执，失败不产生部分占用。

## 材料场景的兑现结果

| 操作 | 结果 |
| --- | --- |
| P1（v1 预览 C1） | 命中 TA；TB 要 WB/精装，v1 下与 C1 不同作品 |
| 切 v2（WB→WA 合并） | P1 随即失效 |
| J1（提交旧预览） | `stale_prep_catalog_changed` 拒绝 |
| J2（v2 保留 C1 北馆精装） | 给更早的 **TB**（seq 1）而非 TA（seq 2） |
| J3（v2 保留 C2 平装） | TB 装帧不匹配，给 **TA** |
| J4（RA 取 C3） | `reader_mismatch` 拒绝，H3 不被消耗 |
| J5（RC 取 C3） | 成功；合并后 C3 仍只能由 TC 的读者 RC 兑现 |

## 代码地图

```
library/db.py       SQLite schema、显式事务、操作指纹、回执与版本计数器
library/catalog.py  修订快照、边校验/环检测、多跳规范解析、拆别名、目录查询
library/ledger.py   票据、prepare/commit、reserve、pickup、release、公开查询
library/system.py   门面与重开（LibrarySystem.reopen）
demo.py             材料严格重放 + 拆别名/环拒绝/过期重选/幂等/重开场景
tests.py            unittest 测试与规模验证

knitroom/           针织班：版师（pattern）与缝台（sewing），同库独立分表
  engine.py         针织机：平织/二并一/一针增一针、具名线圈谱系、边弧与标记
  schema.py         册版、缝边记录、实针记录、线轴占用的 SQLite 表
  pattern.py        版师：读台账真实取件回执，按取件当时册版出缝边记录
  sewing.py         缝台：余下针序、2对1吃势、临缝/定针/装饰线、改肩与交货
  scenario.py       题面图解册版工厂（≤8 行、每行 ≤12 针）与台账装载
  system.py         KnitRoom 门面（KnitRoom.open / KnitRoom.reopen）
knit_demo.py        针织班端到端演示（J5 册版锁定、四组针序、改肩、重开找回）
knit_tests.py       26 个针织班单元/集成测试
```

## 针织班：版师与缝台（`knitroom/`）

复用同一 SQLite 库与同一套幂等/回执约定，但与目录、台账物理分表。

### 版师（`pattern.PatternService`）

- 从 `library.ledger` 的**已兑现取件回执**读实际样本（如 J5：RC 取到 C3），
  未兑现（held）的保留不能出缝边记录。
- 图解册版是不可变全量快照；勘误/再版只追加新版本并切 `knit_chart_head` 指针。
  缝边记录锁定取件当时的 `revision` 与 `edition` 并整块保存边谱快照：
  之后更正同书勘误或目录，重开仍能按 J5 的册版找回那一针和实际余弧；
  **只有更正之后新兑现的借件**才取得到更正版。
- 同名作品原书目（`book_id`）与本册已画记号（`knit_copy_marks`）原样保留。
- 三种动作：`knit` 平织、`dec` 二并一（两个父圈可追溯）、
  `inc` 一针增一针（一个父圈分出两个具名子圈，可追溯）。
- 边针何时从两端收掉由册版 `edge_dec_end`（left/right/None）决定；
  标记锚圈的延续链一旦被边端二并一收掉，标记即消失（`marker_gone`）。
- 本地短行：`arcs_between(seam, m1, m2)` 查两标记间剩余边弧段数，
  `marker_alive(seam, m)` 查标记是否已随收针消失；
  边圈谱 `edge_loop_ledger` 与缝台实针 `needle_log` 可分别单独查。

### 缝台（`sewing.SewingService`）

- 从腋下起点顺序合拢；袖山与衣身同名定位标方向（orientation）必须相配，
  不可翻面（`marker_orientation_mismatch`），不得跨标记跳缝。
- 一次缝合取两侧各一段（1 对 1），或仅在准许吃势的**具名区**用同一针缝
  两段袖弧对一段身弧（2 对 1），每区至多一次吃势。
- 临缝用专用线轴：一次占用到本次交货前不能再借给另一袖
  （`baste_thread_reserved_until_delivery`），拆临缝后才释放；
  定针占用不可释放，缝入袖圈即使同名也不能拆后当原圈复用，后续针序只从断点继续。
- 共用的一段装饰标线只够一只袖（`decorative_thread_one_sleeve_only`）。
- 两种接单方式同一套可复用功能：`baste_sleeve`（整袖临缝、试衣后拆临缝定针）
  与 `place_permanent`（腋下直接落定针，`stop_after` 留活口）。
- 改肩宽 `evaluate_shoulder_change`：肩标前末弧一段改两段，只有留在肩标前的
  吃势位能吸收；定针已缝住末弧报 `permanent_needle_blocks_change`（带针序），
  吃势已耗报 `ease_already_consumed`（带被耗的针序）。
- `delivery_report` 报能按时完成的袖对、已占用的线；交不齐一对时指出
  耗掉的临缝占用或锁定袖圈的那一针。

### 题面固定肩宽案例的针序结果

| 情形 | 袖 AXL→SHD | 衣身 | 吃势 | 结果 |
| --- | --- | --- | --- | --- |
| 原图（一次增加） | 3 段 | 2 段 | 区内 1 次 | 2对1 + 1对1，可合 |
| 改连续两次增加 | 4 段 | 2 段 | 区内 1 次 | `cannot_align`，一次吃势不够 |
| 恢复原图、取消吃势 | 3 段 | 2 段 | 无 | `cannot_align` |
| 吃势许可移到肩标外 | 3 段 | 2 段 | 仅 SHD 外 | `cannot_align` |
| 同名再版改收针端 | 旧袖按原册查 | — | — | 旧标记/旧针原样，新借件用新版 |

运行：

```bash
python3.11 knit_demo.py                  # 针织班端到端演示，退出码 0 即全部兑现
python3.11 -m unittest knit_tests -v     # 26 个针织班测试
python3.11 -m unittest tests knit_tests  # 原仓 32 个 + 针织班 26 个一起跑
```
