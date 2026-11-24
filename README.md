# 可修订加权记录源、可重复加权采样与试饮柜接单

本仓库在两个基础模块之上实现试饮小铺的接单、偏好推断与发包规划：

- `python -m wrsource.source`：SQLite 支撑的可修订加权记录源，稳定 `row_id`、
  分组、权重 1..100、正文、递增修订号、不可变源版本快照与固定版本分页。
- `python -m wrsource.dataset`：可重复的加权采样数据集生产器，全量构建、按变更
  清单增量刷新、真实 JSONL + manifest 输出与对照源 API 的校验。
- `wrsource/receipts.py`：试购小票正文模式与**偏好台**。只允许采样器实际选中的
  小票作证，回答“两个品种谁必在前、是否都有可能、有没有成环的矛盾票据”。
- `wrsource/cabinet.py`：试饮日发包柜。整箱上架、按到场次序取包，支持普通开柜、
  开门前实名保底分包、公开货架 + 隔壁补货权两种开店方式，对所有相容偏好做鲁棒
  兑现规划，输出最便宜全兑单或每种可承担单子的真实失约经过。
- `wrsource/ledger.py`：接单/取包台账（追加式 SQLite）。重开昨日单号即可独立复读
  当时入样的票与实际交出的包；事后勘误若与已发包矛盾，只记矛盾事件，不改取货。

另一个模块 `wrsource/sampling.py` 只负责**精确**抽样键算术与选择；`wrsource/reference.py`
是测试用的小集合独立参考实现。没有模型训练，也没有人工标注界面。

## 抽样规则（题目定义，照此实现）

对每个活跃行、给定 256 位 seed：

```
h = SHA-256(seed[32] || uint64_be(len(utf8(row_id))) || utf8(row_id))
a = int(h) + 1                         # u = a / D,  D = 2^256 + 1
priority = u ** (1 / weight)
```

按 priority 降序选 `quota` 行，完全相等时按 `row_id` 码点序。`quota <= 0` 给空
样本，活跃行少于 quota 时全选。不同 seed 是不同请求。抽签键只绑定
`(seed, row_id)`，因此正文修改、改组、移动分片都不会重抽该行的键；权重修改会
改变该行优先级。

### 精确比较（不能先转 float）

两个优先级的比较等价于整数比较：

```
p1 > p2  <=>  (a1/D)^(1/w1) > (a2/D)^(1/w2)
         <=>  a1^w2 * D^w1  >  a2^w1 * D^w2        (两边同取 w1*w2 次幂)
```

实现采用三级比较器（`sampling.compare_priority`）：

1. **float 对数筛选**：仅当 `|log p1 - log p2| > 1e-9` 时直接判定。阈值远大于
   `math.log` 在 `[1, 2^256]` 上的实际误差，只会给出必然正确的结论。
2. **60 位 Decimal 对数**：误差约 `1e-55`，仅当差值 `> 1e-50` 时判定。
3. **精确整数幂交叉比较**：`a1^w2 * D^w1` 与 `a2^w1 * D^w2` 直接比大整数，作为
   最终裁决。返回 0 只表示**实数意义下精确相等**，此时再按 `row_id` 码点序。

因此极近键不会被 float 压成相等。测试构造了 `a` 仅差 1、对数在 float 上完全
相等的情形，精确路径仍能区分；并用 300 位 Decimal 对随机与近邻键做交叉校验。
manifest 为每行保存 `hash_hex`、`a`、`weight`，并为每个超额组保存 `cut` 边界
证书（`a_sel^w_cut * D^w_sel` 与 `a_cut^w_sel * D^w_cut` 两个大整数），可离线
复核比较，无需相信浮点快路。

## 源服务设计

### 表结构

- `rows`：每行的**当前**可变状态，含稳定 `row_id`、group、weight、正文摘要、
  `rev`、`active`、`shard`。`row_id` 一旦分配终身不变。
- `bodies`：内容寻址、只追加的正文库。旧版本正文永不回收（本实现不清理），因此
  每个已发布源版本都保留**真实可读取**的正文。
- `versions` / `version_rows`：不可变整版快照。快照只存 group、weight、正文摘要、
  `rev`，**不存 shard**。
- `batch_log`：原始批次与结果，用于审计。

### 事务与修订

一个批次的每个操作先在内存中折叠为**每行最终状态**，然后在一个
`BEGIN IMMEDIATE` 事务里写当前状态、必要时发布新快照；任何错误整体回滚。因此
“批内按最终状态原子提交”。

- 逻辑内容（active/group/weight/body 摘要）改变 → `rev += 1`，并发布新源版本。
  同一批次对同一行多次修改只算一次修订。
- 只改 `shard`（物理位置）→ `rev` 不变，**不发布新版本**，旧的固定版本内容不变。
  这正是“分片只是物理存放位置，同一逻辑版本内改变分片不改变行身份或内容”。
- 批内先 `add` 后 `retract` 的新行视为从未存在（返回 `discarded_row_ids`）；
  先撤回后新增是合法的“最终状态为活跃”。

### 固定版本分页

分页键是 `(version_id, row_id)`，按 `row_id` 的 UTF-8 字节序（与码点序一致）。
因为版本是不可变快照，读者持有旧版本游标时，写者发布新版本不会让分页漏行或重复。
游标编码了版本号，用错版本会被拒绝。读者在分页过程中穿插写入当前状态，读取的仍
是同一快照。跨分片的行按 `row_id` 统一排序，分片不参与行身份或内容。

### 为何选 SQLite

单文件、WAL 并发读者/单写者、事务与 `PRIMARY KEY` 唯一性天然满足“稳定 row_id +
原子批次 + 不可变版本”。10 万行量级写入约 2s，整版快照读取与分页都在亚秒到数秒
级，远低于题目规模。

## 数据集生产器设计

### 合并：每分片产出全部候选，父进程按组统一选择

- 从**一次一致元数据读取** `all_rows_meta(version)` 取得所有活跃行及其当前 shard，
  在父进程按 shard 分组后分发。即使并发移动分片，也不会漏行或重复。
- 每个 worker 只计算该分片所有行的候选（`a`、`log_screen`）并**全部返回**。
- 父进程把所有分片候选按 `group` 合并成完整候选池，再对整组执行精确 top-quota。
- **绝不拼接各分片样本，也不只保留一个全局候选数**。因此无论分片数与遍历顺序，
  合并结果与单流参考完全一致（测试用随机改分片、并行/串行对照验证）。
  `--parallel` 使用进程池；缺省单进程。二者输出逐字节相同。

### 扫描 vs 候选持久索引

本实现对**需要的组**做一次全量扫描并精确排序，不在数据库里持久化全局候选顺序
索引。理由：

- 抽签键依赖 seed 与权重；权重可变，任何持久索引都要按 `(seed, weight)` 失效，
  维护成本高且易与源版本不一致。
- 当前规模（10 万行、每组 quota ≤ 1000）下，整库精确排序在亚秒到数秒级，重扫
  受影响组足够快。
- 重扫可证明与单流参考一致，避免持久索引的陈旧风险。

若规模继续增长，可缓存 `(row_id, seed) -> a`（与正文/分片无关，仅在 seed 变化时
失效），并用每组堆/树维护 top-quota；权重变化仍要求对应组重排序。当前实现刻意
选择简单且可验证的方案。

### 精确比较缓存

`Candidate` 缓存 `a`（精确键）与 `log_screen`（对数快路值），排序时大量比较走
O(1) 的 float 快路，只在极近时回退 Decimal 与大整数幂。`cut` 证书只在 manifest
生成时对每组边界计算一次，把昂贵的大整数运算限制在真正的边界与极近键上。

### 增量刷新与跨组修订

`refresh` 计算 `diff_versions(old, new)`，得到 added/removed/changed 行及其
**旧组与新组**。受影响的组（新增、撤回、改组的新旧两组、改权、改正文，以及 quota
变化的组）全部在**新版本上完整重扫**；未受影响组复用旧选择。因为未受影响组的行、
权重、分组、正文、quota 都没变，复用与重算在数学上完全相等。

关键点：撤回样本内一行时，是在**新版本的完整组候选**上重新取 top-quota，补入的
是真正的下一名，而不是从旧样本里重抽。改组同时进入旧组和新组的受影响集合，名额
在两组间正确迁移。测试覆盖：撤回首名后与同版本全量重建逐字节相同；改组后两组分别
与独立参考一致；增量刷新与同版本全量构建的 manifest 和 JSONL 完全相同。

### 输出内容

`samples.jsonl` 每行含：`group`、`rank`、`row_id`、`weight`、`hash_hex`、`a`、
`log_screen`、`body_sha256`、`body_bytes`、`source_version`、`seed_hex`、**真实
`body`**。`manifest.json` 含 `format`、`source_version`、`seed_hex`、`quotas`、
`totals`、`records_file`、`records_sha256`、以及每组 `eligible`、`quota`、
`selected_count`、`selected`（带精确比较材料）与 `cut` 边界证书。

`verify` 会重算 `records_sha256`、逐行回查源 API 的正文与摘要、按 seed 重算
`hash_hex`，并做一次同版本全量重建比对身份、顺序与真实正文。因此**每行都从源
API 回查验证**，而不是只写抽样元数据。

## 试饮小票与偏好台（`wrsource/receipts.py`）

小票就是加权源行的 JSON 正文：

```json
{"buyer": "小林", "visible": ["甲", "乙"], "picked": "甲"}
```

每位虚构买家有一份固定但未知的**严格品种次序**，总挑当时可见品种里最喜欢的一款，
因此一张票给出约束 `picked > v`（对所有其他可见品种）。偏好台只从数据集
`samples.jsonl` 的入样记录构建（`PreferenceDesk.from_records`），源里未入样的行
永远不作证。

- 约束按买家分别保存，并合并成一张台面总图。`must_precede / can_precede /
  relation` 默认回答总图，也可传 `buyer=` 只看某人的证据。
- `find_cycle / cycle_evidence` 用 DFS 找有向环并给出每条边的小票 `row_id`
  出处；单张票自身永远可满足，多张票合并后才可能成环。
- `possible_picks(buyer, options)` 给试饮柜使用：在某买家的约束下，当前货架上
  哪些品种**有可能**成为他的首选（等价于“该品种压过其余选项”这组附加边仍可
  拓扑排序）。
- `witness_order / compatible_orders` 给出可满足的严格次序实例，供测试与复读。

## 试饮柜（`wrsource/cabinet.py`）

静态输入：品种表、若干真实封箱（每箱品种数量与价钱固定）、每位买家的可见货架、
到场次序与可接受清单、总预算。开门前只能整箱上架；开门后买家按次序取一包。公共
货架只能因真实交包（或补货到达）而变化，后来的预约不能临时藏货。

三种 `mode`，后两种与题目两种开店方式对应，且共用同一预算、可接受清单与可见记录：

- `plain`：无保底分包、无补货权。
- `reserve`：开门前可按预约从整箱分出实名保底包，每包收一次分包工费；保底包离开
  公共货架，只能在该买家到场时交给他，其余人从公共余量挑。
- `restock`：维持公开货架，可持有具名品种补货权。每份权只能叫一次，**在某买家
  取包之后才能叫，下一位挑完后才到**（`restock_arrival_delay >= 2`，规划器强制），
  因此永远救不了当前步或紧接着的下一位；买权收订补费，叫权收执行费，已发包不可
  撤销。

规划器 `CabinetEngine.evaluate()` 枚举封箱子集（题目规模至多六箱，共 64 种），对
每种箱单跑对抗式记忆化 DFS：每个买家当前可见、可接受且在架的品种，经偏好台
`possible_picks` 收窄后，**每一个相容首选都必须能走完当天**；补货模式下店铺在取包
后选择“不叫 / 叫一份权”的最便宜鲁棒动作。报告：

- 能对所有相容偏好全兑的箱单按总成本升序，`cheapest` 即最低总成本（箱价 + 分包工费
  + 订补费 + 最坏分支保证执行费）；
- 每个预算内但不能保证全兑的箱单附一段真实失约经过 `witness`：逐步货架、可选项、
  实际取走的包 ID、叫权记录与 `first_failed_buyer`。
- `execute(plan, orders)` 用固定严格次序跑一条真实当日线，返回可落盘的
  `Handover`；无法兑现时抛 `CabinetFailure` 并携带此前全部交包。

## 接单与取包台账（`wrsource/ledger.py`）

追加式 SQLite，两张主表：

- `orders / order_tickets`：接单时冻结数据集目录、源版本、seed、名额与**当时实际
  入样的小票**（含正文摘要），柜台据此接新箱单；
- `events`：只追加的时间线，含 `pickup`（具体包 ID）、`correction`（事后勘误）、
  `contradiction`（新证据与已发包冲突）、`failure`、`note`。

勘误与已发货记录意见相反时，`record_correction(..., contradicts_pack=...)` 追加一条
不可变矛盾事件，**不改写任何取货**。`reopen_order(order_id)` 不依赖当前源内容，
独立返回昨日用的票与实际交出的包。CLI：

```bash
python -m wrsource.ledger orders --db ledger.sqlite
python -m wrsource.ledger reopen --db ledger.sqlite --order-id 1
```

## 代码结构

```
wrsource/
  sampling.py   精确键、比较器、top-quota、边界证书（无 I/O）
  source.py     版本化源服务 + 分页 + CLI（可独立运行）
  dataset.py    全量构建 / 增量刷新 / 校验 + CLI（可独立运行）
  receipts.py   小票模式、偏好台（必在前/都可能/成环、相容首选）
  cabinet.py    封箱与买家模型、两种开店方式、鲁棒规划、失约经过、执行回放
  ledger.py     追加式接单/取包台账 + 矛盾留痕 + CLI
  reference.py  测试用独立精确参考实现
tests/
  test_sampling.py    键构造、双向一致性、极近键、边界证书、参考对照
  test_source.py      原子批次、折叠、修订号、分片移动、分页并发、重启
  test_dataset.py     全量/参考对照、零/超名额、正文版本、分片不变性、
                      并行一致、撤回首名补位、改组、增量==全量、篡改检测、重开
  test_receipts.py    小票校验、偏好问答、成环与逐买家隔离、相容次序
  test_cabinet.py     普通/保底/补货三方式、鲁棒对抗分支、费用、失约经过、规模界
  test_shop_ledger.py 接单重开、追加顺序、矛盾不改取货、跨进程持久化
  test_shop_flow.py   源->采样->偏好台->柜->台账端到端、勘误、撤回补位、两种开店
demo/run_demo.py          原有源 + 采样端到端演示
demo/run_shop_demo.py     试饮柜两场验收演示（落盘 samples 与发放清单）
```

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 试饮柜演示（两场验收，产物在 demo/_shop）
python3 demo/run_shop_demo.py

# 原有演示（小规模 / 10 万行）
python3 demo/run_demo.py
python3 demo/run_demo.py --large --parallel

# 源服务 CLI
python3 -m wrsource.source init --db src.sqlite
python3 -m wrsource.source batch --db src.sqlite --ops ops.json
python3 -m wrsource.source versions --db src.sqlite
python3 -m wrsource.source page --db src.sqlite --version 1 --limit 100
python3 -m wrsource.source page --db src.sqlite --version 1 --cursor <CURSOR>
python3 -m wrsource.source diff --db src.sqlite --old 1 --new 2
python3 -m wrsource.source verify-bodies --db src.sqlite --version 1
python3 -m wrsource.source dump --db src.sqlite --version 1 --out dump.jsonl

# 数据集 CLI
python3 -m wrsource.dataset build --db src.sqlite --version 1 \
    --seed-hex <64 hex> --quota g0=1000 --quota g1=1000 --out ds_v1
python3 -m wrsource.dataset refresh --db src.sqlite --old ds_v1 \
    --new-version 2 --out ds_v2
python3 -m wrsource.dataset verify --db src.sqlite --dataset ds_v2
python3 -m wrsource.dataset compare --a ds_v2 --b ds_v2_full
```

规模验证：10 万行、100 组、每组 quota 1000（合计 10 万样本）时，写入约 2s，
并行全量构建约 2.5s，增量刷新受影响组约 3s，校验约 3s。
