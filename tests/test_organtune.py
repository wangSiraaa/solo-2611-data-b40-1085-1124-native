"""管风琴调律工单的端到端断言：测量层真实快照 + 装配层真实联动 + 封区复演。

运行：python3 -m tests.test_organtune（仓库根目录）。
"""

import shutil
import tempfile

from budget_solver.organtune import (
    Assembly,
    build_card_payload,
    plan,
    query_detent_effect,
    query_natural_interval,
    render_work_order,
    resolve_card,
    rod_break_analysis,
    validate_work_order,
)
from budget_solver.service import BudgetService

from . import organtune_scenarios as S

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh_service():
    tmp = tempfile.mkdtemp(prefix="organ-test-")
    return BudgetService(tmp), tmp


# ---------------------------------------------------------------- 测量层
def test_card_uncertain_pipes_determined_interval():
    """两管各有不确定偏差（都随共同零位 t 变），但差值被唯一确定。

    这是题面核心：``甲 = t``、``乙 = 甲 + 3`` 由测音卡给出；乙、甲自身
    parametric，而 ``乙-甲`` 与 ``甲-乙`` 必须 determined。同一快照只解一次。
    """
    service, tmp = fresh_service()
    try:
        payload = build_card_payload(
            "CARD_PAIR",
            roots=["甲"],
            differences=[("乙", "甲", "3")],
            sums=[("甲", "乙")],
            queries=[("甲", "乙"), ("乙", "甲")],
        )
        res = resolve_card(service, payload)
        check(res["verify_ok"], "测音卡报告独立验证通过")
        check(res["pipes"]["甲"]["status"] == "parametric"
              and res["pipes"]["乙"]["status"] == "parametric",
              "两管自身均不确定（parametric，随共同零位）")
        check(res["intervals"][("甲", "乙")] == {"status": "determined", "value": "3"},
              "乙-甲 被唯一确定 = 3")
        check(res["intervals"][("乙", "甲")] == {"status": "determined", "value": "-3"},
              "乙-甲 反向 = -3")
        check(res["sums"][("甲", "乙")]["status"] == "parametric",
              "和 甲+乙 仍不确定（随 2t）")
        # 单查一对管天然差：同一 solve_id，不会各挑一次误差
        check(res["solve_id"] == res["report"]["solve_id"],
              "前后查询共用同一份解（同一快照）")
    finally:
        shutil.rmtree(tmp)


def test_card_three_pipes_relative_relay():
    """三管测音卡：乙-甲=3、丙-乙=2 ⇒ 丙-甲=5 亦被唯一确定（链式传递）。"""
    service, tmp = fresh_service()
    try:
        payload = build_card_payload(
            "CARD_CHAIN",
            roots=["甲"],
            differences=[("乙", "甲", "3"), ("丙", "乙", "2")],
            queries=[("甲", "丙"), ("甲", "乙")],
        )
        res = resolve_card(service, payload)
        check(res["intervals"][("甲", "丙")]["value"] == "5",
              "链式测音卡：丙-甲 = 5")
        check(res["verify_ok"], "链式测音卡独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------- D 系列
def _run(name):
    rig, kwargs, expected = S.__dict__[name]()
    return rig, kwargs, expected, plan(rig, **kwargs)


def test_d1_connector_delivers():
    rig, kwargs, exp, res = _run("d1_connector")
    check(res["ok"] == exp["ok"], "D1 可交")
    check(res["implementation"] == exp["implementation"], "D1 实现为可脱接头")
    acts = [r["action"] for r in res["shifts"]]
    check(acts == exp["actions"], "D1 工单为 安装→转接脱开→拨栓")
    check(res["shifts"][-1]["shift"] == exp["shifts_used"], "D1 三班内交付")
    v = validate_work_order(rig, res)
    check(v["ok"], "D1 沿真实装配复核通过")
    check(v["seals"].get("甲") == 3, "D1 甲在第 3 班末确已封区")
    check(v["shifts_left"] >= 0, "D1 余班非负")
    # 脱开后甲不再被抬：末班只动乙
    check(list(v["trace"][-1]["moved"]) == ["乙"], "D1 末班只带动乙，甲被冻在 0")


def test_d1_both_chords_hold():
    rig, kwargs, exp, res = _run("d1_connector")
    v = validate_work_order(rig, res)
    for name in ("甲乙", "乙甲"):
        check(v["final"][name]["ok"], f"D1 两套和弦 {name} 到交付仍成立")


def test_d2_materials_after_seal_fails():
    rig, kwargs, exp, res = _run("d2_materials_after_seal")
    check(not res["ok"], "D2 来料班移到两区封盖后，工单失效")
    check(exp["reason_contains"] in res["reason"],
          "D2 诊断指出安装已晚于封区")


def test_d3_abc_connector_two_pipes():
    rig, kwargs, exp, res = _run("d3_abc_connector")
    check(res["ok"] == exp["ok"], "D3 甲乙丙 可交（可脱接头）")
    v = validate_work_order(rig, res)
    check(v["ok"], "D3 沿真实装配复核通过")
    last = v["trace"][-1]
    check(sorted(last["moved"]) == exp["last_moved"],
          "D3 末班一次拨栓同时抬乙、丙两根")
    check(last["pitch"]["甲"] == "0", "D3 甲被封在原音 0")


def test_d3_micro_only_impossible():
    rig, kwargs, exp, res = _run("d3_abc_micro_only")
    check(not res["ok"], "D3 只有一枚微调栓时无法同时抬两根")
    check(exp["reason_contains"] in res["reason"],
          "D3 诊断明确“只有一枚可用微栓，不能假装有第二枚”")


# ---------------------------------------------------------------- C 系列
def test_c1_fixed_rig_delivers():
    rig, kwargs, exp, res = _run("c1_fixed_rig")
    check(res["ok"] == exp["ok"], "C1 旧工装单拨一档即可交")
    check(res["implementation"] == "existing_rig", "C1 未新增任何接头/微栓")
    check(res["shifts"][0]["shift"] == 1, "C1 首班即可调")
    v = validate_work_order(rig, res)
    check(v["ok"], "C1 沿真实装配复核通过")
    check(v["final"]["甲乙"]["ok"] and v["final"]["乙甲"]["ok"],
          "C1 两套和弦（±4）到交付成立")


def test_c1_prime_insufficient():
    rig, kwargs, exp, res = _run("c1_prime_insufficient")
    check(not res["ok"], "C1′ 乙=t+2 时一次不够")
    check(exp["reason_contains"] in res["reason"], "C1′ 诊断说明单档不足")


def test_c2_sealed_before_first_fails():
    rig, kwargs, exp, res = _run("c2_sealed_before_first")
    check(not res["ok"], "C2 乙首班前已封则交不了")
    check(exp["reason_contains"] in res["reason"], "C2 诊断指出首班前已封区")


def test_c3_rod_breaks_previous_chord():
    rig, kwargs, exp, res = _run("c3_rod_breaks_previous_chord")
    check(not res["ok"], "C3 同杆会带坏上一和弦，无法完成")
    check(exp["reason_contains"] in res["reason"], "C3 诊断指出带坏已成立和弦")
    breaks = rod_break_analysis(rig)
    check(any("带坏" in h["message"] for h in breaks),
          "C3 rod_break_analysis 给出耦合证据")


# ---------------------------------------------------------------- 越界/上限
def test_limits_enforced():
    from budget_solver.organtune import OrganError, Rig
    try:
        Rig(pipes={f"p{i}": {"zero": "0", "rod": "R"} for i in range(7)},
            rods={"R": {"knobs": {}}})
        check(False, "七管应被拒绝")
    except OrganError:
        check(True, "至多六管的上限被强制")
    try:
        Rig(pipes={"a": {"zero": "0", "rod": "R"}},
            rods={"R": {"knobs": {f"k{i}": {"coef": "1"} for i in range(5)}}})
        check(False, "五栓应被拒绝")
    except OrganError:
        check(True, "至多四栓的上限被强制")
    try:
        Rig(pipes={"a": {"zero": "0"}}, max_shifts=6)
        check(False, "六班应被拒绝")
    except OrganError:
        check(True, "至多五班的上限被强制")


def test_render_work_order_lists_listen_and_seal():
    rig, kwargs, exp, res = _run("d1_connector")
    text = render_work_order(rig, res)
    check("听" in text and "封区" in text, "工单文本给出每班听哪管、何时封区")


def test_single_queries_natural_interval_and_detent_effect():
    """调律师的单查入口：某对管天然差多少；一枚栓进一格哪些管一同走。"""
    service, tmp = fresh_service()
    try:
        payload = build_card_payload(
            "CARD_Q", roots=["甲"],
            differences=[("乙", "甲", "3")],
            queries=[("甲", "乙")],
        )
        res = resolve_card(service, payload)
        check(query_natural_interval(res, "甲", "乙") == "3",
              "单查 乙-甲 天然相差 3")
        check(query_natural_interval(res, "乙", "甲") == "-3",
              "反向单查 甲-乙 天然相差 -3")
    finally:
        shutil.rmtree(tmp)

    # 一枚栓进一格：当前真实联动下乙、丙一同走
    rig = S.d3_abc_connector()[0]
    eff1 = query_detent_effect(rig, "k", 1)
    check(set(eff1) == {"甲", "乙", "丙"}, "未脱开时拨栓带动整条杆（甲/乙/丙）")
    # 脱开甲之后，拨栓只带乙、丙
    asm = Assembly(rig)
    asm.connectors.add("甲")
    asm.detach("甲")
    eff2 = query_detent_effect(rig, "k", 1, assembly=asm)
    check(set(eff2) == {"乙", "丙"}, "脱开甲后拨栓只带乙、丙（真实联动）")


def test_certified_chord_must_keep_holding():
    """已验和弦到交付时仍须成立：验过之后再出手破坏它，复演必须判失败。"""
    from budget_solver.organtune import Rig
    rig = Rig(
        pipes={"甲": {"zero": "0"},
               "乙": {"zero": "3", "rod": "RE"}},
        rods={"RE": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
        chords=[{"name": "A", "pairs": [["甲", "乙", "4"]], "deadline": 1}],
        max_shifts=5,
    )
    bad = {"ok": True, "implementation": "existing_rig", "shifts": [
        {"shift": 1, "action": "knob", "detail": {"knob": "k", "detent": "1"},
         "listen": ["乙"]},
        {"shift": 2, "action": "knob", "detail": {"knob": "k", "detent": "1"},
         "listen": ["乙"]},
    ]}
    v = validate_work_order(rig, bad)
    check(not v["ok"], "验后又被破坏：复演判失败")
    check(not v["final"]["A"]["ok"], "交付时 A 已不成立")
    # 规划器本身不会产出这种先验后破的工单
    p = plan(rig, allow_connectors=False, allow_micro=False)
    check(p["ok"] and len(p["shifts"]) == 1, "规划器在验成后即交付，不再多拨破坏")


def test_real_linkage_seal_nuance():
    """封区按真实联动判定：同杆上若已把封管脱开，其它管仍可被拨动；否则禁止。

    - 先装接头并脱开甲，再封甲：此后拨杆只带乙，合法；
    - 若没有接头（甲仍随杆），即便只想动乙，拨杆会实际带动已封的甲，禁止。
    """
    from budget_solver.organtune import Rig

    def mk():
        return Rig(
            pipes={"甲": {"zero": "0", "rod": "R"},
                   "乙": {"zero": "0", "rod": "R"}},
            rods={"R": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
            chords=[{"name": "甲乙", "pairs": [["甲", "乙", "1"]], "deadline": 5}],
            regions={"甲区": {"pipes": ["甲"], "seal_after": 2}},
            fixed_pipes=["甲"], max_shifts=5,
        )

    p = plan(mk(), allow_connectors=True)
    check(p["ok"], "脱开甲后封甲，拨杆只带乙，合法")
    v = validate_work_order(mk(), p)
    check(v["ok"] and list(v["trace"][-1]["moved"]) == ["乙"],
          "封区那班拨杆只实际带动乙")

    p2 = plan(mk(), allow_connectors=False, allow_micro=False)
    check(not p2["ok"], "甲未脱开且已封/固定时，拨杆会实际带动它，禁止")


def test_measurement_to_assembly_bridge():
    """测音卡（甲=t, 乙=t+3）→ 相对零位 → C1 工单，端到端串起来。

    测量层：两管自身随自由 t 不确定，但 乙-甲=3 确定；共同零位 t 在音程里抵消。
    装配层：以 甲 为参照取相对零位（甲=0, 乙=3），据此规划 C1 工单并复核。
    """
    from budget_solver.organtune import Rig
    service, tmp = fresh_service()
    try:
        payload = build_card_payload(
            "CARD_C1", roots=["甲"], differences=[("乙", "甲", "3")],
            queries=[("甲", "乙")],
        )
        res = resolve_card(service, payload)
        check(res["verify_ok"], "C1 测音卡独立验证通过")
        check(res["pipes"]["甲"]["status"] == "parametric"
              and res["pipes"]["乙"]["status"] == "parametric",
              "C1 两管随自由 t 不确定")
        check(query_natural_interval(res, "甲", "乙") == "3",
              "C1 天然差 乙-甲=3 被确定（共同零位抵消）")
        # 用相对零位构造装配层（甲为 0 参照）
        rel = res["relatives"]
        rig = Rig(
            pipes={"甲": {"zero": str(rel["甲"])},
                   "乙": {"zero": str(rel["乙"]), "rod": "RE"}},
            rods={"RE": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
            chords=[
                {"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 5},
                {"name": "乙甲", "pairs": [["乙", "甲", "-4"]], "deadline": 5},
            ],
            regions={"乙区": {"pipes": ["乙"], "seal_after": 1}},
            max_shifts=5,
        )
        p = plan(rig, allow_connectors=False, allow_micro=False)
        check(p["ok"], "由测音卡相对零位驱动的 C1 工单可交")
        check(validate_work_order(rig, p)["ok"], "端到端工单沿真实装配复核通过")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_card_uncertain_pipes_determined_interval,
        test_card_three_pipes_relative_relay,
        test_single_queries_natural_interval_and_detent_effect,
        test_measurement_to_assembly_bridge,
        test_certified_chord_must_keep_holding,
        test_real_linkage_seal_nuance,
        test_d1_connector_delivers,
        test_d1_both_chords_hold,
        test_d2_materials_after_seal_fails,
        test_d3_abc_connector_two_pipes,
        test_d3_micro_only_impossible,
        test_c1_fixed_rig_delivers,
        test_c1_prime_insufficient,
        test_c2_sealed_before_first_fails,
        test_c3_rod_breaks_previous_chord,
        test_limits_enforced,
        test_render_work_order_lists_listen_and_seal,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
