# 可修订加权记录源与可重复加权采样

本仓库实现两个可独立运行的模块：

- `python -m wrsource.source`：SQLite 支撑的可修订加权记录源，稳定 `row_id`、
  分组、权重 1..100、正文、递增修订号、不可变源版本快照与固定版本分页。
- `python -m wrsource.dataset`：可重复的加权采样数据集生产器，全量构建、按变更
  清单增量刷新、真实 JSONL + manifest 输出与对照源 API 的校验。

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
  reference.py  测试用独立精确参考实现
tests/
  test_sampling.py  键构造、双向一致性、极近键、边界证书、参考对照
  test_source.py    原子批次、折叠、修订号、分片移动、分页并发、重启
  test_dataset.py   全量/参考对照、零/超名额、正文版本、分片不变性、
                    并行一致、撤回首名补位、改组、增量==全量、篡改检测、重开
demo/run_demo.py    端到端演示（含 --large 10 万行）
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
```

## 奶酪模型展质检台与切样程序

新增代码只依赖标准库，核心包为 `wrsource.cheese`；同时保留 `cheese`、`qc.py`、
`slicing.py` 等便捷导入别名。原 `demo/run_demo.py` 入口不变，奶酪案例入口为：

```bash
python3 demo/cheese_demo.py
python3 -m wrsource.cheese examples
python3 -m wrsource.cheese audit-demo --work demo/_work/cheese_audit

# 直接对 canonical JSON 案例跑质检/切样
python3 -m wrsource.cheese qc --case demo/cheese_case.json
python3 -m wrsource.cheese plan --case demo/cheese_case.json \
    --mode sealed --beats 1
```

### 输入概念

- **块/候选模型**：`size` 每边最多 5 格，一次最多 4 块；模型给出零基 `holes`
  体素坐标（一维整数 `4` 按“第 4 格”的一基简写处理）、`wax_faces`/`colored_wax`
  与可选 `wax_voxels`。
- **探针记录**：`block_id`、`entry_face`、`grid_point`、`direction`、
  `first_hole_depth`（一基格序号）或 `solid: true`。有 `rig_id` 的记录共用同一
  个安装朝向；默认枚举 24 个物理旋转，也可用 rig 的 `orientations` 限定。
- **盒**：`boxes[].strips[]` 给出 `shape`、`max_holes` 和
  `display_face: "left" | "right" | ... | "any_outer"`，可用 `wax_color` 对色蜡。
- **夹具/实刀**：`fixtures` 或块内 `fixtures` 给出准许平面
  `{"id":"mid","axis":"x","at":2}`；`cuts` 是已有实刀记录。已切开的子块不会被
  合并回原块，对已消失父块再次落刀会报错。

### 可重复调用 API

```python
from wrsource.cheese import (
    quality_check, quality_check_dataset, explain_plane,
    plan_order, CheeseDesk,
)

qc = quality_check(case)                 # 也可直接吃 samples.jsonl 的记录列表
print(qc["possible_models_by_block"])    # 每块仍可能的完整模型
print(qc["rig_orientation_conflicts"])   # 同架朝向冲突

print(explain_plane(qc, "B", {"axis": "x", "at": 2}))

# 普通单：提前封签，不能临时换条
sealed = plan_order(qc, mode="sealed", station_beats=1)

# 照面台：一个 cut beat + 一个 table beat，故 station_beats=2
adaptive = plan_order(qc, mode="table", station_beats=2)

# 读真实采样器产物，并可回查 SQLite 源版本
qc2 = quality_check_dataset("ds_v1", source_db="src.sqlite", verify=True)
remaining = plan_order(qc2, cuts=cuts, boxes=boxes, mode="sealed", cut_budget=0)
```

`plan_order(...)` 先按“完整盒数量最多”选盒，再在选中的盒集合内找固定切线/签条。
固定签模式要求同一个具名 piece 对所有幸存模型都合格；照面台模式先选第一刀，
按两个新鲜切面上的可见孔性格式分组，每组再跑固定方案。每个分支都独立保证
“同一条实物不重复入盒”，不会把不同模型解释里的条凑成两份实物。

### 题面四格案例的结论

- 候选模型：仅第 4 格有孔（`only-cell-4`），或第 1、4 格有孔
  （`cells-1-and-4`）。
- 左探针记首孔在第 4 格后，只剩 `only-cell-4`；第 4 格会被 `x=3`
  （第 3、4 格之间）那一刀露出，锁死的中线是 `x=2`，其中线切面必无孔。
- 中线切出的左半是第 1–2 格，可作为预先点名的无孔条入盒；右半含第 4 格。
  因此同一个四格块不能承诺两根两格无孔条，最多承诺一个这种盒/条。
- 若把首孔更正为第 1 格，则只剩 `cells-1-and-4`，左右两半各有一孔；只有中线
  夹具时没有两格无孔条。恢复深度 4 的旧正文/样本后，左半重新合格。
- 两份探针若都挂同一 `rig_id`，报告会给出 `rig_orientation` 冲突：每条单独
  都能找到朝向，但所有记录的公共朝向交集为空。
- 孔只可能在第 2 或第 3 格时，固定封“左半”或“右半”都不能对两种模型保证；
  `station_beats=2` 时照面台可在中线一刀后按所见格把无孔半条入盒，把该台拍
  拿走（`station_beats=1`）后同一承诺不可行。

### 加权源修订、首名撤回与重开复查

`CheeseDesk`/`build_weighted_audit(path)` 会真实执行：

1. `SourceService` 建源，原 `build_dataset` 产出样本；
2. 确定性挑选使 `probe-P0-left4` 为同组首名的真实 seed；
3. `set_body` 把首名正文更正为深度 1，再 `refresh_dataset`；
4. 恢复深度 4，再用原采样器撤回首名并增量刷新，补入真实下一名；
5. 保存旧版本签出的中线刀与 `B-left`/`B-right` 身份；
6. `CheeseDesk.reopen(path).recheck()` 分别复查旧样本支持的签条，以及新版本
   证据下已有两半的余料方案。

旧数据集和不可变源版本仍保留深度 4 正文；新证据即使改为深度 1，也只改变复查
结论，不会把已经锯开的 `B-left`、`B-right` 当成未切的 `B` 重排。


规模验证：10 万行、100 组、每组 quota 1000（合计 10 万样本）时，写入约 2s，
并行全量构建约 2.5s，增量刷新受影响组约 3s，校验约 3s。
