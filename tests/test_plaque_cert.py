"""销量认证模型的单元测试：python3 -m unittest tests.test_plaque_cert -v"""

import unittest

from plaque_cert.alloc import PlaqueSpec, max_plaques
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


def _pkg(pid, code, skus=("R1",), store="S", batch="b1"):
    return Package(pid, batch, store, list(skus), code)


def _simple_reconciler(n=3, with_receipts=True):
    r = Reconciler("t")
    r.register_disc(Disc("R1", "rec1", "首版"))
    for i in range(1, n + 1):
        r.register_package(_pkg(f"p{i}", f"C{i:02d}"))
        if with_receipts:
            r.register_receipt(ExternalReceipt(f"rc{i}", "S", f"p{i}", ["R1"], f"o{i}"))
    r.register_draft(PeriodDraft("b1", 0))
    return r


class ReconcileTests(unittest.TestCase):
    def test_hash_chain_tamper_detection(self):
        r = _simple_reconciler()
        entries, issues = r.run()
        self.assertEqual(issues, [])
        self.assertTrue(Reconciler.verify_chain(entries))
        entries[0].retained_skus = ["FORGED"]
        self.assertFalse(Reconciler.verify_chain(entries))

    def test_internal_transfer_is_not_retail(self):
        r = _simple_reconciler(with_receipts=False)
        r.register_transfer(InternalTransfer("i1", "P-INV", "S-INV", "P", "S", "b1",
                                             tuple(p.pkg_id for p in r.packages.values())))
        entries, issues = r.run()
        self.assertTrue(all(e.status == "revoked" for e in entries))
        self.assertTrue(any("实际外售缺席" in i.reason for i in issues))

    def test_double_count_same_package_rejected(self):
        r = _simple_reconciler()
        r.register_receipt(ExternalReceipt("rc-dup", "S", "p1", ["R1"], "o-dup"))
        _, issues = r.run()
        self.assertTrue(any("重复计零售" in i.reason for i in issues))

    def test_partial_return_keeps_remaining_disc(self):
        r = Reconciler("t")
        r.register_disc(Disc("R1", "rec1", "甲"))
        r.register_disc(Disc("R2", "rec2", "乙"))
        r.register_package(_pkg("p1", "C1", ("R1", "R2")))
        r.register_receipt(ExternalReceipt("rc1", "S", "p1", ["R1", "R2"], "o1"))
        r.register_return(ReturnVoucher("rv1", "p1", ("R2",)))
        entries, _ = r.run()
        e = entries[0]
        self.assertEqual(e.status, "eligible")
        self.assertEqual(e.eligible_releases, ["R1"])  # 只退乙，仍可给甲作证

    def test_return_exceeding_original_reported(self):
        r = Reconciler("t")
        r.register_disc(Disc("R1", "rec1", "甲"))
        r.register_package(_pkg("p1", "C1", ("R1",)))
        r.register_receipt(ExternalReceipt("rc1", "S", "p1", ["R1"], "o1"))
        r.register_return(ReturnVoucher("rv1", "p1", ("R1", "R1")))  # 超出原配
        entries, issues = r.run()
        self.assertTrue(any("超出原配" in i.reason for i in issues))
        # 合规的那 1 盘照退；单盘包退尽 => revoked
        self.assertEqual(entries[0].returned_qty, 1)
        self.assertEqual(entries[0].status, "revoked")
        self.assertTrue(entries[0].note.startswith("退货超原配"))

    def test_reprocessed_returns_separate_book(self):
        r = _simple_reconciler()
        r.register_return(ReturnVoucher("rv-x", "p1", ("R1",), reprocessed=True))
        r.run()
        self.assertEqual(len(r.reprocessed_summary()), 1)

    def test_package_cap(self):
        r = _simple_reconciler(n=12)
        with self.assertRaises(ValueError):
            r.register_package(_pkg("p13", "C13"))

    def test_batch_qualified_formula(self):
        # 原售 3 盘（含一只整退包），实退 1，合格份数 2，与底稿未售数 1 对齐
        r = Reconciler("t")
        r.register_disc(Disc("R1", "rec1", "甲"))
        r.register_package(_pkg("p1", "C1"))
        r.register_package(_pkg("p2", "C2"))
        r.register_package(_pkg("p3", "C3"))
        for pid in ("p1", "p2", "p3"):
            r.register_receipt(ExternalReceipt(f"rc-{pid}", "S", pid, ["R1"], "o"))
        r.register_return(ReturnVoucher("rv", "p3", ("R1",)))
        r.register_draft(PeriodDraft("b1", 1))
        _, issues = r.run()
        self.assertEqual(issues, [])


class AllocTests(unittest.TestCase):
    def test_threshold_and_one_code_one_plaque(self):
        r = _simple_reconciler(n=4)
        entries, _ = r.run()
        specs = [PlaqueSpec("A", 2, frozenset({"R1"})),
                 PlaqueSpec("B", 2, frozenset({"R1"}))]
        got = max_plaques(entries, specs)
        self.assertEqual(len(got), 2)
        used = [c for a in got for c in a.codes]
        self.assertEqual(len(used), len(set(used)))  # 码不重复

    def test_linkage_all_or_nothing(self):
        r = Reconciler("t")
        r.register_disc(Disc("RA", "a", "甲"))
        r.register_disc(Disc("RB", "b", "乙"))
        for pid, code, skus in [("p1", "CA", ["RA"]), ("p2", "CB", ["RB"]),
                                ("p3", "K1", ["RA", "RB"]), ("p4", "K2", ["RA", "RB"])]:
            r.register_package(Package(pid, "b1", "S", skus, code))
            r.register_receipt(ExternalReceipt(f"rc-{pid}", "S", pid, skus, "o"))
        entries, _ = r.run()
        specs = [PlaqueSpec("甲", 2, frozenset({"RA"})),
                 PlaqueSpec("乙", 2, frozenset({"RB"}))]
        self.assertEqual(len(max_plaques(entries, specs)), 2)
        linked = max_plaques(entries, specs, {"LG": ("K1", "K2")})
        self.assertEqual(len(linked), 1)  # 联号同牌 => 只有一张凑得齐

    def test_reissue_merge_permission(self):
        r = Reconciler("t")
        r.register_disc(Disc("R1", "rec1", "首版"))
        r.register_disc(Disc("R1R", "rec1", "再版"))
        r.register_package(_pkg("p1", "C1", ("R1R",)))
        r.register_receipt(ExternalReceipt("rc1", "S", "p1", ["R1R"], "o"))
        entries, _ = r.run()
        merged = max_plaques(entries, [PlaqueSpec("A", 1, frozenset({"R1", "R1R"}))])
        no_merge = max_plaques(entries, [PlaqueSpec("A", 1, frozenset({"R1"}))])
        self.assertEqual(len(merged), 1)
        self.assertEqual(no_merge, [])


class HallTests(unittest.TestCase):
    def _net(self, mode=Mode.OFFLINE_MOLD):
        net = HallNetwork(mode, {"G": ("K1", "K2")})
        net.admit_code("K1")
        net.admit_code("K2")
        if mode == Mode.OFFLINE_MOLD:
            net.set_mold_holder("G", Location.WEST.value)
        return net

    def test_schedule_without_authorization_cannot_stamp(self):
        net = self._net()
        net.schedule("O1", Location.WEST.value, "P", ("K1", "K2"))
        with self.assertRaises(HallError):
            net.stamp("O1")

    def test_non_holder_return_does_not_block(self):
        net = self._net()
        net.return_arrives_non_holder("K1", Location.EAST.value)
        self.assertEqual(net.state["K1"], CodeState.UNENGRAVED)
        net.schedule("O1", Location.WEST.value, "P", ("K1", "K2"))
        net.authorize("O1")
        net.stamp("O1")  # 退单未到持馆，已到点承诺照常落印
        self.assertEqual(net.state["K1"], CodeState.ENGRAVED)

    def test_holder_return_revokes_unengraved_and_pauses(self):
        net = self._net()
        net.schedule("O1", Location.EAST.value, "P", ("K1", "K2"))
        ho = net.handover(("K1", "K2"), Location.WEST.value, Location.EAST.value)
        net.complete_handover(ho)
        net.set_mold_holder("G", Location.EAST.value)
        net.authorize("O1")
        net.holder_processes_return("K1")
        self.assertEqual(net.state["K1"], CodeState.REVOKED)
        self.assertEqual(net.state["K2"], CodeState.REVOKED)  # 联号整组
        with self.assertRaises(HallError):
            net.stamp("O1")

    def test_engraved_return_withdraws_but_never_releases_code(self):
        net = self._net()
        net.schedule("O1", Location.WEST.value, "P", ("K1", "K2"))
        net.authorize("O1")
        net.stamp("O1")
        net.holder_processes_return("K1")
        self.assertEqual(net.state["K1"], CodeState.WITHDRAWN)
        # 撤证后的码不能再刻任何牌
        with self.assertRaises(HallError):
            net.schedule("O2", Location.WEST.value, "Q", ("K1",))

    def test_in_transit_cannot_authorize(self):
        for mode in (Mode.OFFLINE_MOLD, Mode.ONLINE_SEAL):
            net = self._net(mode)
            net.schedule("O1", Location.EAST.value, "P", ("K1", "K2"))
            net.handover(("K1", "K2"), Location.WEST.value, Location.EAST.value)
            with self.assertRaises(HallError):
                net.authorize("O1")

    def test_dissolve_allows_independent_transfer(self):
        net = self._net()
        net.dissolve_group("G")
        ho = net.handover(("K1",), Location.WEST.value, Location.EAST.value)
        net.complete_handover(ho)  # 独立码可单独换馆
        self.assertEqual(net.location["K1"], Location.EAST)

    def test_linkage_group_cannot_split_at_handover(self):
        net = self._net()
        with self.assertRaises(HallError):
            net.handover(("K1",), Location.WEST.value, Location.EAST.value)

    def test_online_platform_blocks_after_accepted_return(self):
        net = self._net(Mode.ONLINE_SEAL)
        net.schedule("O1", Location.WEST.value, "P", ("K1", "K2"))
        net.holder_processes_return("K1")  # 签印台受理
        with self.assertRaises(HallError):
            net.authorize("O1")

    def test_unregistered_handover_point_rejected(self):
        net = self._net()
        with self.assertRaises(HallError):
            net.handover(("K1", "K2"), Location.WEST.value, Location.EAST.value, point="XX")

    def test_outbound_classification_distinguishes_revoked_and_engraved(self):
        net = self._net()
        net.schedule("O1", Location.WEST.value, "P", ("K1", "K2"))
        net.authorize("O1")
        net.stamp("O1")
        net.holder_processes_return("K1")  # 已刻 -> withdrawn
        net2_code_state = {(r.code, r.classification) for r in net.outbound_list()}
        self.assertTrue(any("撤证" in c for _, c in net2_code_state))
        self.assertTrue(any("永远不能再刻" in c for _, c in net2_code_state))


if __name__ == "__main__":
    unittest.main()
