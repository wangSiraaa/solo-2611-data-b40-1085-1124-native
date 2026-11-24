"""万花筒芯演示：普通工单、校错见证、卡锁、同槽争色询证、巡展尾签承诺。

运行：python3.11 demo_kaleido.py
所有断言通过、退出码 0，即说明题面的预期全部兑现。
色名一律用本地色名（red/blue），最多六槽、四张谱、四步。
"""

from __future__ import annotations

from collections import Counter

from library.kaleido import (
    Chart,
    ChartDesk,
    F,
    Geometry,
    N,
    Observation,
    ProgramStep,
    RotationPlan,
    Tail,
    View,
    Workshop,
    exhibition_plan,
    format_assembly,
    synthesize,
    verify_run,
)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 一、普通单筒工单（不发额外尾签）
# ---------------------------------------------------------------------------

def single_tube_scenario() -> None:
    banner("一、普通单筒：近盘两槽、远盘全空、窗口每次只看近盘槽0，起位0拨一步见槽1")

    geometry = Geometry(near_size=2, far_size=0)
    inventory = Counter(red=1, blue=1)

    # 馆谱证明起位（槽0）红；同朝向的见证证实槽1蓝
    library_chart = Chart.fixed(
        "馆谱-起位红", [Observation((N(0),), "red", "起位")]
    )
    witness_blue = Chart.fixed(
        "见证-槽1蓝", [Observation((N(1),), "blue", "槽1")]
    )

    program = [ProgramStep("red", "起位看红"), ProgramStep("blue", "拨一步看蓝")]
    plan = RotationPlan(initial_near=0, transitions=[{"near_delta": 1}])

    result = synthesize(geometry, [library_chart, witness_blue], program, plan, inventory)
    print("综合结果:", result.status, "装配单:", format_assembly(result.assembly))
    for step in result.steps:
        v = step["views"][0]
        print(f"  第{step['step']}步 {step['label']}: 看 {v['line']} -> {v['observed']} "
              f"(要求 {v['target']})")
    assert result.status == "ok"
    assert result.assembly == {N(0): "red", N(1): "blue"}
    assert visible_sequence_ok(result, ["red", "blue"])

    # 只把第二张见证校为红：蓝段不成（同一槽1被同时要求蓝与红）
    witness_red = Chart.fixed("见证-槽1红", [Observation((N(1),), "red", "槽1")])
    bad = synthesize(geometry, [library_chart, witness_red], program, plan, inventory)
    print("\n见证改为红 ->", bad.status, bad.reason, bad.conflict)
    assert bad.status == "infeasible"
    assert bad.reason == "color_conflict"
    assert bad.conflict["slot"] == "近1"
    assert set(bad.conflict["colors"]) == {"blue", "red"}

    # 恢复见证，只把近盘卡在起位：同样交不了（近层被锁，第二步仍只看槽0=红）
    locked_plan = RotationPlan(
        initial_near=0, transitions=[{"near_delta": 1, "near_locked": True}]
    )
    locked = synthesize(geometry, [library_chart, witness_blue], program, locked_plan, inventory)
    print("\n近盘卡在起位 ->", locked.status, locked.reason, "motion=", locked.motion)
    assert locked.status == "infeasible"
    assert locked.reason == "motion_conflict"
    assert locked.motion is True


def visible_sequence_ok(result, expected: list) -> bool:
    observed = [step["views"][0]["observed"] for step in result.steps]
    return observed == expected


# ---------------------------------------------------------------------------
# 二、制谱台询证：必定 / 可能 / 抵触 + 可复看装芯例
# ---------------------------------------------------------------------------

def desk_inquiries() -> None:
    banner("二、制谱台询证：必定 / 可能 / 抵触，并给可复看的装芯例（≤6槽、≤4谱）")
    geometry = Geometry(near_size=3, far_size=0)
    inventory = Counter(red=2, blue=1)
    desk = ChartDesk(geometry, inventory)

    # 两张局部分别可装的谱：A 只约束槽0/槽1，B 只约束槽0/槽2
    chart_a = Chart.fixed(
        "谱A", [Observation((N(0),), "red"), Observation((N(1),), "blue")]
    )
    chart_b = Chart.fixed(
        "谱B", [Observation((N(0),), "blue"), Observation((N(2),), "red")]
    )

    # 每张谱单独都装得出：可复看的装芯例
    a_alone = desk.ask([chart_a], N(1), "blue")
    b_alone = desk.ask([chart_b], N(2), "red")
    print("谱A 单独:", a_alone["verdict"], "装芯例:", format_assembly(a_alone["witness"]))
    print("谱B 单独:", b_alone["verdict"], "装芯例:", format_assembly(b_alone["witness"]))
    assert a_alone["verdict"] == "certain"
    assert b_alone["verdict"] == "certain"
    assert a_alone["witness"][N(0)] == "red"
    assert b_alone["witness"][N(0)] == "blue"  # B 单独可把槽0装蓝

    # 同一芯却争同槽：A 要槽0红、B 要槽0蓝 -> 抵触，并指出冲突槽
    joint = desk.ask([chart_a, chart_b], N(0), "red")
    print("\n谱A+谱B 同芯询证 ->", joint["verdict"])
    print("  最小冲突:", joint["conflict"])
    assert joint["verdict"] == "contradiction"
    assert joint["conflict"]["detail"]["slot"] == "近0"
    assert set(joint["conflict"]["detail"]["colors"]) == {"blue", "red"}

    # 三种判定齐全：必定 / 可能 / 抵触
    certain = desk.ask([chart_a], N(0), "red")
    possible = desk.ask([chart_a], N(2), "red")   # A 不约束槽2，可能红也可能空
    contradict = desk.ask([chart_a], N(0), "blue")
    print("\n必定:", certain["verdict"], "| 可能:", possible["verdict"],
          "| 抵触:", contradict["verdict"])
    assert certain["verdict"] == "certain"
    assert possible["verdict"] == "possible"
    assert "counter_witness" in possible            # 可复看的反例
    assert contradict["verdict"] == "contradiction"
    assert contradict["reason"] == "slot_cannot_take_color"

    # 安装朝向：同一谱两朝向，只有一个朝向能让工单成立
    ori0 = [Observation((N(0),), "red", "朝向0")]
    ori1 = [Observation((N(1),), "red", "朝向1")]
    chart_ori = Chart("双朝向谱", [ori0, ori1])
    program = [ProgramStep("blue", "s0"), ProgramStep("red", "s1")]
    plan = RotationPlan(initial_near=0, transitions=[{"near_delta": 1}])
    picked = synthesize(geometry, [chart_ori], program, plan, inventory)
    print("\n朝向求解:", picked.status, "选中朝向:",
          [o.target + "@" + str(o.slots) for o in picked.chosen["双朝向谱"]])
    assert picked.status == "ok"
    assert picked.assembly == {N(0): "blue", N(1): "red"}


# ---------------------------------------------------------------------------
# 三、巡展：单封芯 / 备用芯与最大可承诺尾签集
# ---------------------------------------------------------------------------

def tour_scenarios() -> None:
    banner("三、巡展小盘：单可见槽、红尾/蓝尾各一步、库存红蓝各一、两个空筒")

    geometry = Geometry(near_size=1, far_size=0)
    red_tail = Tail("红尾", [ProgramStep("red", "红尾一步")])
    blue_tail = Tail("蓝尾", [ProgramStep("blue", "蓝尾一步")])
    tails = [red_tail, blue_tail]

    # (a) 未装芯的“也许红也许蓝”不能算两种：两色各只是“可能”，不构成承诺
    desk = ChartDesk(geometry, Counter(red=1, blue=1))
    maybe_red = desk.ask([], N(0), "red")
    maybe_blue = desk.ask([], N(0), "blue")
    print("空芯问红:", maybe_red["verdict"], "| 空芯问蓝:", maybe_blue["verdict"])
    assert maybe_red["verdict"] == "possible"
    assert maybe_blue["verdict"] == "possible"
    # 封筒后只剩一种具体颜色：未装芯一枚芯至多兑现一尾
    empty_covers = [t.name for t in tails if operation_fulfills(geometry, {}, t)]
    print("空芯(未装)能兑现的尾签:", empty_covers)
    assert empty_covers == []

    # (b) 交付 A：只封一枚芯，最多承诺 1 尾（红或蓝择一）
    plan_a = exhibition_plan(tails, geometry, Counter(red=1, blue=1))
    print("\n交付选择:", plan_a["delivery"], "最大可承诺:", plan_a["commitment"])
    print("  单封芯最大:", plan_a["single"]["covered"],
          "装配单:", format_assembly(plan_a["single"]["assembly"]))
    assert plan_a["single"]["size"] == 1

    # (c) 交付 B：两枚备用芯，红蓝各一珠，可同时承诺两尾
    assert plan_a["delivery"] == "B_spare_cores"
    assert plan_a["size"] == 2
    assert plan_a["commitment"] == ["红尾", "蓝尾"]
    for asm in plan_a["assemblies"]:
        print("  备用芯装配:", format_assembly(asm))

    # (d) 临借走唯一蓝珠：蓝尾失去可承诺资格，集合缩到 {红尾}
    borrowed = Counter(red=1)  # 蓝珠已借出
    plan_after = exhibition_plan(tails, geometry, borrowed)
    print("\n借走唯一蓝珠后 -> 交付:", plan_after["delivery"],
          "可承诺:", plan_after["commitment"],
          "装配单:", [format_assembly(a) for a in plan_after["assemblies"]])
    assert plan_after["commitment"] == ["红尾"]
    assert "蓝尾" not in plan_after["commitment"]

    # 交付 B 的装配不得把同一枚珠填进两枚芯（红珠只有一枚）
    single_red = max_spare_cores_no_dup(tails, geometry, Counter(red=1))
    assert single_red["covered"] == ["红尾"]
    print("蓝珠缺货时备用芯方案:", single_red["covered"])

    # 若红蓝都缺货，任何交付都承诺不了
    empty_plan = exhibition_plan(tails, geometry, Counter())
    print("两珠都缺货 -> 可承诺:", empty_plan["commitment"])
    assert empty_plan["commitment"] == []


def operation_fulfills(geometry, assembly, tail) -> bool:
    from library.kaleido import core_fulfills
    return core_fulfills(geometry, assembly, tail)


def max_spare_cores_no_dup(tails, geometry, inventory):
    from library.kaleido import max_spare_cores
    return max_spare_cores(tails, geometry, inventory, max_cores=2)


# ---------------------------------------------------------------------------
# 四、工坊（library 化）：借用副本扇区表、共用实珠库存与回执
# ---------------------------------------------------------------------------

def workshop_scenario() -> None:
    banner("四、教具坊：按领到副本的扇区表取谱、全局实珠扣减与取件回执")

    geometry = Geometry(near_size=2, far_size=0)
    workshop = Workshop(
        inventory=Counter(red=1, blue=1), geometry=geometry
    )

    # 副本 C1 所附扇区表（本制版）：格 c0 回映近0=红，格 c1 回映近1=蓝
    workshop.attach_sector_table(
        "C1", "v1", {
            "c0": {"slots": [["N", 0]], "color": "red"},
            "c1": {"slots": [["N", 1]], "color": "blue"},
        },
    )
    charts = workshop.charts_for("C1", "v1", geometry)
    print("副本 C1@v1 摘出的谱:", charts[0].name)
    q = workshop.inquire("Q1", charts, N(0), "red")
    print("询证 近0=红 ->", q["verdict"], "装芯例:", format_assembly(q["witness"]))
    assert q["verdict"] == "certain"

    program = [ProgramStep("red", "起位看红"), ProgramStep("blue", "拨一步看蓝")]
    plan = RotationPlan(initial_near=0, transitions=[{"near_delta": 1}])
    sol = workshop.synthesize("W1", charts, program, plan)
    print("综合 ->", sol.status, "装配单:", format_assembly(sol.assembly))
    assert sol.status == "ok"

    # 装配扣减共用库存；同一枚蓝珠不能填进两枚芯
    ok = workshop.commit_assembly("CORE-1", [sol.assembly])
    print("登记 CORE-1 ->", ok["status"], "余量:", ok["inventory_after"])
    assert ok["status"] == "ok"
    overuse = workshop.commit_assembly(
        "CORE-2", [{N(0): "blue"}, {N(0): "blue"}]
    )
    print("两枚芯各要一枚蓝珠 ->", overuse["status"], overuse["error"]["reason"])
    assert overuse["status"] == "rejected"
    assert overuse["error"]["reason"] == "insufficient_beads"
    # 拒绝不扣减：重放同一 op 原样返回，且库存不变
    replay = workshop.commit_assembly("CORE-2", [{N(0): "blue"}, {N(0): "blue"}])
    assert replay == overuse

    # 回执可复看
    print("回执条数:", len(workshop.receipts))
    assert any(r["op_id"] == "Q1" for r in workshop.receipts)
    assert any(r["op_id"] == "W1" for r in workshop.receipts)


def main() -> None:
    single_tube_scenario()
    desk_inquiries()
    tour_scenarios()
    workshop_scenario()
    banner("全部断言通过")


if __name__ == "__main__":
    main()
