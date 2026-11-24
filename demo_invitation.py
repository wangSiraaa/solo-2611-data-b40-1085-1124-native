"""请柬/亲称业务端到端演示（全部人物为虚构）。

运行：python3.11 demo_invitation.py
分五幕严格对应题干验收点：
  一、普通柬：两姊妹、姑母/姨母字模、实名证言、撤字模、无安全词联名柬
  二、两份相反隐私谱：成品不可反推、去中性字模后只能封缄
  三、四份家谱：顺序/字盘单项可过、联合放行泄密、保护代价落在封数上
  四、同日两封信：local 最多交普通信 vs sealed 共交两封、钥位预占
  五、基础规则：家系无环、谱外长辈、归并后副本号决定词义、重开续办
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from library import IdempotencyConflict, Rejection
from library.scenarios import load_material
from library.system import LibrarySystem

F_SIB = [["up", "father"], ["sib"]]
M_SIB = [["up", "mother"], ["sib"]]
GU = {"term": "姑母", "patterns": [F_SIB]}
YI = {"term": "姨母", "patterns": [M_SIB]}
ZHANG = {"term": "长辈", "patterns": [F_SIB, M_SIB]}


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def tree(tree_id: str, kind: str, side=None, attrs=None):
    base = [
        {"id": "青", "father": "父", "mother": "母", "seniority": 1},
        {"id": "兰", "father": "父", "mother": "母", "seniority": 2},
    ]
    if kind == "pat":
        base += [
            {"id": "父", "father": "祖父", "mother": "祖母", "seniority": 2},
            {"id": "母", "father": None, "mother": None, "seniority": None},
            {"id": "甲", "father": "祖父", "mother": "祖母", "seniority": 1},
            # 乙同为祖父母的子女，与父同辈，父系下走父的同胞
            {"id": "乙", "father": "祖父", "mother": "祖母", "seniority": 3},
            {"id": "祖父", "father": None, "mother": None, "seniority": None},
            {"id": "祖母", "father": None, "mother": None, "seniority": None},
        ]
    else:
        base += [
            {"id": "母", "father": "外祖父", "mother": "外祖母", "seniority": 2},
            {"id": "父", "father": None, "mother": None, "seniority": None},
            {"id": "甲", "father": "外祖父", "mother": "外祖母", "seniority": 1},
            {"id": "乙", "father": "外祖父", "mother": "外祖母", "seniority": 3},
            {"id": "外祖父", "father": None, "mother": None, "seniority": None},
            {"id": "外祖母", "father": None, "mother": None, "seniority": None},
        ]
    return {"tree_id": tree_id, "persons": base, "side": side, "attrs": attrs or {}}


def setup(system, trees, entries, molds, cards, regs=None, trays=None):
    system.catalog.submit_revision(
        "k1", ["WX"], [],
        [{"id": "V1", "book": "WX", "branch": "东村", "format": "平装"}],
        op_id="cat")
    system.catalog.activate_revision("k1")
    system.kinship.register_copy_terms("V1", entries, op_id="terms")
    for t in trees:
        system.kinship.register_tree(
            t["tree_id"], t["persons"], op_id=f"tree-{t['tree_id']}",
            side=t.get("side"), attrs=t.get("attrs"))
    for reg in regs or []:
        system.kinship.register_registration(
            reg["reg_id"], reg["speaker"], reg["referent"], "东村", "V1",
            reg["paths"], op_id=f"reg-{reg['reg_id']}")
    system.invitation.register_position("K1", True)
    for tray in trays or []:
        system.invitation.add_tray(tray)
    for term in molds:
        system.invitation.set_mold(term, True, op_id=f"mold-{term}")
    for c in cards:
        system.invitation.register_card(
            c["card_id"], c["recipient"], c["signers"], "V1",
            c["deadline_shift"], op_id=f"card-{c['card_id']}")


def act_one() -> None:
    banner("第一幕 普通柬（无保密约定）：两姊妹给甲落款，甲比双亲年长")
    s = LibrarySystem(":memory:")
    setup(
        s, [tree("T_pat", "pat"), tree("T_mat", "mat")], [GU, YI],
        ["姑母", "姨母"],
        [{"card_id": "Q1", "recipient": "甲", "signers": ["青", "兰"],
          "deadline_shift": "S1"}],
        regs=[{"reg_id": "R1", "speaker": "青", "referent": "甲",
               "paths": [F_SIB, M_SIB]},
              {"reg_id": "R2", "speaker": "兰", "referent": "甲",
               "paths": [F_SIB, M_SIB]}],
    )
    print("口述后两谱仍可成立:",
          sorted(t["tree_id"] for t in s.kinship.tenable_trees(
              "li_fang", registration_ids=["R1", "R2"])))
    print("字模只有姑母/姨母时安全词:",
          s.invitation.safe_terms_for_card("Q1")["safe_terms"], "-> 不能寄")
    r = s.invitation.print_local("p1", "Q1", "姑母", "S1")
    assert r["status"] == "rejected" and \
        r["error"]["reason"] == "term_not_safe_across_all_trees"

    s.kinship.add_testimony("N1", "li_fang", "op-n1",
                            named=True, asserted_tree="T_pat")
    print("加实名证言定为父系姐姐后安全词:",
          s.invitation.safe_terms_for_card("Q1")["safe_terms"])
    r = s.invitation.print_local("p2", "Q1", "姑母", "S1")
    assert r["status"] == "ok" and r["term"] == "姑母"
    print("两人共用姑母，已寄:", r["envelope_no"])

    # 撤姑母字模、保留证言
    s2 = LibrarySystem(":memory:")
    setup(
        s2, [tree("T_pat", "pat"), tree("T_mat", "mat")], [GU, YI],
        ["姑母", "姨母"],
        [{"card_id": "Q1", "recipient": "甲", "signers": ["青", "兰"],
          "deadline_shift": "S1"}])
    s2.kinship.add_testimony("N1", "li_fang", "op-n1",
                             named=True, asserted_tree="T_pat")
    s2.invitation.set_mold("姑母", False, op_id="off-gu")
    r = s2.invitation.print_local("p3", "Q1", "姑母", "S1")
    assert r["status"] == "rejected" and r["error"]["reason"] == "mold_unavailable"
    print("保留证言、撤去姑母字模 ->", r["error"]["reason"], "，又不能寄")

    # 无安全词联名柬：每人各自可能正确，无一词对全部家系正确
    s3 = LibrarySystem(":memory:")
    setup(
        s3, [tree("T_pat", "pat"), tree("T_mat", "mat")], [GU, YI],
        ["姑母", "姨母"],
        [{"card_id": "QX", "recipient": "甲", "signers": ["青", "兰"],
          "deadline_shift": "S1"}])
    info = s3.invitation.safe_terms_for_card("QX")
    print("联名柬逐人逐谱可用词:", json.dumps(info["per_signer"], ensure_ascii=False))
    print("对全部落款人×全部家系都正确的词:", info["safe_terms"], "-> 暂存")
    assert info["safe_terms"] == []
    s.close(); s2.close(); s3.close()


def act_two() -> None:
    banner("第二幕 两份仅隐私身份相反的谱：成品不可反推，去中性字模后只能封缄")
    s = LibrarySystem(":memory:")
    setup(
        s,
        [tree("T_pat", "pat", side="patrilineal"),
         tree("T_mat", "mat", side="matrilineal")],
        [GU, YI, ZHANG], ["姑母", "姨母", "长辈"],
        [{"card_id": "Q_plain", "recipient": "甲", "signers": ["青"],
          "deadline_shift": "S1"}])
    assert not s.invitation.card_is_sensitive(s.invitation.get_card("Q_plain"))
    r = s.invitation.print_local("p1", "Q_plain", "长辈", "S1")
    assert r["status"] == "ok"
    print("非敏感收件人两谱都印长辈，礼房身份剩余:",
          s.invitation.identity_remaining_possible())

    s.invitation.return_envelope("ret", r["envelope_no"])  # 释放收件人
    s.invitation.set_mold("长辈", False, op_id="off-zhang")
    late = s.invitation.register_card(
        "Q_late", "甲", ["青"], "V1", "S2", op_id="card-late")
    assert late["status"] == "ok"
    print("撤中性字模后新柬敏感:",
          s.invitation.card_is_sensitive(s.invitation.get_card("Q_late")))
    rj = s.invitation.print_local("p2", "Q_late", "姑母", "S2")
    assert rj["status"] == "rejected" and \
        rj["error"]["reason"] == "sensitive_cover_requires_sealed"
    rc = s.invitation.release_term_credential("Q_late", "cred1")
    assert rc["status"] == "rejected" and \
        rc["error"]["reason"] == "privacy_leak_term_distinguishes_identity"
    print("普通印台拒印:", rj["error"]["reason"],
          "| 配词凭据拒交:", rc["error"]["reason"])
    # 封缄直寄：访谈员具名证言后按实际谱选词，礼房只见素封
    s.kinship.add_testimony("N2", "interviewer", "op-n2",
                            named=True, asserted_tree="T_pat")
    sealed = s.invitation.print_sealed("sl", "Q_late", "姑母", "K1", "S2")
    assert sealed["status"] == "ok"
    print("封缄直寄礼房视角:", s.invitation.li_fang_mailings()[-1])
    print("访谈员封件核对:",
          [(m["card_id"], m["recipient"], m["term"])
           for m in s.invitation.interviewer_mailings()])
    assert s.invitation.identity_remaining_possible() == \
        {"patrilineal": 1, "matrilineal": 1}
    s.close()


def act_three() -> None:
    banner("第三幕 四份家谱：联名先后 × 字盘，单项可过、联合泄密、代价落在封数")
    four = [
        tree("T1", "pat", side="patrilineal",
             attrs={"order": "A_first", "tray": "east"}),
        tree("T2", "mat", side="matrilineal",
             attrs={"order": "A_first", "tray": "west"}),
        tree("T3", "mat", side="matrilineal",
             attrs={"order": "B_first", "tray": "east"}),
        tree("T4", "pat", side="patrilineal",
             attrs={"order": "B_first", "tray": "west"}),
    ]
    s = LibrarySystem(":memory:")
    # 两位受保护收件人：甲（父系姐/母系姐），乙在两谱中同样是父或母的同辈长辈
    setup(s, four, [GU, YI, ZHANG], ["姑母", "姨母", "长辈"],
          [{"card_id": "Q1", "recipient": "甲", "signers": ["青"],
            "deadline_shift": "S1"},
           {"card_id": "Q2", "recipient": "乙", "signers": ["兰"],
            "deadline_shift": "S1"}],
          trays=["东盘", "西盘"])
    print("初始两身份世界数:", s.invitation.identity_remaining_possible())
    print("单项查顺序=甲先:",
          s.invitation.gate_check_attr("order", "A_first")["remaining_worlds"])
    print("单项查盘号=东盘(假设性):",
          s.invitation.gate_check_attr("tray", "east",
                                      hypothetical=True)["remaining_worlds"])
    s.invitation.release_attr("order", "A_first", "attr-order")
    leak = s.invitation.release_attr("tray", "east", "attr-tray")
    assert leak["status"] == "rejected" and \
        leak["error"]["reason"] == "privacy_leak_single_item"
    print("顺序+盘号同时放行 ->", leak["error"]["reason"],
          leak["error"]["remaining_worlds"])
    print("礼房实际收到的字段:",
          [h["payload"] for h in s.invitation.li_fang_handover()
           if h["field_kind"] == "attr"])
    assert s.invitation.identity_remaining_possible() == \
        {"patrilineal": 1, "matrilineal": 1}

    # 保护代价：撤中性字模后两张敏感柬（不同收件人），唯一钥位一班只封一封
    s.invitation.set_mold("长辈", False, op_id="off")
    assert s.invitation.card_is_sensitive(s.invitation.get_card("Q1"))
    assert s.invitation.card_is_sensitive(s.invitation.get_card("Q2"))
    # 实际谱 T1 下两柬都有专称（姑母），但 K1 一班一封 -> 只能寄出一封
    s.kinship.add_testimony("N3", "interviewer", "op-n3",
                            named=True, asserted_tree="T1")
    batch = s.invitation.run_batch("S1", "batch")
    print("本批封缄:", batch["mailed"])
    print("本批暂存:", batch["held"], "| 钥位余量:", batch["keyed_remaining"])
    assert len([m for m in batch["mailed"] if m["mode"] == "sealed"]) == 1
    assert len(batch["held"]) == 1  # 保护一项的代价：少交一封
    s.close()


def act_four() -> None:
    banner("第四幕 同日两封信：local 最多普通信，sealed 共交两封，钥位预占即受限")
    # 方式一：礼房看得见经手封面 -> 撤中性字模后敏感信只能暂存
    s = LibrarySystem(":memory:")
    load_material(s)
    s.kinship.add_testimony("N1", "interviewer", "op-n1",
                            named=True, asserted_tree="T_pat")
    r1 = s.invitation.print_local("lp", "Q_plain", "长辈", "S1")
    assert r1["status"] == "ok"
    s.invitation.set_mold("长辈", False, op_id="off-zhang")
    rj = s.invitation.print_local("lx", "Q_sens", "姑母", "S1")
    assert rj["status"] == "rejected" and \
        rj["error"]["reason"] == "sensitive_cover_requires_sealed"
    print("方式一 local：普通信已寄", r1["envelope_no"],
          "；撤中性字模后敏感信", rj["error"]["reason"], "，只能暂存")
    s.close()

    # 方式二：访谈员封缄直寄 -> 共交两封，礼房两身份下投影同形
    s = LibrarySystem(":memory:")
    load_material(s)
    s.kinship.add_testimony("N1", "interviewer", "op-n1",
                            named=True, asserted_tree="T_pat")
    r1 = s.invitation.print_local("lp", "Q_plain", "长辈", "S1")
    r2 = s.invitation.print_sealed("ls", "Q_sens", "姑母", "K1", "S1")
    assert r1["status"] == "ok" and r2["status"] == "ok"
    print("方式二 sealed：普通信", r1["envelope_no"], "敏感信素封", r2["envelope_no"])
    print("礼房整批回执:")
    print(json.dumps(s.invitation.li_fang_mailings(), ensure_ascii=False, indent=2))
    print("获权岗位封件核对:")
    print(json.dumps(s.invitation.interviewer_mailings(),
                     ensure_ascii=False, indent=2))
    assert s.invitation.identity_remaining_possible() == \
        {"patrilineal": 1, "matrilineal": 1}

    # 钥位先给已有签单：直寄也只能交普通信（用干净库复现同一约束）
    s = LibrarySystem(":memory:")
    load_material(s)
    s.kinship.add_testimony("N1", "interviewer", "op-n1",
                            named=True, asserted_tree="T_pat")
    s.invitation.register_card(
        "Q_hold", "占位收件人", ["沈青"], "V1", "S1", op_id="card-hold")
    s.invitation.assign_keyed_position("Q_hold", "K1", "S1")
    r1 = s.invitation.print_local("lp", "Q_plain", "长辈", "S1")
    r2 = s.invitation.print_sealed("ls", "Q_sens", "姑母", "K1", "S1")
    assert r1["status"] == "ok"
    assert r2["status"] == "rejected" and \
        r2["error"]["reason"] == "keyed_position_shift_taken"
    hold = s.invitation.hold_card("Q_sens", "no_keyed_capacity", "hold1")
    assert hold["status"] == "ok"
    print("钥位预占后：普通信", r1["status"], "；敏感信",
          r2["error"]["reason"], "->", s.invitation.get_card("Q_sens")["status"])
    s.close()


def act_five() -> None:
    banner("第五幕 基础规则：无环、谱外长辈、归并后副本号定词义、重开与幂等")
    s = LibrarySystem(":memory:")
    s.catalog.submit_revision(
        "k1", ["WA", "WB"], [],
        [{"id": "V1", "book": "WA", "branch": "东村", "format": "平装"},
         {"id": "V2", "book": "WB", "branch": "西村", "format": "平装"}],
        op_id="cat1")
    s.catalog.submit_revision(
        "k2", ["WA", "WB"], [{"from": "WB", "to": "WA"}],
        [{"id": "V1", "book": "WA", "branch": "东村", "format": "平装"},
         {"id": "V2", "book": "WB", "branch": "西村", "format": "平装"}],
        op_id="cat2")
    s.catalog.activate_revision("k2")
    s.kinship.register_copy_terms("V1", [GU], op_id="tv1")
    s.kinship.register_copy_terms(
        "V2", [{"term": "阿姑", "patterns": [F_SIB]}], op_id="tv2")
    print("归并后规范映射:", s.catalog.canonical_map("k2"))
    print("按实际副本号读词义 V1:", s.kinship.get_copy_terms("V1")["terms"],
          " V2:", s.kinship.get_copy_terms("V2")["terms"])

    # 含环家系整批拒绝
    cyc = s.kinship.register_tree(
        "Tbad",
        [{"id": "A", "father": "B", "mother": None, "seniority": None},
         {"id": "B", "father": "A", "mother": None, "seniority": None}],
        op_id="tb")
    assert cyc["status"] == "rejected" and cyc["error"]["reason"] == "kinship_cycle"
    print("含环家系拒绝:", cyc["error"]["reason"])

    # 幂等：同文重放 / 异文冲突
    r1 = s.kinship.register_copy_terms(
        "V1", [GU], op_id="tv1")  # 已存在，同文重放
    print("同文重放返回同一回执:", r1["status"])
    try:
        s.catalog.submit_revision(
            "cat2", ["WA"], [], [], op_id="cat2")  # 异文占用同 op_id
        raise AssertionError("未捕获异文冲突")
    except IdempotencyConflict:
        print("异文重放 -> IdempotencyConflict")

    # 重开持久化、按单号续办
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "inv.db")
        p = LibrarySystem(path)
        load_material(p)
        p.kinship.add_testimony("N1", "interviewer", "op-n1",
                                named=True, asserted_tree="T_pat")
        sent = p.invitation.print_sealed("s1", "Q_sens", "姑母", "K1", "S1")
        p.close()
        again = LibrarySystem.reopen(path)
        print("重开后柬状态:",
              again.invitation.get_card("Q_sens")["status"],
              "| 素封状态:",
              again.invitation.public_mail_status(sent["envelope_no"]))
        nxt = again.invitation.register_card(
            "Q_new", "新收件人", ["沈青"], "V1", "S9", op_id="new-card")
        assert nxt["status"] == "ok"
        print("重开后仍可反复办理新柬:", nxt["card_id"])
        again.close()
    s.close()


def main() -> None:
    act_one()
    act_two()
    act_three()
    act_four()
    act_five()
    banner("全部请柬验收断言通过")


if __name__ == "__main__":
    main()
