"""按 JSON 操作计划驱动真实状态机，并逐条断言结果。

操作类型（op）：
  post / register_stock / publish / replay_publish / statement / pending /
  gl / voucher / sources / restart / verify

断言（assert）：
  committed/replayed 状态、head、gl 余额、pending 问题多集合、错误码、statement 字段、
  独立核验结果、旧底稿复读一致性、来源可追溯。

运行方式：python scripts/run_plan.py plans/operations.json [--db state/books.db]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol import (  # noqa: E402
    EntityLedger,
    ConsolidationService,
    LedgerError,
    verify_worksheet,
)
from group_consol.jsoncodec import json_to_frac  # noqa: E402


class PlanRunner:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.ledger = EntityLedger(db_path)
        self.svc = ConsolidationService(self.ledger)
        self.remembered: dict[str, dict] = {}

    def restart(self):
        """关闭后用同一文件重新打开，验证真实可重开状态与旧前缀保留。"""
        self.ledger.close()
        self.ledger = EntityLedger(self.db_path)
        self.svc = ConsolidationService(self.ledger)

    # ------------------------------------------------------------- dispatch
    def run_op(self, step: dict):
        op = step["op"]
        if op == "post":
            return self.ledger.post(
                step["journal_id"], step["entity"], step["lines"], step.get("document")
            )
        if op == "register_stock":
            return self.ledger.register_stock(step["version"], step["items"])
        if op == "publish":
            return self.svc.publish(step["input_id"], step["heads"], step["stock_version"])
        if op == "replay_publish":
            # 同切面复读：结果必须与首次一致
            first = deepcopy(self.remembered[step["remember"]])
            again = self.svc.publish(step["input_id"], step["heads"], step["stock_version"])
            assert again == first, f"同切面复读结果不一致: {step.get('name')}"
            return again
        if op == "statement":
            return self.svc.statement(step["input_id"])
        if op == "pending":
            return self.svc.pending(step["input_id"])
        if op == "gl":
            return self.ledger.gl(step["entity"], step.get("prefix"))
        if op == "voucher":
            return self.ledger.voucher(step["entity"], step["seq"])
        if op == "sources":
            return self.svc.sources(step["input_id"], step.get("entry_id"))
        if op == "restart":
            self.restart()
            return {"restarted": True, "heads": self.ledger.heads()}
        if op == "verify":
            return verify_worksheet(self.ledger.conn, step["input_id"])
        raise ValueError(f"未知操作 {op}")

    def run(self, plan: list[dict]) -> list[dict]:
        results = []
        for index, step in enumerate(plan, 1):
            name = step.get("name", f"step-{index}")
            expect_error = step.get("expect_error")
            try:
                result = self.run_op(step)
            except LedgerError as exc:
                if expect_error is None:
                    raise AssertionError(f"[{name}] 未预期的错误 {exc.code}: {exc.message}") from exc
                self._assert_error(step, exc)
                results.append({"step": name, "ok": True, "error": exc.code})
                continue
            if expect_error is not None:
                raise AssertionError(f"[{name}] 预期错误 {expect_error} 但操作成功")
            if "assert" in step:
                self._assert_result(step["assert"], result, name)
            if step.get("remember"):
                self.remembered[step["remember"]] = deepcopy(result)
            results.append({"step": name, "ok": True, "result": self._brief(result)})
        return results

    # ------------------------------------------------------------ assertions
    @staticmethod
    def _assert_error(step: dict, exc: LedgerError):
        spec = step["expect_error"]
        assert exc.code == spec["code"], f"{step.get('name')}: 错误码 {exc.code} != {spec['code']}"
        for issue in spec.get("issues", []):
            details = exc.details.get("issues", [])
            assert any(
                i["issue_type"] == issue["issue_type"]
                and all(i["details"].get(k) == v for k, v in issue.get("details", {}).items())
                for i in details
            ), f"{step.get('name')}: 待核明细缺少 {issue}，实际 {details}"

    def _assert_result(self, spec: dict, result, name: str):
        for key, want in spec.items():
            if key == "head":
                got = self.ledger.heads()
                assert got == want, f"[{name}] heads {got} != {want}"
            elif key == "status":
                assert result["status"] == want, f"[{name}] status {result['status']} != {want}"
            elif key == "seq":
                assert result["seq"] == want, f"[{name}] seq {result['seq']} != {want}"
            elif key == "balances":
                got = result["balances"]
                for account, value in want.items():
                    assert got[account] == value, (
                        f"[{name}] 账户 {account} 余额 {got[account]} != {value}"
                    )
                assert sum(got.values()) == 0, f"[{name}] 总账借贷不平"
            elif key == "final":
                for account, value in want.items():
                    got = json_to_frac(result["final"][account])
                    assert got == Fraction(value[0], value[1]) if isinstance(value, list) \
                        else got == Fraction(value), (
                        f"[{name}] 最终 {account}: {got} != {value}"
                    )
            elif key == "elimination_kinds":
                counts = {}
                for entry in result["eliminations"]:
                    counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
                assert counts == want, f"[{name}] 抵销类型计数 {counts} != {want}"
            elif key == "entry":
                entry = next(
                    (e for e in result["eliminations"] if e["entry_id"] == want["entry_id"]), None
                )
                assert entry is not None, f"[{name}] 找不到分录 {want['entry_id']}"
                assert entry["kind"] == want["kind"]
                assert entry["invoice_id"] == want["invoice_id"]
                signed = {
                    ln["account"]: json_to_frac(ln["debit"]) - json_to_frac(ln["credit"])
                    for ln in entry["lines"]
                }
                for account, value in want["lines"].items():
                    expect = Fraction(value[0], value[1]) if isinstance(value, list) else Fraction(value)
                    assert signed[account] == expect, (
                        f"[{name}] 分录 {want['entry_id']} {account}: {signed[account]} != {expect}"
                    )
            elif key == "same_as_remembered":
                assert result == self.remembered[want], f"[{name}] 复读结果与首次不一致"
            elif key == "sources_resolved":
                # sources() 返回的每条来源都必须解析出真实凭证或声明条目
                resolved = result if isinstance(result, list) else [result]
                assert len(resolved) == want["count"]
                for item in resolved:
                    assert item["resolved_sources"], f"[{name}] 存在无来源分录"
                    for src in item["resolved_sources"]:
                        assert "voucher" in src or "stock_item" in src
            elif key == "verified":
                assert result["verified"] is True
                assert result["invoices_checked"] == want["invoices"]
                assert result["elimination_entries"] == want["entries"]
            elif key == "journal_count":
                assert len(result) == want
            elif key == "doc_type":
                assert result["doc_type"] == want
            else:
                raise AssertionError(f"[{name}] 不支持的断言字段 {key}")

    @staticmethod
    def _brief(result):
        if isinstance(result, dict) and "eliminations" in result:
            return {
                "input_id": result["input_id"],
                "status": result["status"],
                "final": result["final"],
                "entries": len(result["eliminations"]),
            }
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    parser.add_argument("--db", default="state/books.db")
    args = parser.parse_args()
    if os.path.exists(args.db):
        os.remove(args.db)
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    runner = PlanRunner(args.db)
    outcomes = runner.run(plan)
    print(f"全部 {len(outcomes)} 个计划步骤通过：")
    for item in outcomes:
        print(" -", item["step"], "->", item.get("error", "ok"))
    runner.ledger.close()


if __name__ == "__main__":
    main()
