"""题面全部试办场景的端到端演示。

运行：python3 -m scripts.run_plaque_demo
产物：state/plaque_cert/ 下的合格包册与两馆事件日志（JSON，可复查）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from plaque_cert.alloc import Allocation, PlaqueSpec, max_plaques
from plaque_cert.halls import CodeState, HallError, HallNetwork, Location, Mode
from plaque_cert.reconcile import (
    Disc,
    ExternalReceipt,
    InternalTransfer,
    Package,
    PeriodDraft,
    Reconciler,
    ReturnVoucher,
)

ARTIFACT_DIR = os.path.join("state", "plaque_cert")


def _ensure_dir() -> None:
    os.makedirs(ARTIFACT_DIR, exist_ok=True)


def _dump(name: str, obj) -> str:
    path = os.path.join(ARTIFACT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
    return path


def _print_booklet(title: str, entries, issues) -> None:
    print(f"\n=== {title} ===")
    for e in entries:
        print(
            f"  {e.code} 包{e.pkg_id} 批{e.batch_id} 卖场{e.store} "
            f"原配{e.original_skus} 退{e.returned_skus} 留存{e.retained_skus} "
            f"可作证={e.eligible_releases} 状态={e.status} {e.note}"
        )
    if issues:
        print("  异常报告：")
        for i in issues:
            print(f"    - 包{i.pkg_id}/码{i.code}: {i.reason}")


def _print_allocation(label: str, allocations: list[Allocation]) -> None:
    total = len(allocations)
    print(f"  [{label}] 最多可发 {total} 张牌，真实盖码单：")
    for a in allocations:
        print(f"    牌 {a.plaque_id} <- 码 {a.codes}")
    return total


# --------------------------------------------------------------------------
# 场景一：甲乙各两码门槛；两笔单盘实售 + 两只双盘包；退一整包 => 最多一张
# --------------------------------------------------------------------------
def scenario_one():
    print("\n" + "=" * 72)
    print("场景一：两张唱片套装各售；整包退一 => 最多一张牌")
    print("=" * 72)
    r = Reconciler("tour-A")
    r.register_disc(Disc("RA1", "recA", "唱片甲"))
    r.register_disc(Disc("RB1", "recB", "唱片乙"))
    # 专码（单盘实售）
    r.register_package(Package("p1", "b1", "S", ["RA1"], "C-A1"))
    r.register_package(Package("p2", "b1", "S", ["RB1"], "C-B1"))
    # 双盘包（每包一个码，可选甲或乙）
    r.register_package(Package("p3", "b1", "S", ["RA1", "RB1"], "C-K1"))
    r.register_package(Package("p4", "b1", "S", ["RA1", "RB1"], "C-K2"))
    for pid in ("p1", "p2", "p3", "p4"):
        pkg = r.packages[pid]
        r.register_receipt(
            ExternalReceipt(f"rc-{pid}", "S", pid, list(pkg.skus), f"ord-{pid}")
        )
    r.register_transfer(
        InternalTransfer("i1", "P-INV", "S-INV", "P", "S", "b1", ("p1", "p2", "p3", "p4"))
    )
    r.register_draft(PeriodDraft("b1", unsold_external=2))  # 整包退 2 盘，期末未对外售出=2
    # 仅整包退掉一只双盘包
    r.register_return(ReturnVoucher("rv-p4", "p4", ("RA1", "RB1"), reprocessed=True))

    entries, issues = r.run()
    _print_booklet("合格包册（逐码写明仍可给哪些唱片作证）", entries, issues)
    assert r.verify_chain(entries), "包册哈希链复查失败"
    assert all(e.status == "eligible" for e in entries if e.code != "C-K2")
    assert next(e for e in entries if e.code == "C-K2").status == "revoked"
    print(f"  补办退回实物另册：{r.reprocessed_summary()}")

    specs = [
        PlaqueSpec("甲牌", 2, frozenset({"RA1"})),
        PlaqueSpec("乙牌", 2, frozenset({"RB1"})),
    ]
    got = _print_allocation("整包退后", max_plaques(entries, specs))
    assert got == 1, f"期望最多 1 张，实得 {got}"
    _dump("scenario1_booklet.json", [asdict(e) for e in entries])
    _dump("scenario1_issues.json", [asdict(i) for i in issues])
    return entries


# --------------------------------------------------------------------------
# 场景二：恢复四码，仅给两个套装码加同牌联号条款 => 也最多一张
# --------------------------------------------------------------------------
def scenario_two():
    print("\n" + "=" * 72)
    print("场景二：原四码恢复；套装码联号（同牌或全不盖）=> 最多一张")
    print("=" * 72)

    def build(with_linkage: bool):
        r = Reconciler("tour-B")
        r.register_disc(Disc("RA1", "recA", "唱片甲"))
        r.register_disc(Disc("RB1", "recB", "唱片乙"))
        r.register_package(Package("p1", "b1", "S", ["RA1"], "C-A1"))
        r.register_package(Package("p2", "b1", "S", ["RB1"], "C-B1"))
        r.register_package(Package("p3", "b1", "S", ["RA1", "RB1"], "C-K1"))
        r.register_package(Package("p4", "b1", "S", ["RA1", "RB1"], "C-K2"))
        for pid in ("p1", "p2", "p3", "p4"):
            pkg = r.packages[pid]
            r.register_receipt(
                ExternalReceipt(f"rc-{pid}", "S", pid, list(pkg.skus), f"ord-{pid}")
            )
        r.register_draft(PeriodDraft("b1", 0))
        entries, _ = r.run()
        specs = [
            PlaqueSpec("甲牌", 2, frozenset({"RA1"})),
            PlaqueSpec("乙牌", 2, frozenset({"RB1"})),
        ]
        linkage = {"LG-1": ("C-K1", "C-K2")} if with_linkage else None
        return r, entries, max_plaques(entries, specs, linkage)

    r0, entries0, no_link = build(False)
    n0 = _print_allocation("无联号（原可两张都发）", no_link)
    assert n0 == 2

    r1, entries1, linked = build(True)
    n1 = _print_allocation("套装码同牌联号（不可拆）", linked)
    assert n1 == 1, f"联号后期望最多 1 张，实得 {n1}"
    group = set(linked[0].codes)
    assert {"C-K1", "C-K2"}.issubset(group), "联号组必须同盖一张牌"
    assert r1.verify_chain(entries1)
    _dump("scenario2_booklet.json", [asdict(e) for e in entries1])
    return entries1


# --------------------------------------------------------------------------
# 场景三：限十二包五牌；内售发票齐、外售缺席的拒绝；再版归并；底稿对不上
# --------------------------------------------------------------------------
def scenario_three():
    print("\n" + "=" * 72)
    print("场景三：十二包五牌上限 + 内售/外售拒绝 + 再版归并 + 底稿对账")
    print("=" * 72)
    r = Reconciler("tour-C")
    r.register_disc(Disc("R1", "rec1", "首版"))
    r.register_disc(Disc("R1R", "rec1", "再版"))  # 同一录音，再版独立发行
    r.register_disc(Disc("R2", "rec2", "另一张"))
    # 12 个包：8 个 R1 包、2 个再版包、2 个 R2 包
    plan = [("R1", 8), ("R1R", 2), ("R2", 2)]
    idx = 0
    for sku, n in plan:
        for _ in range(n):
            idx += 1
            pid = f"p{idx}"
            r.register_package(Package(pid, "b1", "S", [sku], f"C{idx:02d}"))
            r.register_receipt(
                ExternalReceipt(f"rc-{pid}", "S", pid, [sku], f"ord-{pid}")
            )
    assert len(r.packages) == 12
    # 超规模登记必须被拒
    try:
        r.register_package(Package("p13", "b1", "S", ["R1"], "C13"))
        raise AssertionError("十二包上限未生效")
    except ValueError as exc:
        print(f"  上限拦截：{exc}")

    r.register_draft(PeriodDraft("b1", unsold_external=0))
    entries, issues = r.run()
    assert not issues
    specs = [PlaqueSpec(f"牌{i}", 2, frozenset({"R1", "R1R"})) for i in range(5)]
    got = max_plaques(entries, specs)
    n = _print_allocation("再版归并许可下（R1R 可计入 R1 牌）", got)
    assert n == 5
    used = {c for a in got for c in a.codes}
    assert len(used) == 10 and len(r.packages) == 12, "五牌共耗十码，余两码"

    # 再版归并许可缺失：再版码不能计入首版牌
    specs_no_merge = [PlaqueSpec(f"牌{i}", 2, frozenset({"R1"})) for i in range(5)]
    got2 = max_plaques(entries, specs_no_merge)
    n2 = _print_allocation("撤销再版归并许可后", got2)
    assert n2 == 4, f"8 个首版码只够 4 张牌，实得 {n2}"

    # 内售发票齐、实际外售缺席
    print("\n  -- 内售发票齐、实际外售缺席 --")
    r2 = Reconciler("tour-C2")
    r2.register_disc(Disc("R1", "rec1", "首版"))
    r2.register_package(Package("q1", "b2", "S", ["R1"], "D01"))
    r2.register_package(Package("q2", "b2", "S", ["R1"], "D02"))
    r2.register_transfer(InternalTransfer("i9", "P-INV", "S-INV", "P", "S", "b2", ("q1", "q2")))
    # 故意不登记任何外部收银凭据
    e2, i2 = r2.run()
    _print_booklet("核单结果", e2, i2)
    assert all(e.status == "revoked" for e in e2)
    assert any("实际外售缺席" in x.reason for x in i2)
    assert max_plaques(e2, [PlaqueSpec("牌X", 1, frozenset({"R1"}))]) == []

    # 旧底稿期末声明对不上
    print("\n  -- 旧底稿只记未售数，逐批核对 --")
    r3 = Reconciler("tour-C3")
    r3.register_disc(Disc("R1", "rec1", "首版"))
    r3.register_package(Package("z1", "b3", "S", ["R1"], "E01"))
    r3.register_receipt(ExternalReceipt("rc-z1", "S", "z1", ["R1"], "o1"))
    r3.register_draft(PeriodDraft("b3", unsold_external=1))  # 实际全售出 => 隐含售出 0，对不上
    e3, i3 = r3.run()
    _print_booklet("核单结果", e3, i3)
    assert any("对不上" in x.reason for x in i3)
    _dump("scenario3_booklet.json", [asdict(e) for e in entries])
    _dump("scenario3_issues.json", [asdict(x) for x in issues + i2 + i3])


# --------------------------------------------------------------------------
# 两馆试办：西->东 两码在途；途中异馆收到退单；两种到点状态对照
# --------------------------------------------------------------------------
def _hall_trial(mode: Mode):
    label = "离线具名模具" if mode == Mode.OFFLINE_MOLD else "联网签印台"
    print("\n" + "-" * 72)
    print(f"两馆试办（{label} 模式）：联号两码 西馆->东馆，途中异馆收到退单")
    print("-" * 72)
    linkage = {"LG-X": ("K1", "K2")}
    common_events = []

    def fresh():
        net = HallNetwork(mode, linkage)
        net.admit_code("K1", Location.WEST.value)
        net.admit_code("K2", Location.WEST.value)
        if mode == Mode.OFFLINE_MOLD:
            net.set_mold_holder("LG-X", Location.WEST.value)
        return net

    # 情形一：东馆只有全量拟成交包册（预排），无模具/最终授权
    net = fresh()
    net.schedule("O1", Location.EAST.value, "牌T", ("K1", "K2"), t=1)
    ho = net.handover(("K1", "K2"), Location.WEST.value, Location.EAST.value, t=2)
    net.return_arrives_non_holder("K1", Location.EAST.value, t=3)  # 东是非持馆（持馆=西）
    assert net.state["K1"] == CodeState.UNENGRAVED, "非持馆退单不得提前阻止在途承诺"
    try:
        net.authorize("O1", t=4)
        raise AssertionError("无实物/无授权预排竟然可刻印")
    except HallError as exc:
        print(f"  情形一 东馆预排授权被拒：{exc}")
    net.complete_handover(ho, t=5)
    if mode == Mode.OFFLINE_MOLD:
        net.authorize("O1", t=6)  # 到馆且模具随组交接 => 可授权
    else:
        net.authorize("O1", t=6)
    net.stamp("O1", t=7)
    net.holder_processes_return("K1", t=8)  # 持馆此刻才办理
    assert net.state["K1"] == CodeState.WITHDRAWN
    print("  情形一 结论：东馆仅持包册时不能刻；实物+模具/最终授权到点后，")
    print("          退单尚在异馆、未为持馆所知，已到点的合法承诺在东馆实刻；")
    print("          其后持馆办理退盘，牌T记撤证，K1/K2 永久不还。")

    # 情形二：东馆已接收真实模具（离线）/ 已拿最终授权（联网），但持馆先办退
    net2 = fresh()
    net2.schedule("O2", Location.EAST.value, "牌U", ("K1", "K2"), t=1)
    ho2 = net2.handover(("K1", "K2"), Location.WEST.value, Location.EAST.value, t=2)
    net2.complete_handover(ho2, t=3)
    if mode == Mode.OFFLINE_MOLD:
        net2.set_mold_holder("LG-X", Location.EAST.value)
    net2.authorize("O2", t=4)
    # 退单先到非持馆（此时东已持模具/授权）
    net2.return_arrives_non_holder("K1", Location.WEST.value, t=5)
    # 持馆在落印前办理退盘
    net2.holder_processes_return("K1", t=6)
    if mode == Mode.ONLINE_SEAL:
        try:
            net2.stamp("O2", t=7)
            raise AssertionError("签印台已受理退盘后仍可落印")
        except HallError as exc:
            print(f"  情形二 落印被拒：{exc}")
    else:
        try:
            net2.stamp("O2", t=7)
            raise AssertionError("暂停新刻后仍可落印")
        except HallError as exc:
            print(f"  情形二 落印被拒：{exc}")
    assert net2.state["K1"] == CodeState.REVOKED
    assert net2.state["K2"] == CodeState.REVOKED  # 联号组同退整组资格
    print("  情形二 结论：持馆办理时刻（t=6）起暂停其后新刻；两码未刻，")
    print("          均为已撤销资格而非已刻，出库名单可与‘永远不能再刻’区分。")
    print("\n  出库名单：")
    for row in net2.outbound_list():
        print(f"    {row.code}: {row.state:<10} 位置={row.location:<10} 分类={row.classification}")
    _dump(f"hall_{mode.value}_events.json",
          [{"t": t, "kind": k, "detail": d} for t, k, d in net.events + net2.events])
    return net, net2


# --------------------------------------------------------------------------
# 联号解除：仍有效的独立码可单独换馆；已刻牌退盘只撤证不还码
# --------------------------------------------------------------------------
def scenario_dissolve():
    print("\n" + "=" * 72)
    print("联号解除演示：独立码单独换馆；已刻后退盘只撤证")
    print("=" * 72)
    net = HallNetwork(Mode.OFFLINE_MOLD, {"LG-Y": ("M1", "M2")})
    net.admit_code("M1", Location.WEST.value)
    net.admit_code("M2", Location.WEST.value)
    net.set_mold_holder("LG-Y", Location.WEST.value)
    # M1 先在西馆独立成牌（解除联号后两码各自独立；M2 保持未刻备用）
    net.dissolve_group("LG-Y", t=2)
    net.schedule("O3b", Location.WEST.value, "牌V", ("M1",), t=3)
    net.authorize("O3b", t=4)
    net.stamp("O3b", t=5)
    net.holder_processes_return("M1", t=6)
    assert net.state["M1"] == CodeState.WITHDRAWN
    print("  M1 已刻入牌V后退盘 -> 牌V撤证，M1 码不发还；M2 从未落刻仍有效")
    # M2 解除联号后作为独立码单独换馆
    ho3 = net.handover(("M2",), Location.WEST.value, Location.EAST.value, t=7)
    net.complete_handover(ho3, t=8)
    net.schedule("O4", Location.EAST.value, "牌W", ("M2",), t=9)
    net.authorize("O4", t=10)
    net.stamp("O4", t=11)
    assert net.state["M2"] == CodeState.ENGRAVED and net.plaque_of["M2"] == "牌W"
    print("  解除联号后 M2 作为独立码单独交东馆，重新刻入牌W")
    for row in net.outbound_list():
        print(f"    {row.code}: {row.state:<10} 牌={row.plaque_id} 分类={row.classification}")
    _dump("hall_dissolve_events.json",
          [{"t": t, "kind": k, "detail": d} for t, k, d in net.events])


def main() -> None:
    _ensure_dir()
    scenario_one()
    scenario_two()
    scenario_three()
    _hall_trial(Mode.OFFLINE_MOLD)   # 采用：馆持有具名联号模具并独占作业
    _hall_trial(Mode.ONLINE_SEAL)    # 对照：联网签印台，事件等价
    scenario_dissolve()
    print("\n全部断言通过；产物目录：", ARTIFACT_DIR)


if __name__ == "__main__":
    main()
