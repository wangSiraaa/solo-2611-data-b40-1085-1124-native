# 案卷页序目录与引用数据仓（task 726）

独立的卷内页序目录 + 摘录引用仓，输出可被只读客户端直接消费的
`citation-product/1` JSONL。不解析 PDF 或图像；不依赖旧后端。

只提供输入与只读客户端时也能完整运行：`materials/726/` 下的
`pages.json`、`references.jsonl`、`operations.jsonl` 是全部初始数据。

## 快速开始

```bash
# 1) 建目录 → 建引用 → 应用操作 → 导出（与 materials 的真实输入一致）
python3 -m citation_system init --db work/cli_run/catalog.db \
    --pages materials/726/pages.json --force
python3 -m citation_system add-references --db work/cli_run/catalog.db \
    --references materials/726/references.jsonl
python3 -m citation_system apply-operations --db work/cli_run/catalog.db \
    --operations materials/726/operations.jsonl
python3 -m citation_system export --db work/cli_run/catalog.db \
    --usable exports/cli_run/usable.jsonl \
    --needs-attention exports/cli_run/needs_attention.jsonl

# 2) 只读客户端只读 JSONL，不打开数据库
python3 materials/726/citation_client.py \
    exports/cli_run/usable.jsonl exports/cli_run/needs_attention.jsonl
```

一次跑完全部展示场景（含保留的旧导出）：

```bash
python3 scripts/demo_726.py     # 写 exports/scenario/，每步一个目录，互不覆盖
python3 -m unittest tests.test_citation_system -v
python3 scripts/benchmark_726.py  # 20 卷 × 200 页 × 500 引用
```

`exports/scenario/00_initial/` 与随题的 `example_usable.jsonl` **逐字节一致**；
客户端对它的输出为：

```
C_PIN usable V1/p2,V1/p3 -
C_RANGE usable V1/p2,V1/p3,V1/p4 -
```

## 目录（`citation_system/catalog.py`）

- **身份**：页的完整身份是 `(volume_id, page_id)`。`page_id` 只在卷内唯一；
  `V1/p1` 与 `V2/p1` 是不同页。`page_id` 一旦分配，插页/移位/停用都**不重发**，
  插入已存在的 `page_id` 会以 `duplicate_page_id` 整批拒绝。
- **物理次序**：每卷内 `seq` 连续 `0..n-1`，**同时**覆盖在用与停用页；
  停用只改状态，不删除、不改位置、不重排号。`UNIQUE(volume_id, seq)` 在
  任何单条 UPDATE 后都成立（右移从高到低，左移从低到高，先挪到临时槽）。
- **显示页码**：`label` 一律字符串，`"3"`、`"03"`、`"2a"` 各不相同，不做
  数值化或前缀匹配。
- **解析**：`resolve(volume, section, label)` 只在**在用**页里找，必须恰好
  一个：0 个 → `resolution_not_found`，多个 → `resolution_ambiguous`
  （附 `matching_page_ids`）。正文 `body/3` 与附录 `appendix/3` 因此不混淆。
- **查询**：当前前后页 `neighbours`（可按 `active_only` 跳过停用页，但返回
  的仍是真实页身份）；有序区间 `ordered_range`（含端，可选排除停用）。
- **操作**：`insert` / `insert_many` / `deactivate` / `activate` / `move` /
  `move_many` / `reorder`，每个请求是一个原子批次。`{"batch_id", "operations"}`
  把多个操作合成一个批次；**任一失败则整批回滚**，包括本来合法的兄弟操作。
- **同卷限制**：`move` 带 `target_volume_id` 且与源卷不同 → `cross_volume_move`；
  锚点页只存在于另一卷 → `cross_volume_anchor`。两种都在写之前拒绝。
- **最终次序**：`reorder` 必须列出该卷全部页（含停用页）且恰好一次，
  可同时改变次序与状态；位置不复用、不重复。
- **操作日志**：每个请求（含被拒绝的）都记录 `status`、`error_code`、
  `catalog_revision`、`expect`/`expectation_met`，可审计。
- **目录独立**：没有任何摘录时目录照常建、改、查。

## 引用仓（`citation_system/repository.py`）

- **创建时固定身份**：接受 `references.jsonl` 的两种写法：
  - 直接给身份：PINNED `page_ids` / RANGE `start_page_id`,`end_page_id`；
  - 给 `(section,label)`（PINNED 用 `items`，RANGE 用 `start_*`/`end_*`）：
    创建时解析一次，之后**只存实际页身份**，读取时不再重解文字。
  - 解析歧义/未找到时**什么都不写**。
- **PINNED**：固定成员集合，无顺序语义。后续插页/移位/重排都不改成员。
  成员停用后仍是成员，整体转入待处理并标明 `active:false`；**绝不静默删成员**。
- **RANGE**：固定同卷两个端页身份。查询时取**当前次序**两端之间（含端）的
  **在用页**，所以中间插页会真实进入结果、中间页停用会被排除。
  任一端停用或先后反转 → 待处理；`source` 与 `pages` 都保持原端点顺序，
  **不交换、不猜测**。
- **重开**：端点重新启用 / 移回原序后，同一引用无需重建即恢复可用。
- **反查**：`citations_referencing(volume, page)` 按实际身份返回 PINNED
  成员与当前 RANGE 覆盖（`member`/`start`/`end`/`interior`）。

## 导出（`citation_system/export.py`）

- 在**一个只读事务**里取完整已提交快照（revision、全部页、全部引用、成员、
  端点），之后只从该快照构造产品。因此一条引用的页列表**不可能拼自两个
  状态**，所有行的 `catalog_revision` 相同。
- `usable.jsonl`：完整可用引用；`reason` 为 `null`；RANGE 的 `pages` 按当前
  卷内次序。
- `needs_attention.jsonl`：端点失效/反转的 RANGE，或含停用固定页的 PINNED。
  保留 `citation_id`、`source`（原卷/原端点或原成员）、`pages`（完整固定集合
  或两个已知端页及状态）、稳定 `reason`，并附 `detail`。
  `reason` 取值：`pinned_inactive_member`、`range_endpoint_inactive`、
  `range_endpoints_reversed`。
- **每个引用恰好出现在一个文件**；PINNED 含停用页时整体进待处理文件，
  不导出其余页冒充完整引用。
- 写出前用 `validate_products` 校验合同；两文件先写临时文件再 `os.replace`，
  单个文件不会半写。若需两文件成对切换，请以**整个导出目录**为单位发布。

## 依赖索引（`citation_system/dependency.py`，可选）

- 目的：加速"某实际页被哪些引用覆盖"。
- 表 `page_citation_dependency(volume_id, page_id, citation_id, role)`。
- **从不用于导出**，只用于查询，所以索引陈旧不会污染产品。
- 带 `catalog_revision` 戳；任何目录变更（含同卷移位）都会使戳不匹配，
  下次查询时惰性整表重建。PINNED 行与次序无关，RANGE 行随次序变化；
  为简单起见整表重建。
- 成本（实测，见 `scripts/benchmark_726.py`）：20 卷 × 200 页 × 500 引用时
  重建约 35 ms、约 1.7 万行；单次反查中位数：直查 ~0.25 ms，
  走索引 ~0.18 ms。规模更小时直查已足够，索引只是可选加速。
  同卷移位本身 ~0.6 ms，重导出 ~18 ms。

## 规模与性能

`scripts/benchmark_726.py` 在目标规模（20 卷、每卷 200 页、500 引用）测得：

| 操作 | 耗时 |
| --- | --- |
| 载入目录 | ~8 ms |
| 创建 500 条引用 | ~31 ms（~0.06 ms/条） |
| 一次性导出 | ~19 ms |
| 同卷移位 | ~0.6 ms |
| 移位后重导出 | ~18 ms |
| 依赖索引重建 | ~35 ms |

## 布局

```
citation_system/   实现源码（catalog / repository / export / dependency / cli）
tests/             66 个单元与集成测试（含随机不变量、真实导出与柜台工作流）
scripts/demo_726.py        端到端展示，逐步保留旧导出
scripts/benchmark_726.py   目标规模基准
exports/scenario/  演示产生的各步骤导出（旧目录保留）
exports/cli_run/   按 materials 原始输入走完整 CLI 的导出
work/              运行的 SQLite 数据库
```

## 旧当簿清理与出柜方案（`pawn.py` / `counter.py`）

- **能否只取怀表**：取决于该物自身仍有效的解押门槛。同一笔债可以同时押怀表和
  印盒；若怀表的门槛是“本债本金不超过 2”，而印盒要求清户，则本金降到 2 时
  怀表可出、印盒仍须留存。
- **整数旧账重放**：`PawnLedger.replay()` 按 `(volume_id, seq)` 重放每页。每页
  一个唯一 `operation_id`，支持立债、加费、续当缴款、押物和解押条款；金额只能是
  非负整数，缴费先补费用再还本金，超出部分以 `refund` 退给具名付款人。
- **固定签记**：普通操作的签记必须是可用 **PINNED** 引用，并包含当前物证页；
  停用页、非 PINNED、引用不含本页或重复使用的签记都会使该页成为无效旧流水。
  无效页保留在结果中，不能由后一页补合法。
- **解押条款**：支持 `full_debt`（本债/所列债全清）和 `principal_cap`
  （所列债费用为零且本金合计不超过门槛）。`item_status()` 为每件物列出所有仍有效
  的门槛、负责人、物证页和当前是否满足。RANGE 条款按真实导出的当前区间判断，端页
  失效即进入待处理，原端点不交换。
- **柜台方案**：本题只实现“有截止的保留”，不实现担保单。`CounterOffice` 把票、物、
  钱柜和债权绑定到具体分柜；整张票不可拆分给两笔债，找零只从该柜零钱出。票一旦收进
  钱柜、物一旦签领，都只通过不可逆状态推进，数据库回滚不会当成实物退回。
- **隔离与最坏责任**：`before_tickets` 班可先揭封，隔离时整单拒接且不收票；
  `after_first_ticket` 班在首票不可退后才看到第二柜，客户未同意部分成交或补偿时，
  `plan_redemption(..., require_full_delivery_guarantee=True)` 必须直接拒绝保证。
- 目标规模为最多 6 笔债、8 件物、10 张票。`tests/test_pawn_counter.py` 用真实
  `export.collect_products()` / JSONL 导出覆盖端页失效、重复签领、页序改变、找零、
  联署权、两柜隔离和不可逆签领。

## 不做什么

不 OCR、不做自然语言定位、不做版式编辑；不解析 PDF 图像；不读取图片内容。
