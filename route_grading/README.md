# 线路分级与赛事排程（route_grading）

在现有案卷产品（`citation_system` 导出的 `citation-product/1` JSONL）之上，重办
抱石线路分级与多组同轮排程。不解析 PDF 或图像；不把颜色名当岩点身份。

## 映射：案卷 → 线路

- **RANGE** 引用 = 本线开放的一组岩点：区间内每个页身份就是一枚稳定编号的岩点
  （“区间所列每页开放一组有位置、颜色和允许左右手的岩点”）。
- **PINNED** 引用 = 本线起终点签：有序起/终握姿必须落在被 PINNED 的页身份里。
- **端页反转则挂起**：RANGE 变成 `needs_attention`（端点反转或停用）时整条线挂起，
  不可分级。
- **同名色不能代替稳定的岩点号**：岩点身份是 `(volume_id, page_id)` / `hold_id`，
  同一格可放两枚不同编号的手点，它们仍是不同岩点。

几何（坐标/颜色/允许手）由独立的 *hold sheet* 提供，见
`route_grading/product.py`；`RouteSpec` 声明起终点与臂展。

## 分级模型（`route_grading/model.py`）

- 握姿 `(left_hold_id, right_hold_id)`，两手必须在两个不同顶点。
- 一步只移一手到本线另一**空**点；另一手固定时两手曼哈顿距离 ≤ 本组臂展。
- 点高不下降（移动手的 `y` 不减）。
- 终点按左右手各自到达；分级 = 最少换手次数（BFS 深度）。
- 无路时给出“能到的最后握姿”（`final_pose`）。
- `required` 可加**正式触碰约束**（中继观察签）：某手必须先碰观察点集合中的一点再
  碰终点，两步都计入评分。

## 并发与安全（`route_grading/scheduling.py`）

同一拍两名选手都看不见对方尚未发生的选择，因此安全必须对**所有**最短路径成立，
而不只是一条“完美回放”：

- `slots_safe_at_beats`：对所有最短路径对逐一检查，任一可能碰撞即不安全。
- **已抓的公共点按实际占用**：一次移动的那一拍同时计入移动前后的握姿，任意同拍先后
  下被握住的公共点都不会被当成空点；补发号不能让已抓点变空。
- **分道罩**（`find_cover_plan`）：开赛前把共享点对某条线永久遮住；被遮线必须仍保持
  **原最短步数**（不按遮后较长步数重标级），且同轮最终不再共用任何岩点。
- **临场号**（`relay_schedule`）：每拍最多发一号；号须在组真实出手**前**抵达
  （到手拍 `a` → 真实首动拍 `a+1`）；`validate_delivery` 拒绝“把第二号改迟至两组
  已同时出手后”再回填成已分开。

## 排程（`route_grading/assignment.py`）

- 每组按签到轮次各用不同线路。
- 指定组间面对**同一最短步数**（按各自实际臂展计算）。
- 同轮不得共用岩点，哪怕线路牌名字不同；声明过的热身岩板重叠区除外。
- 已开攀的组锁定其路线与实际手位；其他组仍可换到备线。
- `≤10 点、4 线、3 组` 的联赛实例见 `build_league()`，并含臂展不同导致评分变化的
  例子 `arm_span_grading_example()`。

## 单场只选一套设备（`route_grading/session.py`）

`plan_session` 在 `covers` / `relay_numbers` / `none` 中只选一套；`assert_single_device`
拒绝混用。

## 用法

```bash
# 1) 现有案卷 CLI：建目录 → 引用 → 导出
python3 -m citation_system init --db work/routes.db --pages materials/routes/pages.json --force
python3 -m citation_system add-references --db work/routes.db --references materials/routes/references.jsonl
python3 -m citation_system export --db work/routes.db \
    --usable work/routes/u.jsonl --needs-attention work/routes/a.jsonl

# 2) 从该产品直接分级
python3 -m route_grading grade \
    --products work/routes/u.jsonl work/routes/a.jsonl \
    --holds materials/routes/holds.json --spec materials/routes/spec.json

# 3) 全场景演示（写入 exports/routes/demo.json）
python3 scripts/demo_routes.py

# 4) 测试
python3 -m unittest tests.test_route_grading tests.test_route_cli_integration -v
```

## 关键示例

- **甲/乙真线**：甲、乙各两步到顶，右先或左先皆可；只把甲区间移出开放右顶点页，
  甲即无路。仅让乙左起点与甲左起点共用同一岩点 → 同轮不得开赛（`undeclared_shared_hold`）。
- **中继观察签等分线**：两左手先碰甲或乙观察点再碰终点（均计分，最少 2 步；直接跳终
  点的“纸面”1 步不被接受）。同拍不安全，即便存在“左取甲、右取乙”的完美回放；用罩可
  把甲只留给左、乙只留给右；再把右翼可选的乙也去掉，原分道即无法保住全场。
- **第二号改迟**：`validate_delivery` 对已同时出手的第二号判 `number_arrived_too_late`。
- **臂展差异**：同一条线，臂展 1 无法起手、臂展 2 得 3 步、臂展 ≥3 得 2 步。
