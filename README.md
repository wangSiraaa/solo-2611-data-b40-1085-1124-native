# 可修订加权记录源、可重复加权采样与贴纸厂/换卡柜共用程序

本仓库实现四个可独立运行的模块：

- `python -m wrsource.source`：SQLite 支撑的可修订加权记录源，稳定 `row_id`、
  分组、权重 1..100、正文、递增修订号、不可变源版本快照与固定版本分页。
- `python -m wrsource.dataset`：可重复的加权采样数据集生产器，全量构建、按变更
  清单增量刷新、真实 JSONL + manifest 输出与对照源 API 的校验。
- `python -m wrsource.tape`：制带室的连印带推断，从**确实入样**的拆包回条核出
  能共同解释全部回条的（带型、起格、方向），并查同一未开包在不同解释下的整组
  内容。
- `python -m wrsource.counter`：换卡柜的保证单办理，在只看回条信念、不预看印带
  的前提下，穷举 AND-OR 搜索能事先签下的最多册数，输出可照办的办理树与逐笔台账。

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

## 制带室：连印带推断（`wrsource/tape.py`）

### 模型

- `TapePattern`：一圈固定具名格位，每格一张完整卡面（图案 + 亮/普面）；所有候选
  带共用同一环长。
- `Hypothesis = (带型, 起格, 方向)`：一班只有**一个未知起格、一个前进方向**
  （正向格号递增绕回 / 反向递减绕回）。包序 `p`、包内第 `s` 张落在
  `(起格 + 方向 * (p * 每包张数 + s)) mod 环长`。
- `Receipt`：来自确实入样开包人的回条，注明包序、包内第几张，以及“看到的图”或
  “亮/普面”至少其一；构造 `TapeRoom` 时可用 `sampled_pack_indices` 强制回条只能
  来自样本目录中的包。

### 推断规则

`TapeRoom.feasible_hypotheses(...)` 枚举候选解释，要求**同一个**（带型、起格、
方向）同时满足每一张回条——不同包不能各挑一个有利起格。回条撤回/新增通过
`with_receipts` / `add_receipt` / `retract_receipt` 换一版不可变制带室；
`Belief.pack_outcomes(pack)` 返回同一未开包在当前全部解释下的整组内容（去重），
`filter_belief` 用开包实卡收缩信念。

## 换卡柜：保证单与办理树（`wrsource/counter.py`）

### 柜面语义

- 每本册子的空位要求具名卡（指定图、指定亮/普面）；一换一清单 `SwapRule` 允许把
  柜台真牌对调成目标卡，**每次对调占一个换权**，用掉不退还。
- 未拆包带包号、只可拆 `max_opens` 次；每开一包才看到整包实物，之后才决定下一包
  开哪号。规划器只依据与回条相容的 `Belief` 推进，开包是**机会结点**：每个可能
  观察都要有能继续兑现的分支，因此无法预看没开包的印带。
- 两种柜制：
  - **即换**：当场交真牌、换入牌马上装册，占一个换权，户随即离柜。
  - **押换**：户主离柜前把双方两张具名实卡封入唯一押柜格（`Escrow`），占一个
    换权；约定包揭出指定图案才成交，落空则原卡退还、换权释放。封着的卡不能转押
    或提前贴册；全柜只有一格押柜。
- 客户带 `departs_before_pack` 离柜拍点：离柜后只能结算已入押信封，不能新办交易。
- 保证单在拆包前开给具名册号；`plan_max` 按册数从大到小枚举册子集，对每个子集做
  穷举 AND-OR 搜索，返回能在**每一种合法包况**下补齐同一组册的最大集合。

### 为什么是 AND-OR 穷举

确定性动作（贴册、清单对调、即换、押换）是 OR 结点，任选一个能通向胜利即可；
开包是 AND 结点，该包在当前信念下的每种整组内容都必须通向胜利。状态按
（剩余空位、持有卡号、空闲换权号、客户在场位、押柜、已开包、当前信念解释集）
记忆化；资源（卡、换权、开包次数）单调消耗，搜索必然终止。规模按题目约束
（≤ 6 包、≤ 3 册的小册演示）设计，枚举即证明，不依赖启发式。

### 台账与追查

`OrderRunner` 按办理树在指定世界（一条可行解释）下重放，`OrderLedger` 逐笔记录
`slip_issued / listed_swap / instant_swap / escrow_placed / escrow_settled /
escrow_failed / pack_opened / mount / order_completed`。柜员可按单号查开包前
承诺给谁（`commitment`）、按原卡号查重卡去向（`card_trace`，含对调衍生卡）、
按换权号查占用与释放（`right_trace`）、按册号查实卡装册记录（`album_destination`）。

### 共用 JSON 规格

制带室与柜台吃同一份规格：顶层放 `patterns / receipts / pack_size / constraints`
（可限定 `start_cells` 与 `directions`），`counter` 段放册号、缺卡清单、持牌、
换权、一换一清单、换卡户、可拆包号与拆包次数。`build_from_spec` 先建信念再建
规划器，并拒绝把已有回条的已开包递给柜台。

## 代码结构

```
wrsource/
  sampling.py   精确键、比较器、top-quota、边界证书（无 I/O）
  source.py     版本化源服务 + 分页 + CLI（可独立运行）
  dataset.py    全量构建 / 增量刷新 / 校验 + CLI（可独立运行）
  tape.py       连印带推断、回条信念、未开包包况（无 I/O）
  counter.py    保证单 AND-OR 规划、押换/即换、台账 + CLI
  reference.py  测试用独立精确参考实现
tests/
  test_sampling.py  键构造、双向一致性、极近键、边界证书、参考对照
  test_source.py    原子批次、折叠、修订号、分片移动、分页并发、重启
  test_dataset.py   全量/参考对照、零/超名额、正文版本、分片不变性、
                    并行一致、撤回首名补位、改组、增量==全量、篡改检测、重开
  test_tape.py      回条共同解释、亮面回条、撤回重开信念、多样本目录闸、
                    多卡包、反向、往返序列化
  test_counter.py   题面例 1/2、押换 vs 即换、入押许可/离柜反事实、
                    自适应开包、亮面区分、台账卡号/换权追查、共用规格
demo/run_demo.py            源 + 采样端到端演示（含 --large 10 万行）
demo/run_counter_demo.py    制带室 + 换卡柜三例端到端演示
```

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 演示（小规模 / 10 万行）
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

# 制带室 + 换卡柜（共用一份 JSON 规格，见 demo/_work/counter_spec.json）
python3 demo/run_counter_demo.py
python3 -m wrsource.counter plan --spec counter_spec.json --out plan.json
```

规模验证：10 万行、100 组、每组 quota 1000（合计 10 万样本）时，写入约 2s，
并行全量构建约 2.5s，增量刷新受影响组约 3s，校验约 3s。
