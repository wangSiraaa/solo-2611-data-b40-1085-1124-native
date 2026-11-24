# 可修订加权记录源、可重复加权采样与速记校录排字房

本仓库实现两个可独立运行的基础模块，并在其上叠加小报的校录/付印扩展：

- `python -m wrsource.source`：SQLite 支撑的可修订加权记录源，稳定 `row_id`、
  分组、权重 1..100、正文、递增修订号、不可变源版本快照与固定版本分页。
- `python -m wrsource.dataset`：可重复的加权采样数据集生产器，全量构建、按变更
  清单增量刷新、真实 JSONL + manifest 输出与对照源 API 的校验。
- `python -m wrsource.proofdesk`：速记校录台与排字房查询（CLI）。练习卡作为普通
  加权行存放在同一个源里，直接复用原抽样器选出的 `row_id` 与固定版本正文。

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

## 代码结构

```
wrsource/
  sampling.py   精确键、比较器、top-quota、边界证书（无 I/O）
  source.py     版本化源服务 + 分页 + CLI（可独立运行）
  dataset.py    全量构建 / 增量刷新 / 校验 + CLI（可独立运行）
  steno.py      连笔切分、一词一义归纳、连笔读法（纯逻辑，无 I/O）
  printshop.py  铅字盒、铅字栏状态机、锁区安全、共版/副栏与整轮排期（纯逻辑）
  proofdesk.py  练习卡入库、抽样目录校录、撤卡重出、CLI
  reference.py  测试用独立精确参考实现
tests/
  test_sampling.py    键构造、双向一致性、极近键、边界证书、参考对照
  test_source.py      原子批次、折叠、修订号、分片移动、分页并发、重启
  test_dataset.py     全量/参考对照、零/超名额、正文版本、分片不变性、
                      并行一致、撤回首名补位、改组、增量==全量、篡改检测、重开
  test_steno.py       完整切分、后缀拒绝、一词一义、冲突与不可支持卡、读法分组
  test_printshop.py   四指令语义、边界停单、锁区违例、同终字越锁、两种工艺、
                      抽走一枚甲、整轮送稿排期
  test_proofdesk.py   抽样目录读卡、改卡出新版本、撤卡刷新==全量、旧版本仍可读
demo/run_demo.py          端到端演示（含 --large 10 万行）
demo/run_press_demo.py    校录台 + 排字房端到端演示（题目全部指定情形）
```

## 速记校录与排字房（题目规则的落地）

### 校录台（`wrsource/steno.py` + `wrsource/proofdesk.py`）

- 手册（manual）的词条表给每个连笔列出可切的短笔形序列；整本手册里一个短笔形
  至多解释成一条指令，改义必须显式 `replace`，不会静默覆盖。至多 6 个短笔、20
  个手势，超限直接拒绝。
- 一张练习卡 = 手势串 + 实际校样上的指令串。只有**完整切分**且短笔数与指令数
  逐条对得上才算支持该卡；只对得上结尾的读法永远不会被返回。
- `induce_semantics` 从入样卡归纳词义：同一短笔出现两种指令时报冲突（附卡 id），
  不猜测；没有任何完整切分支持的卡列入 `unsupported_cards`。
- `proofread` 对尚未排字的新稿枚举全部完整切分，按指令串去重，每个读法保留支持
  它的全部切分；无完整切分时返回 `first_stuck`（首先卡住的位置与剩余手势）。
- 练习卡是源里的普通行（`body` 为 `{"kind":"practice_card",...}`）：`set_body`
  改卡即发布新版本，`retract` 撤卡后 `refresh_dataset` 在新版本完整候选上补入真正
  的下一名，而不是重抽旧样本。校录直接读取抽样目录 `samples.jsonl` 里原抽样器选出
  的 `row_id` 与该版正文。

### 编辑台（`wrsource/printshop.py` 的状态机）

铅字栏是 `text + cursor + locked`，指令四条：

- `insert:字`：在光标前插一粒铅字，光标越过新字；光标位于锁区内即拒。
- `back`：光标退一格；退到/越过锁界可行（不改动锁区），越过左界停单。
- `delete`：删光标前一字并把铅字退回盒中；触到已定版前缀或在左界即拒。
- `commit`：定版到光标，锁区向右扩展。

被拒指令**停掉整串后续工序**，事件流保留最后一条 `refused` 记录。`analyze_safety`
让同一手册下的每种合法读法各在一份独立副本上重跑：终稿相同的读法仍可能在中途触
锁，报告给出最早破坏版面的工序（读法序号、步号、指令、原因）和一条相反的安全读本。

### 早/晚两次发印工艺

- **共版（shared）**：早版只能锁定“所有合法续稿都能安全承接”的最长前缀；求解器从
  目标标题长度向下枚举锁定长度，并同时检查实物铅字盒。晚班沿同一铅栏接排，逐读法
  给出是否交稿、终稿与停单点。早版已发到读者手中的铅字不可收回。
- **副栏（reserve）**：发印前从**同一盒铅字**真占一套备用字粒（需求按整栏翻倍核
  对），末段才决定哪栏付印；备用栏在此之前不得拆给主栏。晚班优先用主栏，越锁读法
  落到未动过的副栏；未选栏的字粒在决定后退回盒中。
- `run_press_schedule` 按送稿次序处理多稿、共享一个物理铅字盒，记录每稿早班最长
  前缀、必须先留下的字粒、晚班能否交稿以及盒内余量。

题目指定情形（见 `demo/run_press_demo.py`，均有断言）：

1. 空栏先打甲乙、光标在末、甲已锁；`u=delete`、`v=insert:丙`，`uv` 只按两笔读，
   印出甲丙。
2. 只把入样练习里 u 的词义改为退格（改卡发布新版本），`uv` 得甲丙乙。
3. 恢复证据并把初始锁区扩为甲乙，第一笔 delete 即停。
4. 连笔 `y` 两种切法自由终字同为甲丁，其中“删甲再补甲、插丁”的读法在锁住甲时
   第一笔即停，另一条“只插丁”安全交稿。
5. 晚稿连笔 `n` 两读法终稿都叫甲乙；共版最长安全早版前缀为 0（锁甲会让删甲读法
   越锁），副栏在盒中有两枚甲时可行、主/副各承接一读；抽走一枚甲后副栏无法浇铸。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 演示（小规模 / 10 万行）
python3 demo/run_demo.py
python3 demo/run_demo.py --large --parallel

# 校录 + 排字房演示
python3 demo/run_press_demo.py

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

# 校录 / 排字房 CLI
python3 -m wrsource.proofdesk induce    --dataset ds_v3 --manual manual.json --apply
python3 -m wrsource.proofdesk proofread --dataset ds_v3 --manual manual.json --gestures y
python3 -m wrsource.proofdesk press     --manual manual.json --dataset ds_v3 \
        --gestures n --initial-text 甲 --title 甲乙 --deadline-ligatures 0 \
        --sorts '{"甲":2,"乙":1}'
python3 -m wrsource.proofdesk schedule  --manual manual.json --dataset ds_v3 \
        --plan schedule.json
python3 -m wrsource.proofdesk retract   --db src.sqlite --dataset ds_v3 --row-id card-005
```

规模验证：10 万行、100 组、每组 quota 1000（合计 10 万样本）时，写入约 2s，
并行全量构建约 2.5s，增量刷新受影响组约 3s，校验约 3s。
