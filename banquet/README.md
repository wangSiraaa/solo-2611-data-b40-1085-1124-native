# 礼俗宴席复原层（banquet）

在既有「页序目录 + 引用仓」（task 726）之上，消费其只读产品
`citation-product/1`，把一桌旧宴按仪注、席位、传译与保密约定复原出来。
**不解析 PDF/图像，不做语音识别**；最多八人、三轮。

本层回答的问题：

- 礼簿上换了上席的人，围桌的人**还能听懂每一轮祝辞吗**？
- 哪些旁听或越界，会让**整晚的保密承诺失效**？

## 输入：只读引用产品

`banquet.products` 只读导出的 JSONL（与 `materials/726/citation_client.py`
一样，不打开数据库），并区分：

- **PINNED = 签到页**：可用的是当场确实到场的人；含停用成员的
  `needs_attention` PINNED 不可信 → **挂起整场**。
- **RANGE = 仪注页**：可用的按当前卷内次序逐页生效；端点停用/反转的
  `needs_attention` RANGE → 挂起，**绝不按现存页拼一桌**。
- 多个引用的 `catalog_revision` 不一致 → 挂起。

`ProductSet.suspend_reasons()` 给出可复查的挂起依据；为空才继续。

## 仪注（`banquet.model`）

每页一条仪注，来自典礼脚本（`banquet.decode`）：

| kind | 含义 |
| --- | --- |
| `order` | 具名来宾的先后（`sequence`），可指定 `head`（主席座） |
| `adjacency` | 谁须紧邻谁（`pairs`） |
| `substitute` | 替位者：`inherit_order` 继承原人礼序；`borrow_chair_only` 仅借其椅、不继承任何关系 |
| `revoke` | **只撤销所指稳定页上的一条仪注**（`target_page_id`） |

- 主席固定在 0 号椅；座序从主席**左手**沿桌顺排，`seats[i]` 即「左 i 步」。
- 邻接是环状的；**空椅是物理缺口**，隔空椅的两个座位不算相邻，传话不能跨越。
- `SubstituteClause(mode="inherit_order")` 把原人在 order/adjacency 中处处改名，
  替位者因此继承其礼序；`borrow_chair_only` 不动关系，只要求替位者在场而原人离席。

## 席图（`banquet.seating`）

`seat_session(products, pages, guests=..., identity_of=...)`：

- 产品齐全 → 枚举**全部合规席图**（`status="seated"`）；
- 有产品但无一张合规 → 给出**可复查的无席依据**（`status="no_seating"`，
  `basis` 形如 `adjacency:甲-乙`、`precedence:甲<乙`、`missing:X`）；
- 产品缺失/冲突/人员未描述 → **挂起**（`status="suspended"`），不臆造。

## 传译（`banquet.relay`）

- 交谈只能沿**相邻椅**传递；途中每位都必须是**获准听该话**的译员；
- 每段相邻两人须有**共同语言**，逐段给出该语言与译员；
- 一轮内同一译员**最多办一条话路**（`plan_round` 要求各路的译员集合互不相交）；
- 四椅例：甲红、乙蓝、丙兼、丁绿。顺排 甲丙乙丁 时，甲乙私谈经 丙，
  语言 `[red, blue]`、译员 丙 → **可谈**；勘误改 甲乙丙丁 后，甲乙直接无共同语、
  绕桌又须过丁（绿、未获准）→ **不可谈**；恢复原座但撤丙的旁听许可 → **不可谈**。

## 线索与保密（`banquet.clues`）

- **每段**给听过者取得一个具名线索 `(conv_id, 段号)`；一段祝辞常由几段私语接成，
  一个 `secret` 标签只有**集齐其全部段线索**才算被完整掌握。
- **普通轮次**只按获准话路确定听众（仅路径上的人）。
- 标为 `incidental`（顺带听闻）的轮次/谈话：除路径上的人外，**同一区域未隔开的
  邻位**也会听见（相邻椅跨未挡接缝）。
- 线索**单调累积**：已开口的一段，不会因追改席图或换译员而「没听见」
  （`ClueLedger` 只增不减；`Night.prior` 用于先前的、已实际开口的轮次）。
- `evaluate_secrecy` 找出每个保密组被**任一译员单独**或**指定可互通的一对**
  合起来集齐的情形，给出 `via="single_interpreter"` 或 `"designated_pair"`。

## 管控：屏或换席（`banquet.control`）

礼房本场**只采取一种**：

- **挡声屏** `ScreenControl(seam, install_round)`：安装须占一个**空轮**
  （`is_empty`），从**随后一轮**起挡住该接缝。它截住题给邻位间的顺带听闻，
  也挡住该处**原本合法的直接传话**（若某路只能过该缝，该轮变不可行）。
  `can_install_screen(..., before_round=...)` 检查目标轮之前是否还有空轮；
  若只剩开口当轮 → 无法及时安装，**不能凭改记录救单**。
- **换席** `SwapControl(swaps)`：不用屏，按各轮 `swap_quota` 把替译员移到
  **未落座且礼序允许**（结果席图仍合规）的位置；线索跟人走，可互通登记按实生效。
  `find_swap_seats` 优先空椅；`book_swap` 复核某次换席是否可订（配额 + 合规）。

`simulate_night` 逐轮推演并返回 `NightResult`（`ok` / `breach` /
`invalid_control`）。`banquet.sheet.build_sheet` 产出唯一交付物
**传译单**：真实椅图、逐段共用语言与译员、累积线索、全部越界，以及
`diagnostics`——场方可据此指出**哪次旁听 / 哪次越界**会让整晚承诺失效。

## 红/蓝两轮（spec 的具体例；见 `scripts/demo_banquet.py`）

基座环（含一空椅）：`0 甲 1 丙 2 戊 3 丁 4 乙 5空`。红片已在前一场（丙在甲乙之间）
实际转出，故丙持红；本轮由戊向丁转蓝片，丙在戊邻位：

| 方案 | 结果 |
| --- | --- |
| 不设管控 | 丙顺带听到蓝 → 独持红蓝 → **single_interpreter 越界** |
| 挡屏（有空轮安装） | 丙-戊接缝被封，蓝路不经过该缝 → **可签** |
| 只剩开口当轮 | 无空轮可安装 → **不能签**（不可改记录救单） |
| 换席（丙移到非邻合规未锁位） | 丙不再听到蓝 → **可签**，且该次换席可订 |
| 最后：戊是丙登记的互通人 | 即便挡住丙旁听，丙(红)+戊(蓝) 合起来集齐 → **designated_pair 越界，仍不能签** |

> 几何说明：环桌上**内部**译员只有左右两邻，二者都被其话路占用；因此
> 「丙在戊邻位」与「戊为内部译员且话路避开丙-戊缝」不可同时成立。规格里
> 「由**戊转**蓝片」即戊是话路端点（发送/转出者），其另一邻才可留给丙——
> 上表即按此忠实几何构造。

## 运行

```bash
python3 -m unittest tests.test_banquet -v      # 本层 36 个测试
python3 scripts/demo_banquet.py                # 全部具体例，写 exports/banquet/index.json

# 只用真实 726 导出 + 典礼脚本，列合规席图 / 挂起依据
python3 -m banquet seat \
    --products exports/scenario/00_initial/usable.jsonl \
               exports/scenario/00_initial/needs_attention.jsonl \
    --script my_script.json

# 在给定椅图与轮次上模拟一晚，出传译单（无 / 挡屏 / 换席）
python3 -m banquet sheet --script my_script.json --night my_night.json \
    --control screen --seam 丙-戊 --before-round 1
python3 -m banquet sheet --script my_script.json --night my_night.json \
    --control swap --swap 1:丙:0
```

`seat` 退出码恒 0（席图/依据/挂起都打印成 JSON）；`sheet` 仅在传译单可签时返回 0。

## 边界

- 不做语音识别、不做 OCR、不解析图像；谈话是符号化的「两人 + 语言 + 保密标签」。
- 席图上限 8 人、轮次上限 3（穷举排列/路径足够）。
- 只消费导出的产品，不读数据库、不重解文字标签。
