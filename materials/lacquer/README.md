# 漆器叠盒复原与装盒验证（lacquer）

把“漆盖能不能扣上盒、转半圈后两层还能不能一起提走”落成一套纯 Python
领域模块。它与既有的案卷目录/引用仓并列：`lacquer/` 不依赖 SQLite，也不
解析 PDF，只消费 `materials/lacquer/` 下的 JSON 材料与（可选的）旧
`citation-product/1` 导出。

## 物理模型（`lacquer/model.py`）

- 一圈等分为 `divisions` 个整格（最多 8 格），顺时针编号 `0..n-1`；每格
  至多一个特征：卡耳 `lug`、缺口 `notch` 或空。
- **扣合规则**：下件的全部卡耳都必须落进上件底沿的缺口；上沿可以有多余
  缺口，但卡耳绝不能落在非缺口格。
- 器（中层盒）有底沿（缺口朝下）和上沿（卡耳朝上），两条沿**共用一个
  物理转角**：`Vessel.rotated(d)` 同时转两条沿，相邻接口因此不可能“各转
  各的”。
- 底座只出耳，盖只出口；盖可被盒签钉死入座方向（`pinned_rotation`）。

## 量规笔记与刻痕图（`lacquer/notebook.py`）

- `Journal` 是一条沿的量规笔记，以稳定 `journal_id` 读取；每页 `Page` 记
  “从上一条刻痕顺时针走几格（`step`）、落点见耳还是口（`feature`）”。
  等份数与第一个基准痕单独记录，不混在页里。
- 两条硬校验，任一不通过则该沿**不出图**，并给稳定错误码：
  - `closure_failure`：各页步长之和不等于一圈（最后一页须走满一圈）；
  - `cell_collision`：两页落到同一格（一格只能有一条痕）。
- 磨失基准给有限几个起点 `origin_candidates`；同一器的上沿、底沿共用该
  起点表，`reconstruct_vessel` 按起点一一配对成若干仍可能成立的
  `VesselConfiguration(origin, bottom, top)`。
- `JournalBook` 对同名页只**追加修订**（`revise_page`），旧读法可用
  `revision_pins` 原样复跑——这就是“同名页修订的复跑”。

## 只在旧导出的可用页段上制样（`lacquer/sampling.py`）

`reconstruct_from_export` 只接受 `citation-product/1` 中 `status=usable`
的行，取其页并集作为可取样页；一条沿若没有任何页落在可用页段里，返回
`no_usable_pages` 且不出图，绝不猜测。

## 见证（`lacquer/witness.py`）

`Witness` 保存“两处落在同一垂线”的固定引用，落成相对角度等式：

```
lower_cell + lower_rotation == upper_cell + upper_rotation (mod n)
```

见证本身不转动任何件，只在装盒解算时排除无法复现该对齐的入座。

## 装盒单（`lacquer/packing.py`）

- `find_seatings_for_world` 在一个**已确定的世界**（每只器一个构型）里
  穷举所有合法入座：逐接口校验扣合、校验见证、尊重盖的钉死方向。
- `find_robust_sheets` 只返回对**所有仍可能成立的世界**都成立的整张单子：
  同一只具名底座、同一列具名器、同一只盖、盖方向、以及**每只器同一个
  整格转角**。中层“须正放”用 `pinned_vessel_rotations={"M": 0}` 表达，
  因此观察前若两世界需要不同底座/盖，就没有任何可交单子——必须先观察。
- 上限 6 件、8 格，超出在构造 `PackingProblem` 时即拒绝。

## 工位、拍与一次性蜡舌（`lacquer/workbench.py`）

全部授权来自工单 `lacquer-work-order/1`，无默认值：

- **量规** `gauge`：在指定基准线上只问“某格是否露出刻痕”；器须在量台
  原样停满 **2 拍**（`GAUGE_DWELL_BEATS`）才读数，不动蜡舌、不耗舌。
- **试咬夹台** `trial_bite`：夹 **1 拍**后才知指定试齿在某角是否入槽；
  成功才消耗本件一枚**具名一次性蜡舌**，失败只耗拍。
- 稳定错误码：`test_not_offered`（工位/问题不在工单）、
  `beat_budget_exhausted`（拍不够）、`tongue_unavailable`（舌已耗或未配）、
  `original_tongue_required`（要求原舌出运，不能拿备舌冒充、也不能试咬
  花掉原舌）、`commitment_blocked`（邻层已压紧不可拆开重验）。

## 自适应验证与签班（`lacquer/planner.py`）

`build_plan` 从工单实际允许的问题长一棵浅决策树：

1. 若某张固定单子对当前分支仍存活的每个世界都扣合，立刻提交、不再问；
2. 否则选最便宜且能区分剩余世界、且在每个答案下都付得起的问题，按
   True/False 递归；
3. 没有可问、可负担的问题而世界仍要求不同单子 → 该分支不可签。

`evaluate_all_worlds` 用同一棵树在每个真实世界上各跑一班，返回实际观察、
耗拍、耗舌（含是否动用备舌）、以及最终提交的装盒单或不可签原因。一张
订单的多种补抄读法在观察前不会分别拿到不同的盖。

## 四环复跑例（材料 + `scripts/demo_lacquer.py`）

`materials/lacquer/notebooks.json` 给出中层 M 的两沿与一个闭圈失败例 X；
`pieces.json` 给底座（耳 0 / 耳 2）、盖（正向口 1 / 正向口 3）和
`1+3 → 2+2` 的同名页修订；`work_orders.json` 给三只工单。复跑：

```bash
python3 scripts/demo_lacquer.py
python3 -m unittest tests.test_lacquer -v
```

复跑逐条复现题面结论：

1. 中层仅可能是 `(下口0,上耳1)`（origin 0）或 `(下口2,上耳3)`（origin 2）；
2. 正放（转角钉死 0）下两世界分别需要 `B0+L1` 与 `B2+L3`，观察前无鲁棒单；
3. 上沿笔记改成先 2 后 2 后，底层仍锁角 0，但口 1、口 3 两只盖都扣不上，
   不能提走；X 沿报 `closure_failure` 不出图；
4. 两拍保全原蜡舌：量规可办，两世界都能签，且一枚蜡舌都不耗；
5. 一拍 + 唯一具名备舌：试咬可办，成功耗备舌、失败只耗拍；撤走备舌单即
   不可签；一拍且无备舌、又要求原舌出运时，试咬不能承诺全成。

## 模块布局

```
lacquer/
  model.py      环、器、底座、盖与扣合规则
  notebook.py   稳定身份量规笔记、同名页修订、闭圈/碰撞校验、磨失起点
  sampling.py   只在旧 citation-product/1 可用页段上制样
  witness.py    同一垂线见证的角度等式
  packing.py    单世界入座穷举与跨所有世界的鲁棒装盒单
  workbench.py  量规/试咬、拍预算、具名一次性蜡舌与稳定错误码
  planner.py    自适应验证决策树与 go/no-go 签班
  orders.py     lacquer-pieces/1 与 lacquer-work-order/1 装载
materials/lacquer/  notebooks.json / pieces.json / work_orders.json
scripts/demo_lacquer.py
tests/test_lacquer.py
```
