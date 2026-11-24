# 合并书目下的预约先后与副本兑现

Python 3.11 + 标准库 SQLite，无第三方依赖。全部数据合成，材料输入在
`materials/765/input.json`。

## 运行

```bash
python3.11 demo.py                      # 端到端演示，内置断言，退出码 0 即全部兑现
python3.11 demo_storybox.py            # 故事盒嗅觉/排程/交班演示，内置断言
python3.11 -m unittest tests -v         # 32 个预约台账测试（含 500 副本/1000 票据规模用例）
python3.11 -m unittest discover -v      # 再加 17 个故事盒测试，共 49 个
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

## 故事盒带读层（`library/storybox.py`）

这是在预约/取件之上新增的纯求解与现场记录层，不改动目录和台账规则；
`LibrarySystem` 初始化时会挂上 `system.storybox`，盒子资料与签约单可用同一
SQLite 连接持久化并重开。

- **按实盒号装资料**：`StoryBoxDesk.register_box` 给物理副本/实盒号登记香片、
  标签、留香拍数和有向遮蔽表；`load_by_receipt("PK1")` 或直接传 pickup 回执
  dict，会读取回执上的 `copy` 实盒号。作品在新目录修订中合并后，不同 copy
  仍可挂不同卡牌。
- **逐拍嗅觉 oracle**：`replay_record(box, record, reader=...)` /
  `StoryBoxDesk.replay(box_id, ...)` 按读者分别计算残留。规则可指定
  `(旧味, 新味, 时间差, 被遮标签)`；多个未散残留同时匹配时全部进入
  `maskings`，每条证据给出旧味来源牌、来源拍、时差和被遮提示。
  `analyze_turn` 给单拍结果，`active_residues` / `decay_report` 检查中断后
  留香仍按时差衰减。
- **两座排程**：`Page` 声明可用实片、固定拍/时窗、必读标签、禁闻/惊喜约束；
  `plan_guaranteed` / `maximum_guaranteed_seats` 穷举十拍、八牌上限内的安排，
  校验同拍同牌、换座转手拍数、小盘固定座位和唯一送片员容量。`verify_run`
  可直接核对现成的发片/转交/翻页表。
- **付费自由页**：标 `free=True`（或在 guide 中给 `free_pages`）的两拍选择
  由读者决定；求解器枚举所有选择，只有每个分支都能读通且许诺实片身份一致，
  才返回 `guaranteed`。柜台不能把它偷改成指定拍，也不能只返回有利分支。
- **两种带读法**：`mode="tray"`（小盘固定各座，场中不换盘，实际揭牌拍在还盘
  前不可知）与 `mode="delivery"`（唯一送片员每拍只到一座，且必须在揭牌当拍
  送到，不能提前放片）。`sign_booking` 返回可签的最多具名席位、各分支安排和
  每座许诺实片。
- **交班与续办**：`ReadingSession.handover_brief` / `brief_for_seat` 在小盘
  归还前只给合同、时窗、许诺实片和可能世界，不暴露实际揭牌拍；`return_tray`
  后才缩到真实分支。现场送片用 `deliver` 追加实际时点，记录同 ID 同文重放、
  异文报 `IdempotencyConflict`，不能改拍/删除，因此不能事后抹掉已生成的残留。

`demo_storybox.py` 固化了题面样例：0 青 2 白读通；遮蔽延到第二拍后梨被遮；
另一读者锁住第 2 拍唯一白牌后甲失败；两座单读可行但合桌转手不及；甲的青页
可在 0/1 拍自由揭时，白页留第 2 拍不能保证，改第 3 拍后小盘可签两席而现场
送片同拍只能保一席；同一签单可分别从甲在 0 拍或 1 拍揭青的现场记录续办。

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
library/storybox.py 故事盒香片/遮蔽、两座排程、自由页保证、小盘/送片交班
library/system.py   门面与重开（LibrarySystem.reopen，含 storybox 柜台）
demo.py             材料严格重放 + 拆别名/环拒绝/过期重选/幂等/重开场景
demo_storybox.py    松梨遮蔽、锁牌/转手、两席自由页、小盘与现场送片交班
tests.py            unittest 测试与规模验证
test_storybox.py    故事盒气味物理、排程保证、交班隐私与回执加载测试
```
