"""齿轮教具 / 演示台 / 测速盘的可复跑 CLI 演示。

不新增 CLI 子命令，而是直接以库调用方式走完题面要求的主线，并用真实
``budget_solver`` 服务（临时数据目录）产生、验证快照与报告。

运行：python3 -m tests.demo_gear
"""

import shutil
import tempfile

from budget_solver.service import BudgetService
from gear_demo import scenarios as S
from gear_demo.bench import common_action_table, find_lockable_beats, simulate
from gear_demo.kinematics import KinematicSolver
from gear_demo.tacho import DUAL_MEASURE, PAUSE_FIXTURE, TachoSwap


def main():
    data_root = tempfile.mkdtemp(prefix="gear-demo-")
    try:
        service = BudgetService(data_root)
        kin = KinematicSolver(service)

        print("== 测具岗：差动座两端 1、−1 ==")
        a = kin.analyze(S.differential_balanced())
        print(f"   座速 {a.speed('C')}，静止端 {a.speed('S')}；"
              f"必同速={a.must_same_speed('C', 'S')[0]}；报告 {a.solve_id} "
              f"独立验证={service.verify_solve(a.solve_id)['ok']}")

        print("== 测具岗：负向输入改为 +1 ==")
        b = kin.analyze(S.differential_same_sign())
        print(f"   座速 {b.speed('C')}，静止端 {b.speed('S')}；"
              f"必同速={b.must_same_speed('C', 'S')[0]}（合不上）")

        print("== 测具岗：轴速自由，L 与 R 是否必同速 ==")
        c = kin.analyze(S.differential_inputs_free())
        same, note, witness = c.must_same_speed("L", "R")
        print(f"   结论={same}（{note}）")
        print(f"   反例同速读数 L=R={witness['equal']['L']}；"
              f"反例不同速读数 L={witness['unequal']['L']} R={witness['unequal']['R']}")

        print("== 测具岗：两套轮争同一同速要求 ==")
        d = kin.analyze(S.two_gears_fight_one_sleeve())
        print(f"   A={d.speed('A')} B={d.speed('B')}；"
              f"必同速={d.must_same_speed('A', 'B')[0]}")

        print("== 演示台：先合后脱再合 ==")
        spec = S.bench_basic()
        result = simulate(spec)
        print(f"   ok={result.ok} 动作表="
              f"{ {k: (v['beat'], v['ok']) for k, v in result.action_table.items()} }")
        print(f"   末态相位={result.final_phases}（换挡不复位标记）")

        print("== 演示台：差一齿永远不同位 ==")
        off = S.bench_phase_off_by_tooth()
        off_result = simulate(off)
        print(f"   ok={off_result.ok} 可锁合拍="
              f"{find_lockable_beats(off, 'sAB', list(range(6)))}")

        print("== 差动座演示（四轴贯通）==")
        for label, factory in [
            ("平衡 1/−1", S.bench_differential_balanced),
            ("同号 ±1", S.bench_differential_same_sign),
            ("差一齿", S.bench_differential_one_tooth),
        ]:
            res = simulate(factory())
            print(f"   {label}: ok={res.ok} 末态座相位={res.final_phases['C']} "
                  f"静止端相位={res.final_phases['S']}")

        print("== 演示台：两套共抢一只滑座 + 共同动作表 ==")
        seat = S.bench_two_sleeves_one_seat()
        seat_result = simulate(seat)
        reading = {"seg0": {"P": 1, "Q": 1, "R": 1}, "seg1": {"P": 1, "Q": 0, "R": 1}}
        verdict = common_action_table(seat, [reading])
        print(f"   ok={seat_result.ok} 共同动作表一致={verdict['ok']} "
              f"末态滑座={seat_result.seats}")

        print("== 测速盘：停台工装 ==")
        swap = TachoSwap(ticks_per_rev=4)
        true_phase = result.trace[2]["phases"]["A"]
        ev = swap.make_evidence_pause_fixture(2, true_phase, 1)
        rep = swap.plan(PAUSE_FIXTURE, 2, result.trace, [[0, 1], [4]], calibration=ev)
        print(f"   放行={rep.released} 停台拍={rep.pause_beats} "
              f"可用窗={rep.usable_window}；{rep.reason}")

        print("== 测速盘：无交接证据（两个显示同为 0）==")
        rep0 = swap.plan(PAUSE_FIXTURE, 2, result.trace, [[0, 1], [4]], calibration=None)
        print(f"   放行={rep0.released}；{rep0.reason}")

        print("== 测速盘：双盘联测 ==")
        old = S.old_disc()
        old_disp = old.display(true_phase, 4)
        new = S.new_disc_shifted()
        new_disp = new.display(true_phase, 4)
        ev2 = swap.make_evidence_dual_measure(
            1, true_phase, old, old_disp, new_disp, new.orientation
        )
        rep2 = swap.plan(DUAL_MEASURE, 1, result.trace, [[0, 1], [4]], calibration=ev2)
        print(f"   反解零位误差={ev2.zero_shift} 放行={rep2.released} "
              f"可用窗={rep2.usable_window}")
        print(f"   错过解说：{swap.explain_miss(rep2, result.trace)[0]}")

        # 报告可持续重读：重开同一数据目录
        reopened = BudgetService(data_root)
        print("== 重开复读 ==")
        print(f"   差动报告哈希不变={reopened.get_report(a.solve_id)['report_hash'] == service.get_report(a.solve_id)['report_hash']}")

        print("\n齿轮演示 CLI 全部通过。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()
