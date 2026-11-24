"""团体追逐实时执裁训练场景与断言。

数值均为手算可复核的精确分数。时间轴以判定段起点为 0，单位是“拍”。
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.pursuit import PursuitService
from budget_solver.rationals import qstr


PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh_service():
    tmp = tempfile.mkdtemp(prefix="pursuit-test-")
    return PursuitService(tmp), tmp


def full_grid(grid_id, riders, times, values, stage="initial", recorded_at="0"):
    assignments = {}
    for rider in riders:
        assignments[f"p_{rider}_t0"] = qstr(values[rider][0])
        for i in range(len(times) - 1):
            velocity = (values[rider][i + 1] - values[rider][i]) / (
                Fraction(times[i + 1]) - Fraction(times[i])
            )
            assignments[f"v_{rider}_i{i}"] = qstr(velocity)
    return {
        "grid_id": grid_id,
        "recorded_at": recorded_at,
        "submitted": stage,
        "assignments": assignments,
    }


def base_event(event_id, model_id, stop="4", hold="2"):
    return {
        "event_id": event_id,
        "model_id": model_id,
        "mode": "real_time",
        "teams": ["A", "B"],
        "chasing_team": "A",
        "riders": {
            "A": ["A1", "A2", "A3"],
            "B": ["B1", "B2", "B3"],
        },
        "times": ["0", "1", "2", "3", "4"],
        "actual_stop": stop,
        "required_hold": hold,
        "evidence": [],
        "grid": [],
    }


def third_person_trace_case(stop="4", hold="2", a3_last="4"):
    event = base_event("E_THIRD", "M_THIRD", stop=stop, hold=hold)
    values = {
        # 前两人始终高于第三人；B 第三人恒为 2。
        "A1": [Fraction(12 + i) for i in range(5)],
        "A2": [Fraction(11 + i) for i in range(5)],
        "A3": [Fraction(a3_last) * i / 4 for i in range(5)],
        "B1": [Fraction(13 + i) for i in range(5)],
        "B2": [Fraction(12 + i) for i in range(5)],
        "B3": [Fraction(2)] * 5,
    }
    riders = event["riders"]["A"] + event["riders"]["B"]
    event["grid"].append(full_grid("G1", riders, event["times"], values))
    event["evidence"] = [
        {"evidence_id": "L_A3_0", "type": "line", "rider": "A3",
         "recorded_at": "0", "submitted": "initial", "distance": "0"},
        {"evidence_id": "L_B3_0", "type": "line", "rider": "B3",
         "recorded_at": "0", "submitted": "initial", "distance": "2"},
        {"evidence_id": "L_A3_LAST", "type": "line", "rider": "A3",
         "recorded_at": "4", "submitted": "initial", "distance": a3_last},
        {"evidence_id": "L_B3_LAST", "type": "line", "rider": "B3",
         "recorded_at": "4", "submitted": "initial", "distance": "2"},
    ]
    return event, values


def test_caught_exactly_two_beats():
    service, tmp = fresh_service()
    try:
        event, _ = third_person_trace_case(stop="4", hold="2", a3_last="4")
        service.intake_event(event)
        verdict = service.judge("E_THIRD", "initial")
        check(verdict["verdict"] == "determined_caught", "甲从 2 追到 4，维持 2 拍判定追及")
        review = verdict["trace_reviews"][0]
        check(review["qualified_intervals"] == [
            {"start": "2", "end": "4", "length": "2"}], "达标区间恰为 [2,4]")
        check(verdict["bell_at"] == "4", "所有完整车迹均在 4 拍追足，实时鸣铃于 4")
    finally:
        shutil.rmtree(tmp)


def test_late_correction_cannot_be_extrapolated():
    service, tmp = fresh_service()
    try:
        # 首版：末点仍写 4，但校准档尚未到齐；实时只能待定，不能借线性模型自行补全。
        event, _ = third_person_trace_case(stop="4", hold="2", a3_last="4")
        event["grid"] = []
        service.intake_event(event)
        first = service.judge("E_THIRD", "initial")
        check(first["verdict"] == "pending", "校准档未齐时首报待定")
        first_id = first["judgment_id"]

        # 迟交修正：真实末位移是 3，甲=3t/4；过线单也是实际进行时已拍下的材料。
        late_event = service.get_event("E_THIRD")
        corrected_values = {
            "A1": [Fraction(12 + i) for i in range(5)],
            "A2": [Fraction(11 + i) for i in range(5)],
            "A3": [Fraction(3) * i / 4 for i in range(5)],
            "B1": [Fraction(13 + i) for i in range(5)],
            "B2": [Fraction(12 + i) for i in range(5)],
            "B3": [Fraction(2)] * 5,
        }
        riders = late_event["riders"]["A"] + late_event["riders"]["B"]
        corrected_line = {
            "evidence_id": "L_A3_LAST_CORRECTED",
            "type": "line",
            "rider": "A3",
            "recorded_at": "4",
            "submitted": "late",
            "distance": "3",
            "supersedes": "L_A3_LAST",
        }
        late_grid = full_grid("G_LATE", riders, late_event["times"],
                              corrected_values, stage="late", recorded_at="0")
        late_grid["received_at"] = "8/3"  # 赛中已拍下，8/3 才交到计时岗
        service.append_late_material(
            "E_THIRD",
            evidence=[corrected_line],
            grid_points=[late_grid]
        )

        corrected = service.judge("E_THIRD", "late")
        review = corrected["trace_reviews"][0]
        check(corrected["verdict"] == "determined_not_caught",
              "末位移 3 时最大连续追及时长 4/3，不足 2 拍")
        check(review["qualified_intervals"] == [] and review["maximum_observed_hold"] == "4/3",
              "给出相反见证：最长非负位差区间为 4/3")
        check(service.get_judgment(first_id)["verdict"] == "pending",
              "旧判定仍可读，允许只更正一个原结论")
    finally:
        shutil.rmtree(tmp)


def test_actual_ride_to_five_can_count():
    service, tmp = fresh_service()
    try:
        event, values = third_person_trace_case(stop="5", hold="2", a3_last="4")
        # 追加第五拍证据；车迹为常速，5 是准许测时点。
        event["times"] = ["0", "1", "2", "3", "4", "5"]
        for rider, row in values.items():
            extra = row[-1] + (row[1] - row[0])
            if rider == "A3":
                extra = row[-1]  # 4 至 5 拍静止，不改变 4 拍已成立的追及区间
            row.append(extra)
        for evidence in event["evidence"]:
            evidence["recorded_at"] = "5" if evidence["evidence_id"].endswith("LAST") else "0"
        riders = event["riders"]["A"] + event["riders"]["B"]
        event["grid"] = [full_grid("G1", riders, event["times"], values)]
        service.intake_event(event)
        verdict = service.judge("E_THIRD", "initial")
        check(verdict["verdict"] == "determined_caught" and verdict["bell_at"] == "4",
              "实际骑到 5 且有测时证据时，[2,4] 可计足两拍")
    finally:
        shutil.rmtree(tmp)


def test_same_trace_required_three_beats_fails():
    service, tmp = fresh_service()
    try:
        event, _ = third_person_trace_case(stop="4", hold="3", a3_last="4")
        service.intake_event(event)
        verdict = service.judge("E_THIRD", "initial")
        check(verdict["verdict"] == "determined_not_caught",
              "原车迹只维持 2 拍，要求 3 拍时确定未及")
        check(verdict["trace_reviews"][0]["maximum_observed_hold"] == "2",
              "相反见证列出 2 拍上限")
    finally:
        shutil.rmtree(tmp)


def test_third_rider_switch_at_fractional_time():
    service, tmp = fresh_service()
    try:
        event = base_event("E_SWAP", "M_SWAP", stop="4", hold="2")
        event["riders"] = {
            "A": ["A1", "A2", "A3", "A4"],
            "B": ["B1", "B2", "B3", "B4"],
        }
        values = {
            "A1": [Fraction(15), Fraction(16), Fraction(17), Fraction(18), Fraction(19)],
            "A2": [Fraction(14), Fraction(15), Fraction(16), Fraction(17), Fraction(18)],
            # A4 在 5/2 前是第三人，之后 A3 成为第三人；交点恰为分数时刻 5/2。
            "A3": [Fraction(i, 8) for i in range(5)],
            "A4": [Fraction(1, 5)] * 5,
            "B1": [Fraction(15), Fraction(16), Fraction(17), Fraction(18), Fraction(19)],
            "B2": [Fraction(14), Fraction(15), Fraction(16), Fraction(17), Fraction(18)],
            "B3": [Fraction(1, 4)] * 5,
            "B4": [Fraction(-1)] * 5,
        }
        riders = event["riders"]["A"] + event["riders"]["B"]
        event["grid"].append(full_grid("G_SWAP", riders, event["times"], values))
        event["evidence"] = [
            {"evidence_id": "L_A3_0", "type": "line", "rider": "A3",
             "recorded_at": "0", "submitted": "initial", "distance": "0"},
            {"evidence_id": "L_A4_0", "type": "line", "rider": "A4",
             "recorded_at": "0", "submitted": "initial", "distance": "1/5"},
            {"evidence_id": "L_B3_0", "type": "line", "rider": "B3",
             "recorded_at": "0", "submitted": "initial", "distance": "1/4"},
        ]
        service.intake_event(event)
        verdict = service.judge("E_SWAP", "initial")
        check(verdict["verdict"] == "determined_caught", "换位后的完整车迹仍追足约定时长")
        segments = verdict["trace_reviews"][0]["team_position_segments"]["A"]
        switch_at = Fraction("8/5")
        check(all(s["third_rider"] == "A4"
                  for s in segments
                  if Fraction(s["end"]) <= switch_at),
              "5/2 前队位第三人是 A4")
        check(all(s["third_rider"] == "A3"
                  for s in segments
                  if Fraction(s["start"]) >= switch_at),
              "5/2 后第三人换位为 A3")
        check(verdict["trace_reviews"][0]["qualified_intervals"] == [
            {"start": "2", "end": "4", "length": "2"}],
              "追及区间按换位后的队位计算，位差为 0 的达到也计入保持")
    finally:
        shutil.rmtree(tmp)


def test_common_uncertain_traces_all_caught():
    service, tmp = fresh_service()
    try:
        event = base_event("E_UNCERTAIN", "M_UNCERTAIN", stop="4", hold="2")
        common = {
            "A1": [Fraction(12 + i) for i in range(5)],
            "A2": [Fraction(11 + i) for i in range(5)],
            "B1": [Fraction(13 + i) for i in range(5)],
            "B2": [Fraction(12 + i) for i in range(5)],
            "B3": [Fraction(2)] * 5,
        }
        trace_slow = {**common, "A3": [Fraction(1), Fraction(3, 2), Fraction(2),
                                       Fraction(5, 2), Fraction(3)]}
        trace_fast = {**common, "A3": [Fraction(0), Fraction(1), Fraction(2),
                                       Fraction(5, 2), Fraction(3)]}
        riders = event["riders"]["A"] + event["riders"]["B"]
        event["grid"] = [
            full_grid("G_SLOW", riders, event["times"], trace_slow),
            full_grid("G_FAST", riders, event["times"], trace_fast),
        ]
        event["evidence"] = [
            {"evidence_id": "L_B3", "type": "line", "rider": "B3",
             "recorded_at": "2", "submitted": "initial", "distance": "2"},
            {"evidence_id": "L_A3_2", "type": "line", "rider": "A3",
             "recorded_at": "2", "submitted": "initial", "distance": "2"},
            {"evidence_id": "L_A3_4", "type": "line", "rider": "A3",
             "recorded_at": "4", "submitted": "initial", "distance": "3"},
        ]
        service.intake_event(event)
        verdict = service.judge("E_UNCERTAIN", "initial")
        check(verdict["verdict"] == "determined_caught",
              "共同不确定车迹训练：两份允许车迹都在 [2,4] 追足才鸣铃")
        check(verdict["trace_count"] == 2 and verdict["bell_at"] == "4",
              "实时执裁按全部可能完整车迹的最晚达标时刻鸣铃")
    finally:
        shutil.rmtree(tmp)


def test_contradiction_and_gap_only_are_separately_queryable():
    service, tmp = fresh_service()
    try:
        event = base_event("E_GAP", "M_GAP", stop="2", hold="1")
        event["evidence"] = [
            # 同一过线点给两个互斥里程，制造矛盾；无完整车迹。
            {"evidence_id": "L1", "type": "line", "rider": "A3",
             "recorded_at": "1", "submitted": "initial", "distance": "2"},
            {"evidence_id": "L2", "type": "line", "rider": "A3",
             "recorded_at": "1", "submitted": "initial", "distance": "3"},
            # 位差证据本身仍可在位置参数化时被确定。
            {"evidence_id": "D1", "type": "gap", "rider_a": "A3", "rider_b": "B3",
             "recorded_at": "1", "submitted": "initial", "difference": "1/2"},
        ]
        service.intake_event(event)
        verdict = service.judge("E_GAP", "initial")
        check(verdict["verdict"] == "contradiction", "互斥过线单单独报矛盾")
        check(any(gap["evidence_id"] == "D1" and gap["difference"] == "1/2"
                  for gap in verdict["determined_gaps"]),
              "位置不定但位差确定可分别查询")

        clean = base_event("E_GAP_CLEAN", "M_GAP_CLEAN", stop="2", hold="1")
        clean["evidence"] = [
            {"evidence_id": "D1", "type": "gap", "rider_a": "A3", "rider_b": "B3",
             "recorded_at": "1", "submitted": "initial", "difference": "1/2"},
        ]
        service.intake_event(clean)
        pending = service.judge("E_GAP_CLEAN", "initial")
        check(pending["verdict"] == "pending", "只有位差、无有限格点时绝对位置不定，整体待定")
        check(pending["determined_gaps"][0]["difference"] == "1/2",
              "待定报告仍保留确定位差证据")
    finally:
        shutil.rmtree(tmp)


def test_short_team_is_recorded_separately():
    service, tmp = fresh_service()
    try:
        event = base_event("E_SHORT", "M_SHORT", stop="2", hold="1")
        event["withdrawals"] = [
            {"rider": "A3", "after": "1"},
            {"rider": "B3", "after": "1"},
        ]
        values = {
            "A1": [Fraction(12), Fraction(13), Fraction(14)],
            "A2": [Fraction(11), Fraction(12), Fraction(13)],
            "A3": [Fraction(0), Fraction(1), Fraction(2)],
            "B1": [Fraction(13), Fraction(14), Fraction(15)],
            "B2": [Fraction(12), Fraction(13), Fraction(14)],
            "B3": [Fraction(0), Fraction(0), Fraction(0)],
        }
        riders = event["riders"]["A"] + event["riders"]["B"]
        event["times"] = ["0", "1", "2"]
        event["grid"].append(full_grid("G_SHORT", riders, event["times"], values))
        service.intake_event(event)
        verdict = service.judge("E_SHORT", "initial")
        check(verdict["verdict"] == "short_team", "不足三人时不硬算追及，另记短队")
        check(verdict["detail"]["status"] == "short_team", "短队细节指出退出状态")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_caught_exactly_two_beats,
        test_late_correction_cannot_be_extrapolated,
        test_actual_ride_to_five_can_count,
        test_same_trace_required_three_beats_fails,
        test_third_rider_switch_at_fractional_time,
        test_common_uncertain_traces_all_caught,
        test_contradiction_and_gap_only_are_separately_queryable,
        test_short_team_is_recorded_separately,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n追逐赛 {len(PASS)} 项断言全部通过。")


if __name__ == "__main__":
    run_all()
