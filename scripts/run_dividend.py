"""红利宣告与兑领端到端演示（题内合成口径，绝不取整）。

运行：python3.11 scripts/run_dividend.py

覆盖：
  A. 在已成功的集团合并底稿上算可分余额（内部毛利留库、对外净利；再扣亏损/已宣告并取零）。
  B. 一笔原亏损被正式冲回一部分；旧版冲回不能重复计。
  C. 稳定选择筛选与保底承诺（只在稳定区间上取；承诺须 ≤ 每个稳定选择下所得）。
  D. 两个优先类互相影响、可稳定宣告的例子；D2 多稳定方案下区间只判保底。
  E. 无稳定选择的例子：交出每个被否选择的一次获益偏离。
  F. 正式宣告后拆转红利权（甲卖股给乙、另转 1 元息权给丙：乙不能领，甲/丙各 3/1）。
  G. 两张相冲的转让申请：先到锁定，后到因余额不足被拒；已实付份额不得二次转走。
  H. 余额相同的两次到柜核对：登记柜面在回执入册前后各办一次；巡回台必须先交真票。
  I. 宣告锁定余额；后加单体账不改原宣告；只有该次宣告可兑领。
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol import DividendError, CustodyError, fresh  # noqa: E402


def F(x):
    return Fraction(x)


def pct(frac):
    if isinstance(frac, Fraction):
        return str(frac) if frac.denominator != 1 else str(frac.numerator)
    return str(frac)


def banner(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def seed_p_and_s(ws):
    """P 以成本 8 造 4 件内售 S，售价 16；S 对外卖 2 件收入 10；另一笔对外净利 6。"""
    ws.main_post(
        "P-internal-i1",
        [["receivable", 16, 0], ["revenue", 0, 16], ["cost", 8, 0], ["inventory", 0, 8]],
        {"type": "internal_sale", "side": "seller", "invoice_id": "i1",
         "q": 4, "T": 16, "C": 8},
    )
    # S 对外售出 2 件：售价 10，成本 8（2 件 × 4，按 P 的成本结转）
    ws.main_post("S-external-2", [["cash", 10, 0], ["revenue", 0, 10],
                                  ["cost", 8, 0], ["inventory", 0, 8]])
    # 另一笔对外净利 6
    ws.main_post("S-other-profit", [["cash", 6, 0], ["revenue", 0, 6]])
    ws.declare_stock("s1", {"i1": 2})  # 剩余两件


def seed_group_worksheet(ws, db_path):
    """用集团合并服务在独立 P/S 账上发布一张底稿，作为可分余额的权威来源。

    返回 (statement, verifier 报告)。P/S 均为全资实体，期末存货 i1 声明 r=2。
    """
    from group_consol import EntityLedger, ConsolidationService
    from group_consol.verifier import verify_worksheet

    if os.path.exists(db_path):
        os.remove(db_path)
    led = EntityLedger(db_path)
    svc = ConsolidationService(led)
    SALE = {"type": "internal_sale", "invoice_id": "i1", "seller": "P", "buyer": "S",
            "sku": "X", "q": 4, "T": 16, "C": 8}
    led.post("Popen", "P", [["inventory", 8, 0], ["capital", 0, 8]])
    led.post("sale", "P",
             [["receivable", 16, 0], ["revenue", 0, 16], ["cost", 8, 0], ["inventory", 0, 8]],
             {**SALE, "side": "seller"})
    led.post("buy", "S", [["inventory", 16, 0], ["payable", 0, 16]],
             {**SALE, "side": "buyer"})
    led.post("Sext", "S", [["cash", 10, 0], ["revenue", 0, 10],
                           ["cost", 8, 0], ["inventory", 0, 8]])
    led.post("Sother", "S", [["cash", 6, 0], ["revenue", 0, 6]])
    led.register_stock("gv", {"i1": 2})
    stmt = svc.publish("group-1", {"P": led.head("P"), "S": led.head("S")}, "gv")
    rep = verify_worksheet(led.conn, "group-1")
    led.close()
    return stmt, rep


def scenario_a_balance(ws):
    banner("A. 可分余额：在已成功的集团底稿上算 + 剩余货物")
    # 1) 用集团合并服务发布一张真实底稿并独立核验
    gdb = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "state", "group-for-dividend.db")
    stmt, rep = seed_group_worksheet(ws, gdb)
    print("集团底稿 final:", {k: pct(v) for k, v in stmt["final"].items()})
    print("独立核验通过，发票数", rep["invoices_checked"], "抵销分录", rep["elimination_entries"])
    # 底稿已把内部销货 revenue/cost 抵销、按 (T−C)×r/q 调掉未实现毛利
    urp = next(e for e in stmt["eliminations"] if e["kind"] == "unrealized_profit")
    signed = {ln["account"]: (ln["debit"], ln["credit"]) for ln in urp["lines"]}
    print("底稿未实现毛利抵销（尚留库）:", signed, "=> 4 件留 2 件的毛利被剔出当期")

    # 2) 在底稿上算可分余额：-(revenue) - cost - 有效亏损 - 已宣告
    b = ws.balance_from_worksheet(stmt)
    print("底稿收入(revenue 借减贷):", pct(b["loss_from_revenue"]),
          " 成本(cost 借减贷):", pct(b["expense_from_cost"]))
    print("当期经营成果 = -revenue - cost =", pct(b["traceable_income"]))
    print("另册尚有效亏损 :", pct(b["memo_loss"]),
          " 此前已宣告未撤销 :", pct(b["net_declared"]))
    print("可分 = max(0, 成果 - 亏损 - 已宣告) =>", pct(b["distributable"]))
    assert b["distributable"] == 12, b["distributable"]

    # 3) 并行账（同一套凭证）口径对照：应得同一结果
    b2 = ws.balance("s1")
    print("并行账对照: 内部毛利", pct(b2["internal_gross_profit"]),
          "已实现", pct(b2["internal_realized_profit"]),
          "留库", pct(b2["inventory_kept_profit"]["total"]))
    assert b2["distributable"] == 12
    assert b2["inventory_kept_profit"]["total"] == 4
    for it in b2["inventory_kept_profit"]["items"]:
        print("  发票", it["invoice_id"], " q/T/C/r =", it["q"], it["T"], it["C"], it["r"],
              " -> 留库毛利", pct(it["unrealized_profit"]))

    # 4) 公式完整演示：扣除另册有效亏损、此前已宣告未撤销，并最低取零
    import tempfile
    side = fresh(tempfile.mktemp(suffix="-formula.db"))
    try:
        for jid, lines in [
            ("ext", [["cash", 16, 0], ["revenue", 0, 16]]),
            ("cost", [["cost", 4, 0], ["cash", 0, 4]]),
            ("other", [["cash", 6, 0], ["revenue", 0, 6]]),
        ]:
            side.main_post(jid, lines)
        print("\n公式演示（简化账：对外收入 22，对外成本 4 => 成果 18）")
        base = side.balance()["distributable"]
        print("  无亏损无宣告 =>", pct(base))
        side.memo_loss("L", 5)
        side.memo_recover("R", "L", 2)
        print("  扣另册有效亏损 3 =>", pct(side.balance()["distributable"]))
        side.main.declare("D", F(8), "prior")
        print("  再扣已宣告未撤销 8 =>", pct(side.balance()["distributable"]))
        side.main.declare("D2", F(100), "prior2")
        print("  已宣告超过成果 => 最低取零 =>", pct(side.balance()["distributable"]))
        assert side.balance()["distributable"] == 0
    finally:
        side.close()
    # 把底稿口径用于后续分派
    return b


def scenario_b_loss_recovery():
    banner("B. 原亏损可被正式冲回一部分；旧版冲回不得冲两遍")
    import tempfile
    tmp = tempfile.mktemp(suffix="-memo.db")
    bus = fresh(tmp)
    try:
        bus.memo_loss("L1", 5)
        bus.memo_recover("R1", "L1", 2)
        print("原亏损 5，正式冲回 2 => 另册尚有效亏损", pct(bus.balance()["memo_loss"]))
        assert bus.balance()["memo_loss"] == 3
        # 新版冲回替换旧版：3 -> 4（只算一次）
        bus.memo_recover("R1b", "L1", 4, supersedes="R1")
        print("用新版冲回 4 替换旧版 2 =>", pct(bus.balance()["memo_loss"]))
        assert bus.balance()["memo_loss"] == 1
        # 再把旧版冲回当新冲回再用 -> 拒绝
        try:
            bus.memo_recover("R1", "L1", 2)
            raise AssertionError("旧版冲回不应能再次计入")
        except Exception as exc:  # BookError
            print("旧版冲回再次入账被拒:", getattr(exc, "code", type(exc).__name__))
        # 超过原额 -> 拒绝
        try:
            bus.memo_recover("R1c", "L1", 99)
            raise AssertionError("冲回不应超过原亏损")
        except Exception as exc:
            print("超额冲回被拒:", getattr(exc, "code", type(exc).__name__))
        # 把有效冲回调回 2 验证“可只冲回一部分”后余额
        bus.memo_recover("R1d", "L1", 2, supersedes="R1b")
        print("把有效冲回调回 2 => 另册尚有效亏损", pct(bus.balance()["memo_loss"]))
        assert bus.balance()["memo_loss"] == 3
    finally:
        bus.close()
        if os.path.exists(tmp):
            os.remove(tmp)


def scenario_c_single_pref(ws, b):
    banner("C. 单优先类：先取 5 或整类转两份；稳定选择与保底承诺")
    classes = {"common": {"kind": "common"},
               "B": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0}}
    holdings = {"甲": {"common": 1}, "乙": {"B": 1}}
    ev = ws.evaluate(classes, holdings, b["distributable"])
    for o in ev["all_options"]:
        tag = "稳定" if o["stable"] else "不稳定"
        print(f"  乙选{o['choice']['B']:8s} 甲={pct(o['amounts']['甲'])} "
              f"乙={pct(o['amounts']['乙'])} -> {tag}")
        if not o["stable"]:
            for d in o["unstable_by"]:
                print(f"     单独反转 {d['class']} {d['from']}->{d['to']} 可多拿 "
                      f"{pct(d['gain'])}")
    print("每人金额区间（只在稳定选择上取）:",
          {k: (pct(v["min"]), pct(v["max"])) for k, v in ev["ranges"].items()})
    stable = ws.choose_stable(ev, 0)
    print("选定稳定方案:", stable["choice"], "=> 甲", pct(stable["amounts"]["甲"]),
          "乙", pct(stable["amounts"]["乙"]))
    assert stable["amounts"] == {"甲": F(4), "乙": F(8)}
    # 保底承诺：乙可在每个稳定选择下都拿 >= 8
    print("承诺乙 8:", ws.promise(ev, "乙", F(8)))

    # 只在账上加费用 3 -> 甲保证仅 3
    ws.main_post("expense-3", [["cost", 3, 0], ["cash", 0, 3]])
    b1 = ws.balance("s1")
    assert b1["distributable"] == 9, b1["distributable"]
    ev1 = ws.evaluate(classes, holdings, b1["distributable"])
    print("\n加费用 3 后：可分", pct(b1["distributable"]), "稳定方案",
          ws.choose_stable(ev1, 0)["amounts"])
    assert ws.choose_stable(ev1, 0)["amounts"]["甲"] == 3
    print("承诺甲 3:", ws.promise(ev1, "甲", F(3)))
    try:
        ws.promise(ev1, "甲", F(4))
        raise AssertionError("甲拿不到 4")
    except DividendError as exc:
        print("承诺甲 4 被拒（付不足）:", exc.code, exc.details["guaranteed_min"])

    # 复原原账，仅把乙先取额改 9 -> 乙不转，甲也仅 3
    ws.main_post("revert-expense-3", [["cost", 0, 3], ["cash", 3, 0]])
    assert ws.balance("s1")["distributable"] == 12
    classes9 = {"common": {"kind": "common"},
                "B": {"kind": "preference", "shares": 1, "fixed": 9, "ratio": 2, "order": 0}}
    ev9 = ws.evaluate(classes9, holdings, F(12))
    print("\n仅把先取额定改 9（不转）后稳定方案:",
          ws.choose_stable(ev9, 0)["amounts"])
    assert ws.choose_stable(ev9, 0)["amounts"] == {"甲": F(3), "乙": F(9)}
    return ev


def scenario_d_dual_pref(ws):
    banner("D. 两个优先类互相影响：可稳定宣告的例子")
    classes = {"common": {"kind": "common"},
               "A": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0},
               "B": {"kind": "preference", "shares": 1, "fixed": 7, "ratio": 2, "order": 1}}
    holdings = {"甲": {"common": 1}, "乙": {"A": 1}, "丙": {"B": 1}}
    ev = ws.evaluate(classes, holdings, F(12))
    for o in ev["all_options"]:
        print("  A->", o["choice"]["A"], " B->", o["choice"]["B"],
              {h: pct(v) for h, v in o["amounts"].items()},
              "稳定" if o["stable"] else "不稳定")
    stable = [o["choice"] for o in ev["stable_options"]]
    print("稳定选择:", stable)
    assert stable == [{"A": "cash", "B": "cash"}]
    assert ev["stable_options"][0]["amounts"] == {"甲": F(0), "乙": F(5), "丙": F(7)}
    # 另一个可稳定宣告的互相影响例子（存在多个稳定选择）
    classes2 = {"common": {"kind": "common"},
                "A": {"kind": "preference", "shares": 1, "fixed": 6, "ratio": 2, "order": 0},
                "B": {"kind": "preference", "shares": 1, "fixed": 4, "ratio": 2, "order": 1}}
    ev2 = ws.evaluate(classes2, holdings, F(12))
    print("\n第二例（多稳定选择）稳定:", [o["choice"] for o in ev2["stable_options"]])
    for o in ev2["stable_options"]:
        print("  ", o["choice"], {h: pct(v) for h, v in o["amounts"].items()})
    # 两类份额按序取定额、后类承受短缺的例子
    classes3 = {"common": {"kind": "common"},
                "A": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0},
                "B": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 1}}
    ev3 = ws.evaluate(classes3, holdings, F(7))
    print("\n短缺例（可分 7，两类各要 5）稳定:",
          [o["choice"] for o in ev3["stable_options"]])
    print("  金额:", {h: pct(v) for h, v in ev3["stable_options"][0]["amounts"].items()})
    return ev


def scenario_d2_range(ws):
    banner("D2. 两个稳定方案金额不同：区间只用于判断能否保底，不决定应得")
    classes = {"common": {"kind": "common"},
               "P0": {"kind": "preference", "shares": 3, "fixed": 5, "ratio": 3, "order": 0},
               "P1": {"kind": "preference", "shares": 1, "fixed": 2, "ratio": Fraction(3, 2),
                      "order": 1}}
    holdings = {"J0": {"common": 1}, "P0a": {"P0": 2}, "P0b": {"P0": 1},
                "P1": {"P1": 1}}
    ev = ws.evaluate(classes, holdings, F(12))
    print("两个稳定方案:")
    for o in ev["stable_options"]:
        print("  ", o["choice"], {h: pct(v) for h, v in o["amounts"].items()})
    print("每人稳定区间:", {h: (pct(v["min"]), pct(v["max"]))
                            for h, v in ev["ranges"].items()})
    j0_min = ev["ranges"]["J0"]["min"]
    p1_min = ev["ranges"]["P1"]["min"]
    print("J0 区间 [%s,%s]、P1 区间 [%s,%s]：可保底 J0=%s、P1=%s；"
          "但正式宣告只选一个方案。" % (
              pct(ev["ranges"]["J0"]["min"]), pct(ev["ranges"]["J0"]["max"]),
              pct(ev["ranges"]["P1"]["min"]), pct(ev["ranges"]["P1"]["max"]),
              pct(j0_min), pct(p1_min)))
    assert ev["ranges"]["J0"] == {"min": F(0), "max": F(1)}
    assert ev["ranges"]["P1"] == {"min": F(2), "max": F(3)}
    # 承诺超过某稳定方案下的所得 -> 付不足
    try:
        ws.promise(ev, "P1", F(3))
        raise AssertionError("P1 在其一稳定方案下只拿 2，不应能承诺 3")
    except DividendError as exc:
        print("承诺 P1=3 被拒:", exc.code, "保底", exc.details["guaranteed_min"])
    return ev


def scenario_e_no_equilibrium(ws):
    banner("E. 无稳定选择：交出每个被否选择的一次获益偏离")
    classes = {"common": {"kind": "common"},
               "A": {"kind": "preference", "shares": 1, "fixed": 1, "ratio": 1, "order": 0},
               "B": {"kind": "preference", "shares": 1, "fixed": 6, "ratio": 2, "order": 1},
               "C": {"kind": "preference", "shares": 1, "fixed": 1, "ratio": 2, "order": 2}}
    holdings = {"甲": {"common": 1}, "A人": {"A": 1}, "B人": {"B": 1}, "C人": {"C": 1}}
    ev = ws.evaluate(classes, holdings, F(6))
    print("有无稳定选择:", not ev["all_unstable"])
    assert ev["all_unstable"]
    for d in ev["rejected_deviations"]:
        dev = d["deviation"]
        print(f"  方案 {d['choice']} 被否：单独把 {dev['class']} 由 {dev['from']} 改为 "
              f"{dev['to']} 可多拿 {pct(dev['gain'])}")
    # 无稳定选择不能宣告
    try:
        ws.choose_stable(ev, 0)
        raise AssertionError("无稳定选择却可选")
    except DividendError as exc:
        print("无稳定选择，拒绝宣告:", exc.code)


def scenario_f_claims():
    banner("F. 正式宣告 4 给甲；甲卖股给乙、另转 1 元息权给丙：乙不能领，甲/丙最终 3/1")
    import tempfile
    ws = fresh(tempfile.mktemp(suffix="-fc.db"))
    try:
        # 造一个可分 4 的干净批次，正式宣告给甲一人（宣告金额 = 该稳定方案总额）
        ws.main_post("ext", [["cash", 4, 0], ["revenue", 0, 4]])
        classes = {"common": {"kind": "common"}}
        holdings = {"甲": {"common": 1}}
        ev = ws.evaluate(classes, holdings, F(4))
        rec = ws.declare("lot-F4", F(4),
                         {"evaluation": ev, "choice": ws.choose_stable(ev, 0)["choice"]})
        jia = ws.claim_by_holder("lot-F4", "甲")["claim_id"]
        print("正式宣告 lot-F4:", pct(rec["amount"]), "；甲的红利权:", jia)
        # 宣告后股票交易恢复：甲把股票卖给乙（持股人乙），另把 1 元息权转给丙
        ws.transfer("TR-甲-丙", jia, "甲", "丙", F(1), at_step=2, at="facade")
        st = ws.claims.claim_state(jia)
        receipt = st["applications"][0]["receipt"]
        bing = f"{jia}+TR-甲-丙"
        print("甲把 1 元息权转给丙：锁定，甲余", pct(st["remaining"]),
              "；丙收据", receipt, "尚未入册")
        # 乙买了股票也没用：乙既无 claim 也无票；也不能领甲/丙的
        holders = {c["owner"] for c in ws.claims.list_claims()}
        print("持股人乙未获得上次红利（红利权持有人集合):", sorted(holders))
        assert "乙" not in holders
        # 丙的回执未入册 -> 柜面拒付，指明去哪里核、等到哪步
        try:
            ws.pay_claim(bing, "pay-丙-早", at_step=3)
        except CustodyError as exc:
            print("丙的回执未入册，柜面拒绝签付:", exc.code,
                  "->", exc.details.get("go_to"), "/", exc.details.get("wait_until"))
        # 甲领自己的 3
        print("甲领款:", ws.pay_claim(jia, "pay-甲", at_step=4)["amount"])
        # 丙把回执入册后领 1
        ws.register_receipt(receipt, at_step=5)
        print("丙回执入册后领款:", ws.pay_claim(bing, "pay-丙", at_step=6)["amount"])
        print("实付：甲", pct(ws.claims.paid_total("甲")),
              " 丙", pct(ws.claims.paid_total("丙")),
              " 乙", pct(ws.claims.paid_total("乙")))
        assert ws.claims.paid_total("甲") == 3
        assert ws.claims.paid_total("丙") == 1
        assert ws.claims.paid_total("乙") == 0
    finally:
        ws.close()


def scenario_g_contested(ws):
    banner("G. 两张相冲的转让申请：先到锁定，后到不足被拒；已实付不得二次转走")
    rec = ws.claims.open_claims({"K": F(4)}, {"K": "facade"}, step=10)
    k = rec[0]["claim_id"]
    a1 = ws.transfer("APP-1", k, "K", "X", F(3), at_step=11, at="facade")
    print("申请 1（K->X 3）:", a1["status"], "K 余", pct(a1["source_remaining"]))
    try:
        ws.transfer("APP-2", k, "K", "Y", F(3), at_step=12, at="facade")
        raise AssertionError("第二笔相冲应被拒")
    except CustodyError as exc:
        print("申请 2（K->Y 3）被拒:", exc.code, exc.details)
    print("权益现状:", ws.claims.claim_state(k))
    # 已实付的份额不能再被转走
    ws.register_receipt(a1["receipt_id"], at_step=13)
    ws.pay_claim(k, "pay-K", at_step=14)
    try:
        ws.transfer("APP-3", k, "K", "Z", F(1), at_step=15, at="facade")
        raise AssertionError("已付份额不应能再转")
    except CustodyError as exc:
        print("已实付后再次转出被拒:", exc.code)


def scenario_h_same_balance(ws):
    banner("H. 余额相同的两次到柜核对")
    # 情形 1：登记处柜面，分别在转让回执入册前/后各到一次
    rec = ws.claims.open_claims({"M": F(2)}, {"M": "facade"}, step=20)
    m = rec[0]["claim_id"]
    tr = ws.transfer("TR-M-N", m, "M", "N", F(2), at_step=21, at="facade")
    n = tr["new_claim_id"]
    # 同一笔 2 元：回执未入册时到柜 -> 拒绝，并指明去哪里核、等到哪步
    try:
        ws.pay_claim(n, "pay-N-before", at_step=22)
    except CustodyError as exc:
        print("[柜面/回执前] 拒付:", exc.code, "->", exc.details.get("go_to"))
    ws.register_receipt(tr["receipt_id"], at_step=23)
    out = ws.pay_claim(n, "pay-N-after", at_step=24)
    print("[柜面/回执后] 签付:", pct(out["amount"]), " 余额相同、仅入册前后之别")

    # 情形 2：巡回台，必须先交真实未拆票，再换成只带拆前旧票号码的子票领款
    rec = ws.claims.open_claims({"P": F(2)}, {"P": "circuit"}, step=30)
    p = rec[0]["claim_id"]
    tk = ws.issue_ticket(p, at_step=31)
    print("为 P 签具名息票:", tk["ticket_id"], "票面", pct(tk["amount"]))
    # 未交票 -> 巡回台拒付
    try:
        ws.pay_ticket(tk["ticket_id"], "pay-P-undelivered", at_step=32)
    except CustodyError as exc:
        print("[巡回/未交票] 拒付:", exc.code, "-> 先交真实未拆票")
    # 拆票：必须收旧票再交子票，子票只带拆前旧票号码
    sp = ws.split_ticket(tk["ticket_id"], "P", [("Q", F(2))], at_step=33)
    child = sp["children"][0]
    print("收旧票", sp["parent"], "交子票", child["ticket_id"],
          "（只带拆前旧票号码", child["old_no"], "）")
    # 旧票不能再交
    try:
        ws.deliver_ticket(tk["ticket_id"], "P", at_step=34)
        raise AssertionError("旧票已拆不应能再交")
    except CustodyError as exc:
        print("[巡回/旧票已拆] 拒付:", exc.code)
    # 客户要拿到真实凭据：先交子票，再领款
    print("巡回台本地核验子票:", ws.check_ticket(child["ticket_id"])["genuine"],
          "（不联查登记处）")
    ws.deliver_ticket(child["ticket_id"], "Q", at_step=35)
    print("[巡回/已交真票] 签付:", pct(ws.pay_ticket(child["ticket_id"], "pay-Q",
                                                     at_step=36)["amount"]))
    # 领过的真票不能再退回持有人
    try:
        ws.deliver_ticket(child["ticket_id"], "Q", at_step=37)
        raise AssertionError("已领票不应退回")
    except CustodyError as exc:
        print("[巡回/已领票] 不得退还:", exc.code)


def scenario_i_declare_and_lock(ws):
    banner("I. 正式宣告锁定余额；后加单体账不改原宣告；只有该次宣告可兑领")
    # 在干净批次上入账
    pre = ws.balance("s1")["distributable"]
    print("宣告前可分:", pct(pre))
    classes = {"common": {"kind": "common"},
               "B": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0}}
    holdings = {"甲": {"common": 1}, "乙": {"B": 1}}
    ev = ws.evaluate(classes, holdings, pre)
    stable = ws.choose_stable(ev, 0)
    print("经核稳定方案:", stable["choice"], "=> 甲", pct(stable["amounts"]["甲"]),
          "乙", pct(stable["amounts"]["乙"]))
    rec = ws.declare("lot-A", pre, {"evaluation": ev, "choice": stable["choice"]},
                     stock_version="s1")
    print("正式宣告 lot-A:", pct(rec["amount"]), "分派", rec["allocations"])
    # 后加单体账（费用 3）：旧宣告不变，只改变后续批次的余额
    ws.main_post("post-decl-expense", [["cost", 3, 0], ["cash", 0, 3]])
    after = ws.balance("s1")
    print("宣告后新入费用 3：当期成果降为", pct(after["traceable_income"]),
          "，扣除旧宣告", pct(after["net_declared"]),
          "后最低取零 => 新批次可分", pct(after["distributable"]),
          "（旧宣告仍为", pct(ws.declaration("lot-A")["amount"]), "）")
    assert ws.declaration("lot-A")["amount"] == "12"
    # 只有 lot-A 的红利权可兑领；用 lot-A 的 claim 领款
    jia = ws.claim_by_holder("lot-A", "甲")
    print("lot-A 甲可兑领:", pct(ws.claim_of(jia["claim_id"])["amount"]))
    # 试图超可分额再宣告 -> 拒绝（旧宣告已锁走 12）
    try:
        ws.declare("lot-B", F(1), {"evaluation": ws.evaluate(classes, holdings, F(0)),
                                   "choice": stable["choice"]}, stock_version="s1")
        raise AssertionError("余额已锁，不应再宣告")
    except (DividendError, AssertionError) as exc:
        print("余额不足再宣告被拒:", getattr(exc, "code", "assert"))
    return rec


def main():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "state", "dividend.db")
    ws = fresh(path)
    try:
        seed_p_and_s(ws)
        b = scenario_a_balance(ws)
        scenario_b_loss_recovery()
        scenario_c_single_pref(ws, b)
        scenario_d_dual_pref(ws)
        scenario_d2_range(ws)
        scenario_e_no_equilibrium(ws)
        scenario_f_claims()
        scenario_g_contested(ws)
        scenario_h_same_balance(ws)
        scenario_i_declare_and_lock(ws)
        print("\n全部场景通过。")
    finally:
        ws.close()


if __name__ == "__main__":
    main()
