# 精确预算循环引用解算系统

纯 Python 3.11 标准库实现（无第三方依赖、无浮点、无 `eval`、不使用 Excel 缓存值）。
模型格要么是精确有理数输入，要么是“常数 + Σ 有理系数 × 另一格”的线性公式；
跨模型引用使用永久 `model_id` + `cell_id`。系统显式禁止：两个未知格相乘、
任意代码执行、float 输入。循环引用被当作线性方程组的正常情形处理——可能唯一
可解、冗余但有自由变量、或互相矛盾。

## 一键复跑

```bash
./run_tests.sh
# 或分步
python3 -m tests.test_service   # 58 项断言：手算小例 + 六类规定场景 + 重开 + 两个核心回归
python3 -m tests.test_scale     # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli       # CLI 子进程端到端
python3 -m tests.test_organtune # 管风琴调律工单：测音卡 + D 系列（接头）+ C 系列（旧工装）
```

## 架构与数据流

```
JSON 模型 ──put-model──▶ 目录 Catalog（修订/快照，文件持久化、可重开）
                              │ snapshot（递归钉住外部输入版本）
                              ▼
                       编译器 Compiler
              引用核对 → 规范化线性方程 → 依赖图 → 迭代 Tarjan SCC
                              │ 自包含编译产物（带内容哈希、落盘）
                              ▼
                       解算器 SolverService
       按输出依赖闭包 + SCC 拓扑分块精确 RREF（Fraction，带行溯源）
              内容寻址缓存（随服务落盘）→ 三段式解算报告
                              │
                              ▼
              验证器 Verifier（独立从快照重建方程/图，独立分块消元）
       确定值代回全部原方程；参数表达式多组实例化；矛盾 λ 组合重算
```

### 模块

| 文件 | 职责 |
| --- | --- |
| `budget_solver/rationals.py` | 精确有理数边界：拒绝 float、分子分母 ≤12 位、规范分数串 |
| `budget_solver/catalog.py` | 模型目录：创建/修订/固定快照；永久 id 与改名分离；整次修订原子拒绝 |
| `budget_solver/compiler.py` | 快照作用域展开、方程规范化（含自环系数 `1-k`）、依赖图、迭代 Tarjan SCC |
| `budget_solver/solver.py` | SCC 分块精确 RREF、跨块自由变量传播、矛盾证书、内容缓存、报告发布 |
| `budget_solver/verifier.py` | 独立验证路径：重建事实、独立分块求解、代回/证书/哈希核对 |
| `budget_solver/service.py` | 可重开门面：目录面/编译面/解算面/验证面 |
| `budget_solver/cli.py` | `put-model` `snapshot` `compile` `solve` `query` `explain` `verify-*` |

## 关键设计决策

### 1. 目录、快照与永久身份

* `model_id` 永久不变；`name` 是可改显示名，改名只产生新修订，不动身份。
* 每次 `put-model` 在落盘前完成全部引用核对：目标模型/格不存在、修订会让别的
  模型的入向引用悬挂，都会抛 `ReferenceRejectedError` 且**不写任何文件**。
* 快照不可变，包含完整公式、格哈希和每个外部模型钉住的快照 id；递归固定保证
  跨模型输入版本可复现。外部模型改名/更正后重新固定下游时，会钉住外部新 HEAD，
  旧快照与旧报告原样保留。

### 2. 编译器：实际依赖与强连通分量

* 输入格方程退化为 `x = value`；公式格 `x = c + Σ kᵢrᵢ` 规范为
  `x - Σ kᵢrᵢ = c`。自引用合法，自指系数与单位项**相加**（`1-k`），回归测试
  `test_self_reference_cell` 锁定该行为。
* 依赖图只保留真实引用边；迭代 Tarjan（无递归深度限制）输出拓扑序 SCC，
  自环单格也标记为循环块。

### 3. 解算器：分块精确消元（题面允许二选一，这里选依赖分解 + 循环块求解）

* 对每个所选输出取实际依赖闭包，按 SCC 拓扑逐块求解；块内做带**参数列**和
  **行溯源**的精确分数 RREF。
* 上游唯一确定的格直接代入常数；上游未确定的格成为参数并沿 DAG 向下游传播，
  因此“系统有自由度但某输出被唯一确定”能正确分类（如 `t = u - v + 2/7` 恒为
  `2/7`，而 `u=v=p` 保留为参数）。
* 块内出现 `0 = 非零常数` 即无解：溯源行给出原方程的整数 λ 组合作为矛盾证书，
  同时保留参数残差列。绝不把“不收敛”当作无解——精确消元不存在收敛问题，也
  不会返回上一轮数值。
* 闭包按 `(变量, 格哈希)` 集合做 SHA-256 内容寻址缓存：输入修订/系数变更改变
  格哈希 → 指纹变化 → 受影响闭包重算，未受影响闭包命中；缓存与报告随数据目录
  持久化，重开服务后继续生效。旧报告永不重写。

**算法权衡（实测）**：早期对整个闭包做稠密 RREF，300 格规模下分数膨胀到
4300+ 位整数、单次解算约 12 秒；改 SCC 分块后块矩阵有界（循环块 4 格为主），
同规模解算约 0.6 秒，确定值最大约 1 万位（输入仍严格 12 位，增长只发生在消元
过程中）。代价是需要显式维护跨块参数表达式与参数残差列。

### 4. 三段式发布与可核查性

报告把结果分成 `determined`（规范分数）、`undetermined_parameters`
（常数 + 命名参数 `p_<model>::<cell>` 的表达式）、`contradictions`
（λ 组合证书 + 来源方程）三段，并固定目录快照描述、作用域快照、解算配置与
全部内容哈希。

### 5. 独立验证（不是同一套代码自证）

`verify-compile` 与 `verify-solve` 直接从目录快照里的原始公式重建方程和引用
图，用验证器自带的独立分块组装与 RREF 解释复算：

* 编译产物：变量集、逐条方程系数/右端、引用边、SCC 循环标记、哈希；
* 确定值：赋入闭包内全部原方程，残差必须全为 0；
* 参数表达式：6 组参数实例化符号代回，目标方程恒等成立；
* 矛盾证书：按报告 λ 重算原方程组合，左端恒零、右端非零且与证书一致。

## 手算案例（tests/scenarios.py，全部有独立断言）

| 案例 | 手算结论 |
| --- | --- |
| 两格唯一循环 `x=½+¼y; y=⅓+½x` | `x=y=2/3` |
| 冗余一致循环 `a=b=c` | `a,b,c` 自由；`r=a-b+5=5` 固定 |
| 矛盾循环 `a=b+1; b=a+1` | 相加得 `0=2`，输出失败并附来源 |
| 自由但输出固定 `u=v; t=u-v+2/7` | `u` 参数化，`t=2/7` 固定 |
| 跨模型改名 | 永久 id 不变；旧报告哈希/数值不变，新快照钉新 HEAD |
| 输入更正循环 `p=k+q/2; q=k-p/4` | `p=4k/3, q=2k/3`；k=6→8,4；k=12→16,8；旧报告保留 |
| 单格自环 `z=⅓+¼z` | `(¾)z=⅓ => z=4/9` |
| 自由零位下的差 `a=t; b=t+3; d=b-a` | `a,b` 参数化，`d=3` 固定 |
| 跨块参数传播 `a=t; b=a+3` | `b` 仍参数化（`b=3+1·t`），不得误判为确定 |

### 两个核心回归（`tests/test_service.py`）

在给管风琴调律构造测音卡时暴露并修复了两处**长期潜伏**的内核缺陷：

1. **解算器跨块参数传播**（`solver.solve_closure`）：块内遇到“上游变量本身是
   底层自由参数的仿射表达式”（如 `甲 = t`）时，旧实现按**上游变量名**建参数列，
   而把真正的参数贡献写进了**底层自由参数**名下，导致索引错位、参数贡献被丢弃，
   把本应随 `t` 变化的 `乙` 误判为 `determined`。现按底层自由参数建列
   （`upstream_params.update(info[2].keys())`），`乙` 正确保留为 parametric。
2. **独立验证器代回**（`verifier.verify_solve`）：目标输出为 `determined`、但其
   依赖闭包仍含自由参数时，旧实现把自由变量**填 0** 再代回，因而误报残差非零、
   拒绝正确结果。现改为**符号代回**（`symbolic_residuals`）：保留参数列，逐条
   要求常数项与各参数列都为 0。

两处均由上述两个回归测试分别锁定：把任一处改回旧写法，对应测试立即失败。

## 管风琴调律工单（`budget_solver/organtune.py`）

题目“修复室按题定音分做一台小管风琴的调律教学”要求的领域层，直接架在
精确内核之上，两层职责：

* **测量层（真实快照）**：合奏测音卡只给若干管的**差值或和**；连同“各管共享
  一个自由零位 `t`”一起登记为 `budget_solver` 模型并固定成**真实快照**求解一次。
  于是各管自身 parametric（随 `t` 不确定），而某些差值/和却可能 determined——
  正是“两管各有不确定偏差，音程仍完全确定”。所有查询共用同一 `solve_id`，
  不会“前后查同一台架各挑一次误差”。`build_card_payload` / `resolve_card` 提供
  入口；`resolve_card` 顺带做独立验证。
* **装配层（精确 Fraction）**：在已确定的相对零位上，按**真实联动**模拟拨栓、
  可脱接头、独立微调栓、封区，BFS 规划班次工单，并用 `validate_work_order`
  **沿真实装配重演**复核两套和弦、封区与余班（“现场敲封后沿真实装配核”）。

物理与规则要点：管音高 `zero+offset`；一班一个动作（拨一枚栓走一个准许档 /
装一次工装 / 转接脱开一次 / 空班）；同杆联动按系数共动；“可脱接头”脱开后该管
停住、其余仍跟原杆；“独立微调栓”只动该管、档位 ±1 音分且**不脱离原杆**；“封区”
在 `seal_after` 班末封住，之后凡**实际带动**封管的操作一律禁止（已脱开的管不受
同杆封印约束）；已验和弦到交付时仍成立。工具选型取**可脱接头**一种实现。

案例（`tests/organtune_scenarios.py` + `tests/test_organtune.py`）：

| 案例 | 结论 |
| --- | --- |
| 测音卡 `甲=t; 乙=甲+3` | 两管 parametric，`乙-甲=3` determined；和仍不确定 |
| D1 甲固定/将封，乙需 +1，同杆 | 可交：安装→转接脱开→拨栓，共 3 班；末班只动乙 |
| D2 来料班移到两区封盖后 | 同一工单失效：诊断指出安装已晚于封区 |
| D3 甲乙丙同杆，甲封原音、乙丙各 +1 | 可脱接头：脱开甲后旧杆一次抬乙丙（听两根） |
| D3′ 只有一枚微调栓 | 不能完成：一根微栓不能同时抬两根，不假装有第二枚 |
| C1 旧工装，`乙=t+3`，唯一栓 +1，首班调后封 | 可交（首班一档）；反向 −4 和弦同成立 |
| C1′ 测音卡换 `乙=t+2` | 交不了：需 +2，单档只有 +1，一次不够 |
| C2 恢复原测量但乙首班前已封 | 交不了：任何带动乙的操作被封区禁止 |
| C3 同一杆会带坏上一和弦 | 无法完成：`rod_break_analysis` 给出“修好后和弦却带坏前和弦”的证据 |

工单以 `render_work_order` 给出每班**听哪管、做什么、何时封区**；`plan` 失败时
`reason` 用自然语言说明“不能完成的部分”。

## CLI 用法

```bash
python3 -m budget_solver.cli --data-root ./data put-model model.json
python3 -m budget_solver.cli --data-root ./data snapshot M_ID --label v1
python3 -m budget_solver.cli --data-root ./data compile s1
python3 -m budget_solver.cli --data-root ./data solve <artifact-hash-or-path>
python3 -m budget_solver.cli --data-root ./data query solve1 'M_ID::cell'
python3 -m budget_solver.cli --data-root ./data explain solve1 'M_ID::cell'
python3 -m budget_solver.cli --data-root ./data verify-compile <artifact>
python3 -m budget_solver.cli --data-root ./data verify-solve solve1
```

模型 JSON 示例：

```json
{
  "model_id": "M_UNIQUE",
  "name": "两格唯一循环",
  "cells": {
    "x": {"kind": "formula", "constant": "1/2",
          "terms": [{"coef": "1/4", "cell_id": "y"}]},
    "y": {"kind": "formula", "constant": "1/3",
          "terms": [{"coef": "1/2", "cell_id": "x"}]},
    "d": {"kind": "input", "value": "1000/3"}
  },
  "outputs": ["x", "y"]
}
```

跨模型 term 增加 `"model_id": "OTHER_ID"`。项形状本身（单个有理系数 × 单个格
引用）从结构上排除了“两个未知格相乘”。

## 持久化布局

```
<data-root>/
  catalog/catalog.json              # 序列与 HEAD 指针；models/<id>/revs|snaps
  artifacts/<hash12>.json           # 不可变编译产物
  reports/solveN.json + seq.json    # 不可变解算报告
  cache/index.json + closure-*.json # 内容寻址闭包缓存（可重开）
```

所有 JSON 经临时文件 + `os.replace` 原子写入。
