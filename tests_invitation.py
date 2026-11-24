"""请柬/亲称业务的验收测试，严格对应题干中的每一个验收点（全部人物为虚构）。

运行：python3.11 -m unittest tests_invitation -v
分五组：
- 普通柬（无保密约定）：两姊妹、实名证言、撤字模、无安全词联名柬
- 两份相反隐私谱：成品反推检查、去中性字模
- 四份家谱的顺序×字盘单项/联合泄密
- 同日两封信的 local/sealed 两种办理、钥位预占
- 基础规则：家系无环、父母至多一人、谱外长辈、副本号决定词义、重开续办
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from library import IdempotencyConflict, Rejection
from library.scenarios import load_material, new_system_with_catalog
from library.system import LibrarySystem

# 关系路径的简写
F_SIB = [["up", "father"], ["sib"]]          # 父的姐/妹
M_SIB = [["up", "mother"], ["sib"]]          # 母的姐/妹
F_SIB_ELDER = [["up", "father"], ["sib", "elder"]]    # 父的姐
F_SIB_YOUNGER = [["up", "father"], ["sib", "younger"]]  # 父的妹
GU_TERM = {"term": "姑母", "patterns": [F_SIB]}
YI_TERM = {"term": "姨母", "patterns": [M_SIB]}
ZHANG_TERM = {"term": "长辈", "patterns": [F_SIB, M_SIB]}


def _tree(tree_id: str, kind: str, side=None, attrs=None, extra=None):
    """造一份最小谱：说话人 青/兰 是同胞；甲是其父或母的姐姐。

    kind='pat'：甲与“父”同父母（父系姐姐）；kind='mat'：甲与“母”同父母。
    extra：追加人物（谱外长辈等）。
    """
    base = [
        {"id": "青", "father": "父", "mother": "母", "seniority": 1},
        {"id": "兰", "father": "父", "mother": "母", "seniority": 2},
    ]
    if kind == "pat":
        base += [
            {"id": "父", "father": "祖父", "mother": "祖母", "seniority": 2},
            {"id": "母", "father": None, "mother": None, "seniority": None},
            {"id": "甲", "father": "祖父", "mother": "祖母", "seniority": 1},
            {"id": "祖父", "father": None, "mother": None, "seniority": None},
            {"id": "祖母", "father": None, "mother": None, "seniority": None},
        ]
    else:
        base += [
            {"id": "母", "father": "外祖父", "mother": "外祖母", "seniority": 2},
            {"id": "父", "father": None, "mother": None, "seniority": None},
            {"id": "甲", "father": "外祖父", "mother": "外祖母", "seniority": 1},
            {"id": "外祖父", "father": None, "mother": None, "seniority": None},
            {"id": "外祖母", "father": None, "mother": None, "seniority": None},
        ]
    if extra:
        base += extra
    return {"tree_id": tree_id, "persons": base, "side": side, "attrs": attrs or {}}


def _setup(system, trees, entries, molds, cards, registrations=None,
           positions=None, trays=None):
    """通用装载：一份 V1 副本 + 词条 + 谱 + 字模 + 柬（+口述/印位/字盘）。"""
    system.catalog.submit_revision(
        "k1", ["WX"], [],
        [{"id": "V1", "book": "WX", "branch": "东村", "format": "平装"}],
        op_id="cat",
    )
    system.catalog.activate_revision("k1")
    system.kinship.register_copy_terms("V1", entries, op_id="terms")
    for t in trees:
        system.kinship.register_tree(
            t["tree_id"], t["persons"], op_id=f"tree-{t['tree_id']}",
            side=t.get("side"), attrs=t.get("attrs"),
        )
    for reg in registrations or []:
        system.kinship.register_registration(
            reg["reg_id"], reg["speaker"], reg["referent"], reg.get("village", "东村"),
            "V1", reg["paths"], op_id=f"reg-{reg['reg_id']}",
        )
    for pos in positions or [{"position_id": "K1", "keyed": True}]:
        system.invitation.register_position(pos["position_id"], pos["keyed"])
    for tray in trays or []:
        system.invitation.add_tray(tray)
    for term in molds:
        system.invitation.set_mold(term, True, op_id=f"mold-{term}")
    for c in cards:
        system.invitation.register_card(
            c["card_id"], c["recipient"], c["signers"], "V1",
            c["deadline_shift"], op_id=f"card-{c['card_id']}",
            must_joint=c.get("must_joint"),
        )


class OrdinaryCardTest(unittest.TestCase):
    """场景一：没有保密约定的普通柬（side=None，两份谱原均可成立）。"""

    def _build(self, molds):
        system = LibrarySystem(":memory:")
        _setup(
            system,
            [_tree("T_pat", "pat"), _tree("T_mat", "mat")],
            [GU_TERM, YI_TERM],
            molds,
            [{"card_id": "Q1", "recipient": "甲",
              "signers": ["青", "兰"], "deadline_shift": "S1"}],
            registrations=[
                {"reg_id": "R1", "speaker": "青", "referent": "甲",
                 "paths": [F_SIB, M_SIB]},
                {"reg_id": "R2", "speaker": "兰", "referent": "甲",
                 "paths": [F_SIB, M_SIB]},
            ],
        )
        return system

    def test_no_safe_term_cannot_mail(self):
        # 字模只有姑母、姨母；两谱原均可成立 -> 无一对两谱都正确的词，不能寄
        system = self._build(["姑母", "姨母"])
        self.assertFalse(system.invitation.confidentiality_active())
        safe = system.invitation.safe_terms_for_card("Q1")["safe_terms"]
        self.assertEqual(safe, [])
        r = system.invitation.print_local("p1", "Q1", "姑母", "S1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "term_not_safe_across_all_trees")
        # 口述登记在两谱都可成立（父的姐或母的姐）
        self.assertEqual(
            sorted(t["tree_id"] for t in system.kinship.tenable_trees(
                "li_fang", registration_ids=["R1", "R2"])),
            ["T_mat", "T_pat"],
        )
        system.close()

    def test_named_testimony_fixes_patrilineal_then_gu_mails(self):
        system = self._build(["姑母", "姨母"])
        # 加一条实名证言定为父系姐姐（访谈员持有，礼房不可见则两谱仍在；
        # 这里直接交给礼房视角以验证配词收缩）
        system.kinship.add_testimony(
            "N1", "li_fang", "op-n1", named=True, asserted_tree="T_pat")
        safe = system.invitation.safe_terms_for_card("Q1")["safe_terms"]
        self.assertEqual(safe, ["姑母"])  # 两人共用姑母
        r = system.invitation.print_local("p2", "Q1", "姑母", "S1")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["term"], "姑母")
        system.close()

    def test_keep_testimony_remove_gu_mold_cannot_mail(self):
        system = self._build(["姑母", "姨母"])
        system.kinship.add_testimony(
            "N1", "li_fang", "op-n1", named=True, asserted_tree="T_pat")
        # 保留证言，只撤去姑母字模
        system.invitation.set_mold("姑母", False, op_id="mold-off-gu")
        safe = system.invitation.safe_terms_for_card("Q1")["safe_terms"]
        self.assertEqual(safe, [])
        r = system.invitation.print_local("p3", "Q1", "姑母", "S1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "mold_unavailable")
        system.close()

    def test_each_signer_has_term_but_none_for_all_trees(self):
        """词对每位落款人各自可能正确，却无一词对全部家系均正确的联名柬。"""
        system = LibrarySystem(":memory:")
        # 造两个落款人，各自在某一谱下能用姑母、另一谱下能用姨母，但交叉相反：
        # 青 在 T_pat 走父系姐（姑母），兰 在 T_mat 走母系姐（姨母）。
        trees = [_tree("T_pat", "pat"), _tree("T_mat", "mat")]
        _setup(
            system, trees, [GU_TERM, YI_TERM], ["姑母", "姨母"],
            [{"card_id": "QX", "recipient": "甲",
              "signers": ["青", "兰"], "deadline_shift": "S1"}],
        )
        info = system.invitation.safe_terms_for_card("QX")
        # 每位落款人在某个家系下都有正确词，但不存在对“全部人×全部谱”正确的词
        for signer, per_tree in info["per_signer"].items():
            self.assertTrue(any(per_tree.values()))
        self.assertEqual(info["safe_terms"], [])
        r = system.invitation.print_local("p4", "QX", "姑母", "S1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "term_not_safe_across_all_trees")
        system.close()


class OpposingPrivacyTreesTest(unittest.TestCase):
    """场景二：两份仅隐私身份相反的谱。"""

    def test_batch_cannot_reveal_identity_and_neutral_removal(self):
        system = LibrarySystem(":memory:")
        _setup(
            system,
            [
                _tree("T_pat", "pat", side="patrilineal"),
                _tree("T_mat", "mat", side="matrilineal"),
            ],
            [GU_TERM, YI_TERM, ZHANG_TERM],
            ["姑母", "姨母", "长辈"],
            [
                {"card_id": "Q_plain", "recipient": "甲",
                 "signers": ["青"], "deadline_shift": "S1"},
            ],
        )
        # 非敏感收件人：两谱都可印“长辈”，普通柬不泄密
        self.assertFalse(
            system.invitation.card_is_sensitive(system.invitation.get_card("Q_plain"))
        )
        self.assertEqual(
            system.invitation.safe_terms_for_card("Q_plain")["safe_terms"], ["长辈"]
        )
        r = system.invitation.print_local("p1", "Q_plain", "长辈", "S1")
        self.assertEqual(r["status"], "ok")
        # 礼房整批成品在两身份下投影同形，无法反推
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 1, "matrilineal": 1})

        # 去掉中性字模：新登记的同类柬（收件人仍是谱内长辈“甲”，新班次）变敏感。
        # 甲已被 Q_plain 占用，先把 Q_plain 退回以释放收件人，再开新柬。
        env = r["envelope_no"]
        system.invitation.return_envelope("ret1", env)
        system.invitation.set_mold("长辈", False, op_id="off-zhang")
        new_card = system.invitation.register_card(
            "Q_late", "甲", ["青"], "V1", "S2", op_id="card-late")
        self.assertEqual(new_card["status"], "ok")
        late = system.invitation.get_card("Q_late")
        self.assertTrue(system.invitation.card_is_sensitive(late))
        r2 = system.invitation.print_local("p2", "Q_late", "姑母", "S2")
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["error"]["reason"], "sensitive_cover_requires_sealed")
        # 强不可区分：配词凭据交接也被拒（两身份安全词不同）
        rc = system.invitation.release_term_credential("Q_late", "cred1")
        self.assertEqual(rc["status"], "rejected")
        self.assertEqual(
            rc["error"]["reason"], "privacy_leak_term_distinguishes_identity"
        )
        system.close()

    def test_sensitive_terms_only_valid_in_own_tree(self):
        system = LibrarySystem(":memory:")
        _setup(
            system,
            [
                _tree("T_pat", "pat", side="patrilineal"),
                _tree("T_mat", "mat", side="matrilineal"),
            ],
            [GU_TERM, YI_TERM],
            ["姑母", "姨母"],
            [{"card_id": "Q_sens", "recipient": "甲",
              "signers": ["青"], "deadline_shift": "S1"}],
        )
        card = system.invitation.get_card("Q_sens")
        self.assertTrue(system.invitation.card_is_sensitive(card))
        # 姑母仅父系谱成立、姨母仅母系谱成立
        self.assertTrue(system.kinship.term_valid_in_tree(
            "V1", "姑母", "青", "甲", "T_pat"))
        self.assertFalse(system.kinship.term_valid_in_tree(
            "V1", "姑母", "青", "甲", "T_mat"))
        self.assertTrue(system.kinship.term_valid_in_tree(
            "V1", "姨母", "青", "甲", "T_mat"))
        self.assertFalse(system.kinship.term_valid_in_tree(
            "V1", "姨母", "青", "甲", "T_pat"))
        # 礼房无安全词；访谈员具名证言后可在有钥位封缄
        self.assertEqual(system.invitation.safe_terms_for_card("Q_sens")["safe_terms"], [])
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T_mat")
        r = system.invitation.print_sealed("s1", "Q_sens", "姨母", "K1", "S1")
        self.assertEqual(r["status"], "ok")
        # 礼房只见素封编号 + 公开状态，不见词
        view = system.invitation.li_fang_mailings()[0]
        self.assertEqual(set(view), {"envelope_no", "sealed", "public_status", "shift"})
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 1, "matrilineal": 1})
        system.close()


class OrderTrayLeakTest(unittest.TestCase):
    """场景三：四份家谱，联名先后（甲先/乙先）×字盘（东盘/西盘）的联合泄密。"""

    def _four_trees(self):
        # 甲先东盘、乙先西盘 -> 父系；另两种 -> 母系
        return [
            _tree("T1", "pat", side="patrilineal",
                  attrs={"order": "A_first", "tray": "east"}),
            _tree("T2", "mat", side="matrilineal",
                  attrs={"order": "A_first", "tray": "west"}),
            _tree("T3", "mat", side="matrilineal",
                  attrs={"order": "B_first", "tray": "east"}),
            _tree("T4", "pat", side="patrilineal",
                  attrs={"order": "B_first", "tray": "west"}),
        ]

    def test_single_items_pass_but_joint_release_leaks(self):
        system = LibrarySystem(":memory:")
        _setup(
            system, self._four_trees(), [GU_TERM, YI_TERM, ZHANG_TERM],
            ["长辈"],
            [{"card_id": "Q1", "recipient": "甲",
              "signers": ["青"], "deadline_shift": "S1"}],
            trays=["东盘", "西盘"],
        )
        # 初始：父系、母系各两个世界
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 2, "matrilineal": 2})
        # 单项放行“顺序=甲先”：两身份仍各有世界
        chk1 = system.invitation.gate_check_attr("order", "A_first")
        self.assertTrue(chk1["protected"])
        r1 = system.invitation.release_attr("order", "A_first", "attr-order")
        self.assertEqual(r1["status"], "ok")
        # 单项放行“盘号=东盘”（假设性单项检查，不带已收顺序）也过
        chk2 = system.invitation.gate_check_attr("tray", "east", hypothetical=True)
        self.assertTrue(chk2["protected"])
        # 但在已收顺序资料上再放盘号：只剩 T1（父系），母系归零 -> 拒绝
        leak = system.invitation.release_attr("tray", "east", "attr-tray")
        self.assertEqual(leak["status"], "rejected")
        self.assertEqual(leak["error"]["reason"], "privacy_leak_single_item")
        # 交接日志里没有被拒的盘号，含旧回执在内仍保留两种身份
        fields = {k for h in system.invitation.li_fang_handover()
                  for k in h["payload"]}
        self.assertNotIn("tray", fields)
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 1, "matrilineal": 1})
        system.close()

    def test_protection_costs_a_card_in_batch(self):
        """保护一项的代价要在本批交付封数上看得见。"""
        system = LibrarySystem(":memory:")
        trees = self._four_trees()
        _setup(
            system, trees, [GU_TERM, YI_TERM, ZHANG_TERM],
            ["姑母", "姨母", "长辈"],
            [
                {"card_id": "Q1", "recipient": "甲",
                 "signers": ["青"], "deadline_shift": "S1"},
                {"card_id": "Q2", "recipient": "乙",
                 "signers": ["兰"], "deadline_shift": "S1"},
            ],
        )
        # 乙在四份谱里都不是青/兰的父或母的同胞：把 Q2 做成无安全词柬
        # （通过只给 Q1 可用词的方式不现实，这里直接验证容量代价：
        # 唯一钥位一班一封，两张敏感柬只能寄出一张，另一张暂存）
        system.invitation.set_mold("长辈", False, op_id="off-zhang")
        # 两张都变敏感
        self.assertTrue(system.invitation.card_is_sensitive(
            system.invitation.get_card("Q1")))
        self.assertTrue(system.invitation.card_is_sensitive(
            system.invitation.get_card("Q2")))
        # 访谈员具名证言，使其能在实际谱配词
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T1")
        batch = system.invitation.run_batch("S1", "batch")
        mailed = [m for m in batch["mailed"] if m["mode"] == "sealed"]
        self.assertEqual(len(mailed), 1)            # 唯一钥位只封一封
        self.assertEqual(len(batch["held"]), 1)    # 另一张只能暂存
        self.assertEqual(batch["keyed_remaining"][0]["remaining"], 0)
        # 礼房整批视角不暴露专称，两身份仍在
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 2, "matrilineal": 2})
        system.close()


class TwoLettersSameDeadlineTest(unittest.TestCase):
    """同日两封信：普通信两谱准印长辈，敏感信分谱用姑母/姨母。"""

    def test_local_mode_only_delivers_plain_letter(self):
        # 礼房看得见经手封面的办理方式：普通信可寄，敏感信只能暂存
        system = LibrarySystem(":memory:")
        load_material(system)  # Q_plain 给乙(长辈)、Q_sens 给甲
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T_pat")
        # 普通信两谱都准印长辈，普通印台即可
        r1 = system.invitation.print_local("lp", "Q_plain", "长辈", "S1")
        self.assertEqual(r1["status"], "ok")
        # 撤中性字模后甲柬敏感；普通印台不能代打敏感封面
        system.invitation.set_mold("长辈", False, op_id="off-zhang")
        rj = system.invitation.print_local("lx", "Q_sens", "姑母", "S1")
        self.assertEqual(rj["status"], "rejected")
        self.assertEqual(rj["error"]["reason"], "sensitive_cover_requires_sealed")
        # 最多交普通信一封；敏感信暂存
        self.assertEqual(len(system.invitation.li_fang_mailings()), 1)
        system.close()

    def test_sealed_mode_delivers_both_with_identical_li_fang_view(self):
        # 访谈员封缄直寄：共交两封，两种身份下礼房看到的投影仍相容
        system = LibrarySystem(":memory:")
        load_material(system)
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T_pat")
        r1 = system.invitation.print_local("lp", "Q_plain", "长辈", "S1")
        r2 = system.invitation.print_sealed("ls", "Q_sens", "姑母", "K1", "S1")
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r2["status"], "ok")
        # 礼房视角：普通信见封面，敏感信只见编号/公开状态/班次
        view = system.invitation.li_fang_mailings()
        sealed_view = next(v for v in view if v["sealed"])
        self.assertEqual(
            set(sealed_view), {"envelope_no", "sealed", "public_status", "shift"})
        local_view = next(v for v in view if not v["sealed"])
        self.assertEqual(local_view["term"], "长辈")
        # 获权岗位可见全量封件核对（落款、收件人、称呼）
        full = {m["card_id"]: m for m in system.invitation.interviewer_mailings()}
        self.assertEqual(full["Q_sens"]["term"], "姑母")
        self.assertEqual(full["Q_sens"]["signers"], ["沈青", "沈兰"])
        # 两身份下礼房看到的编号/状态/时点仍相容：两身份都还成立
        self.assertEqual(system.invitation.identity_remaining_possible(),
                         {"patrilineal": 1, "matrilineal": 1})
        system.close()

    def test_keyed_position_preassigned_then_sealed_also_only_plain(self):
        # 唯一钥位先给已有签单：直寄也只能交普通信，敏感信暂存
        system = LibrarySystem(":memory:")
        load_material(system)
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T_pat")
        system.invitation.register_card(
            "Q_hold", "占位收件人", ["沈青"], "V1", "S1", op_id="card-hold")
        system.invitation.assign_keyed_position("Q_hold", "K1", "S1")
        self.assertEqual(system.invitation.position_remaining("K1", "S1"), 0)
        r1 = system.invitation.print_local("lp", "Q_plain", "长辈", "S1")
        self.assertEqual(r1["status"], "ok")
        r2 = system.invitation.print_sealed("ls", "Q_sens", "姑母", "K1", "S1")
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["error"]["reason"], "keyed_position_shift_taken")
        hold = system.invitation.hold_card("Q_sens", "no_keyed_capacity", "hold1")
        self.assertEqual(hold["status"], "ok")
        self.assertEqual(system.invitation.get_card("Q_sens")["status"], "held")
        system.close()

    def test_li_fang_receipt_view_redacts_sealed_cover(self):
        # 礼房回执视图不得经封缄寄件的原始 result 读到称呼/落款
        system = LibrarySystem(":memory:")
        load_material(system)
        system.kinship.add_testimony(
            "N1", "interviewer", "op-n1", named=True, asserted_tree="T_pat")
        system.invitation.print_sealed("ls", "Q_sens", "姑母", "K1", "S1")
        view = system.invitation.li_fang_receipts()
        sealed = [r for r in view if r["kind"] == "print_sealed"]
        self.assertEqual(len(sealed), 1)
        self.assertNotIn("term", json.dumps(sealed[0], ensure_ascii=False))
        self.assertEqual(
            sealed[0]["result"]["li_fang_view"]["public_status"], "accepted")
        # 持钥岗位仍可经封件核对看到称呼
        full = next(m for m in system.invitation.interviewer_mailings()
                    if m["card_id"] == "Q_sens")
        self.assertEqual(full["term"], "姑母")
        system.close()


class FoundationRulesTest(unittest.TestCase):
    """基础规则：家系结构、谱外长辈、副本号决定词义、重开续办与幂等。"""

    def test_seniority_backs_collateral_terms(self):
        # 旁支称呼须由同一张家系与已登记长幼顺序支持
        system = new_system_with_catalog(
            [{"id": "V1", "book": "WX", "branch": "东村", "format": "平装"}])
        gu_elder = {"term": "姑母",
                    "patterns": [F_SIB_ELDER]}
        gu_young = {"term": "小姑",
                    "patterns": [F_SIB_YOUNGER]}
        system.kinship.register_copy_terms("V1", [gu_elder, gu_young], op_id="terms")
        persons = [
            # 父行：甲 seniority 1（姐）、父 seniority 2、丁 seniority 3（妹）
            {"id": "青", "father": "父", "mother": "母", "seniority": 1},
            {"id": "父", "father": "祖父", "mother": "祖母", "seniority": 2},
            {"id": "母", "father": None, "mother": None, "seniority": None},
            {"id": "甲", "father": "祖父", "mother": "祖母", "seniority": 1},
            {"id": "丁", "father": "祖父", "mother": "祖母", "seniority": 3},
            {"id": "祖父", "father": None, "mother": None, "seniority": None},
            {"id": "祖母", "father": None, "mother": None, "seniority": None},
        ]
        system.kinship.register_tree("T1", persons, op_id="t1")
        # 甲是父的姐姐 -> 姑母（elder）成立，小姑（younger）不成立
        self.assertTrue(system.kinship.term_valid_in_tree(
            "V1", "姑母", "青", "甲", "T1"))
        self.assertFalse(system.kinship.term_valid_in_tree(
            "V1", "小姑", "青", "甲", "T1"))
        # 丁是父的妹妹 -> 小姑成立、姑母不成立
        self.assertTrue(system.kinship.term_valid_in_tree(
            "V1", "小姑", "青", "丁", "T1"))
        self.assertFalse(system.kinship.term_valid_in_tree(
            "V1", "姑母", "青", "丁", "T1"))
        system.close()

    def test_kinship_cycle_rejected(self):
        system = LibrarySystem(":memory:")
        system.catalog.submit_revision(
            "k1", ["WX"], [],
            [{"id": "V1", "book": "WX", "branch": "东村", "format": "平装"}],
            op_id="cat")
        cyclic = [
            {"id": "A", "father": "B", "mother": None, "seniority": None},
            {"id": "B", "father": "A", "mother": None, "seniority": None},
        ]
        r = system.kinship.register_tree("Tbad", cyclic, op_id="tb")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "kinship_cycle")
        self.assertNotIn("Tbad", system.kinship.list_trees())
        system.close()

    def test_off_pedigree_elder_not_in_relations(self):
        system = new_system_with_catalog(
            [{"id": "V1", "book": "WX", "branch": "东村", "format": "平装"}])
        system.kinship.register_copy_terms("V1", [GU_TERM], op_id="terms")
        persons = [
            {"id": "青", "father": "父", "mother": "母", "seniority": 1},
            {"id": "父", "father": None, "mother": None, "seniority": None},
            {"id": "母", "father": None, "mother": None, "seniority": None},
            # 谱外长辈：父母留空
            {"id": "老寿星", "father": None, "mother": None,
             "seniority": None, "off_pedigree": True},
        ]
        r = system.kinship.register_tree("T1", persons, op_id="t1")
        self.assertEqual(r["status"], "ok")
        # 谱外长辈不能有父母
        bad = persons + [
            {"id": "怪长辈", "father": "父", "mother": None,
             "seniority": None, "off_pedigree": True}]
        r2 = system.kinship.register_tree("T2", bad, op_id="t2")
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["error"]["reason"], "off_pedigree_has_parent")
        # 谱外长辈不参与备选关系：姑母路径无法命中
        self.assertFalse(system.kinship.term_valid_in_tree(
            "V1", "姑母", "青", "老寿星", "T1"))
        system.close()

    def test_copy_id_decides_word_sense_after_merge(self):
        """书目归并后，实际取走的村志版本（副本号）仍决定词义。"""
        system = LibrarySystem(":memory:")
        system.catalog.submit_revision(
            "k1", ["WA", "WB"], [],
            [{"id": "V1", "book": "WA", "branch": "东村", "format": "平装"},
             {"id": "V2", "book": "WB", "branch": "西村", "format": "平装"}],
            op_id="cat1")
        # 归并 WB -> WA
        system.catalog.submit_revision(
            "k2", ["WA", "WB"], [{"from": "WB", "to": "WA"}],
            [{"id": "V1", "book": "WA", "branch": "东村", "format": "平装"},
             {"id": "V2", "book": "WB", "branch": "西村", "format": "平装"}],
            op_id="cat2")
        system.catalog.activate_revision("k2")
        # 两副本词条不同：V1 把父系姐叫“姑母”，V2 同关系却叫“阿姑”
        system.kinship.register_copy_terms("V1", [GU_TERM], op_id="tv1")
        system.kinship.register_copy_terms(
            "V2", [{"term": "阿姑", "patterns": [F_SIB]}], op_id="tv2")
        self.assertEqual(system.kinship.get_copy_terms("V1")["terms"], ["姑母"])
        self.assertEqual(system.kinship.get_copy_terms("V2")["terms"], ["阿姑"])
        # 规范书目已归并，但副本原始身份与各自词义都不被揉掉
        canon = system.catalog.canonical_map("k2")
        self.assertEqual(canon["WB"], "WA")
        with self.assertRaises(Rejection):
            system.kinship.get_copy_terms("V9")  # 未登记副本号不可读
        system.close()

    def test_reopen_keeps_cards_envelopes_and_handover(self):
        system = LibrarySystem(":memory:")
        load_material(system)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "inv.db")
            persisted = LibrarySystem(path)
            load_material(persisted)
            persisted.kinship.add_testimony(
                "N1", "interviewer", "op-n1", named=True, asserted_tree="T_pat")
            r = persisted.invitation.print_sealed(
                "s1", "Q_sens", "姑母", "K1", "S1")
            self.assertEqual(r["status"], "ok")
            env = r["envelope_no"]
            persisted.close()

            reopened = LibrarySystem.reopen(path)
            # 柬单、素封编号、交接日志、回执全部保留，可按单号续办
            self.assertEqual(reopened.invitation.get_card("Q_sens")["status"], "mailed")
            self.assertEqual(reopened.invitation.public_mail_status(env)
                             ["public_status"], "accepted")
            self.assertTrue(reopened.invitation.li_fang_handover())
            # 反复办理新柬：登记一张新单号柬
            new_card = reopened.invitation.register_card(
                "Q_new", "新收件人", ["沈青"], "V1", "S2", op_id="card-new")
            self.assertEqual(new_card["status"], "ok")
            reopened.close()
        system.close()

    def test_idempotent_replay_and_conflict(self):
        system = LibrarySystem(":memory:")
        load_material(system)
        r1 = system.invitation.print_local("lp", "Q_plain", "长辈", "S1")
        r2 = system.invitation.print_local("lp", "Q_plain", "长辈", "S1")
        self.assertEqual(r1, r2)  # 同文重放原样返回
        before = len(system.conn.execute("SELECT * FROM receipts").fetchall())
        with self.assertRaises(IdempotencyConflict):
            system.invitation.print_local("lp", "Q_plain", "姑母", "S1")  # 异文
        after = len(system.conn.execute("SELECT * FROM receipts").fetchall())
        self.assertEqual(before, after)  # 冲突不写回执
        system.close()

    def test_must_joint_and_recipient_once(self):
        system = LibrarySystem(":memory:")
        _setup(
            system,
            [_tree("T_pat", "pat"), _tree("T_mat", "mat")],
            [GU_TERM, YI_TERM, ZHANG_TERM], ["长辈"],
            [{"card_id": "Q1", "recipient": "甲",
              "signers": ["青", "兰"], "deadline_shift": "S1",
              "must_joint": ["青", "兰"]}],
        )
        # 两人必须联名给同一收件人：缺一人即拒
        r = system.invitation.register_card(
            "Qbad", "甲", ["青"], "V1", "S1", op_id="qb",
            must_joint=["青", "兰"])
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "must_joint_violation")
        # 收件人各须收一次：同收件人再开被拒
        r2 = system.invitation.register_card(
            "Q2", "甲", ["兰"], "V1", "S1", op_id="q2")
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["error"]["reason"], "recipient_already_served")
        system.close()

    def test_max_eight_signers_six_cards(self):
        system = LibrarySystem(":memory:")
        _setup(
            system, [_tree("T_pat", "pat")], [ZHANG_TERM], ["长辈"],
            [{"card_id": "Q0", "recipient": "甲",
              "signers": ["青"], "deadline_shift": "S1"}],
        )
        # 9 个落款人超限（bad_signer_count 是入事务前的参数拒绝，直接抛异常）
        with self.assertRaises(Rejection) as ctx:
            system.invitation.register_card(
                "Qbig", "超大", [f"人{i}" for i in range(9)],
                "V1", "S1", op_id="qb")
        self.assertEqual(ctx.exception.reason, "bad_signer_count")
        # 待发柬上限 6：Q0 已占一张，再登记 6 张时最后一张被拒
        for i in range(1, 7):
            rr = system.invitation.register_card(
                f"Q{i}", f"收件{i}", ["青"], "V1", "S1", op_id=f"q{i}")
            if i <= 5:
                self.assertEqual(rr["status"], "ok")
            else:
                self.assertEqual(rr["status"], "rejected")
                self.assertEqual(rr["error"]["reason"], "too_many_pending_cards")
        system.close()


if __name__ == "__main__":
    unittest.main()
