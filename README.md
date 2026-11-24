# 合并书目下的预约先后与副本兑现

Python 3.11 + 标准库 SQLite，无第三方依赖。全部数据合成，材料输入在
`materials/765/input.json`；方言亲称/联名柬业务材料在
`materials/invitation/input.json`（全部人物为虚构）。

## 运行

```bash
python3.11 demo.py                      # 借书端到端演示，内置断言，退出码 0 即全部兑现
python3.11 demo_invitation.py           # 亲称/柬房五幕验收演示
python3.11 -m unittest tests -v         # 32 个借书测试（含 500 副本/1000 票据规模用例）
python3.11 -m unittest tests_invitation # 20 个请柬/亲称验收测试
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
library/db.py         SQLite schema、显式事务、操作指纹、回执与版本计数器
library/catalog.py    修订快照、边校验/环检测、多跳规范解析、拆别名、目录查询
library/ledger.py     票据、prepare/commit、reserve、pickup、release、公开查询
library/kinship.py    亲称词册、候选家系、口述语料、证言、可能世界认知引擎
library/invitation.py 柬单、字模/字盘、印位班次、local/sealed 两种印寄、隐私闸
library/scenarios.py  请柬材料装载与编程式造场景
library/system.py     门面与重开（LibrarySystem.reopen）
demo.py               借书材料严格重放 + 拆别名/环拒绝/过期重选/幂等/重开
demo_invitation.py    请柬五幕验收（普通柬/相反隐私谱/顺序字盘泄密/同日两信/基础规则）
tests.py              借书 unittest 与规模验证
tests_invitation.py   请柬 unittest 验收
```

## 亲称词册与联名柬（`kinship` + `invitation`）

### 与借书系统的衔接

- 方言词册仍走 `catalog`：村志版本是 `revision_copies` 里的副本，书目可经别名边
  归并；但**词义只按真实取件回执上的副本号 `copy_id` 读取**（`kin_copy_terms`），
  归并不揉掉“实际取走的村志版本”。
- 复用同一套 SQLite 连接、操作 ID 幂等指纹与回执表；柬单号、素封编号、交接日志
  与全部回执持久化，`reopen` 后可按单号反复办理新柬。

### 家系与语料（`library/kinship.py`）

- 候选家系是**有限具名备选**：父/母各至多一人（单字段保证），含环（含自环）
  整批拒绝——选边不能令某人成为自己的祖先；谱外长辈父母留空、不参与备选关系。
- 路径语言：`[["up","father"],["sib","elder"]]` 表示“父的姐”；`sib` 可带
  `elder/younger`，旁支称呼由同一对父母与已登记 `seniority` 长幼顺序支持。
- 每条口述登记说话人、所指、村别与该册容许的一组关系路径（如父的姐或母的姐）。
- 证言分持钥访谈员/礼房两个 holder：实名证言把可能世界收缩为所指家系；
  `tenable_trees` 按岗位可见证据现算“仍不能排除的家系”，`divergent_trees`
  给出两份有分歧的具体家系。

### 柬房与隐私闸（`library/invitation.py`）

- 一张柬一行称呼，落款 1–8 人、待发至多 6 张；词只能从在架字模挑，且须对**全部
  落款人 × 礼房每份仍成立家系**都语义正确（`safe_terms_for_card`），否则只能暂存。
- 收件人各收一次；两人必须联名给同一收件人的约定在登记时校验。
- 两种办理：
  - **local（礼房离线印寄）**：看得到经手封面，普通印台；保密约定下敏感封面拒印。
  - **sealed（访谈员有钥位封缄直寄）**：有钥印位一班一封、普通印台不能代用；
    礼房只见素封编号 + 公开状态 + 班次，封面全量仅持钥岗位可核对。
- 隐私闸：已登记带 `side`（父系/母系）的家系即启用。礼房累积交接材料（含旧回执）
  后两种身份都不能被排除——配词要求两侧安全词强不可区分；顺序/字盘等单项资料
  各自放行可过、联合放行若排除某身份即拒（`privacy_leak_single_item`）。
- 角色化查询：`interviewer_mailings` 给持钥岗位逐封落款/收件人/称呼；
  `li_fang_mailings` 与 `li_fang_receipts` 对封缄件只出素封投影；
  `public_mail_status` 是准许公开的寄件状态。
- `run_batch` 以截止前能安全寄出的最多具名信件交单，并保留已寄编号与本岗位
  还能接哪封的记录（`keyed_capacity`）。
