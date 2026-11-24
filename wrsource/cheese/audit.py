"""Persistent audit desk tying weighted source versions to cuts and labels."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Mapping, Optional

from ..dataset import build_dataset, compare_datasets, refresh_dataset
from ..sampling import make_candidate, select_top
from ..source import SourceService
from .planner import plan_order, reconstruct_pieces
from .sample_io import quality_check_dataset
from .scenarios import four_cell_case

STATE_FILE = "cheese_desk_state.json"


class CheeseDesk:
    """A small JSON/SQLite-backed audit desk.

    The SQLite source is owned by :class:`SourceService`; cheese-specific cut
    records, sealed labels and dataset pointers are saved in a JSON state file.
    ``reopen(root)`` reloads exactly that audit state.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.db_path = os.path.join(self.root, "cheese_source.sqlite")
        self.state_path = os.path.join(self.root, STATE_FILE)
        self.state: Dict[str, Any] = {
            "datasets": {},
            "cuts": [],
            "labels": [],
            "plans": {},
            "events": [],
            "case": None,
            "latest_dataset": None,
        }

    @classmethod
    def create(cls, root: str, fresh: bool = True) -> "CheeseDesk":
        if fresh and os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root, exist_ok=True)
        desk = cls(root)
        if os.path.exists(desk.state_path):
            desk.state = json.load(open(desk.state_path, encoding="utf-8"))
        desk.save()
        return desk

    @classmethod
    def reopen(cls, root: str) -> "CheeseDesk":
        desk = cls(root)
        with open(desk.state_path, encoding="utf-8") as fh:
            desk.state = json.load(fh)
        return desk

    @classmethod
    def open(cls, root: str) -> "CheeseDesk":
        return cls.reopen(root)

    def service(self) -> SourceService:
        return SourceService(self.db_path)

    def save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.state_path)

    def event(self, kind: str, **payload) -> None:
        self.state.setdefault("events", []).append({"kind": kind, **payload})
        self.save()

    # -- canonical weighted audit -----------------------------------------

    def seed_four_cell_weighted_case(self) -> Dict[str, Any]:
        case = four_cell_case()
        self.state["case"] = case
        svc = self.service()
        svc.init()
        case_body = {
            "record_type": "cheese_case",
            # Probe rows live in the sampled probe group; keep block/box/fixture
            # facts in a high-quota case group.
            "blocks": case["blocks"],
            "boxes": case["boxes"],
        }
        ops: List[Dict[str, Any]] = [{
            "op": "add", "row_id": "cheese-case-four-cell",
            "group": "cheese_case", "weight": 100,
            "body": json.dumps(case_body, ensure_ascii=False), "shard": "qc",
        }]
        probe_bodies = {
            "probe-P0-left4": 4,
            "probe-P1-left4-backup": 4,
            "probe-P2-left4-true-next": 4,
            "probe-P3-left4-reserve": 4,
        }
        for rid, depth in probe_bodies.items():
            body = {
                "record_type": "probe", "probe_id": rid.replace("probe-", ""),
                "block_id": "B", "entry_face": "left",
                "grid_point": [0, 0], "direction": [1, 0, 0],
                "first_hole_depth": depth,
            }
            ops.append({
                "op": "add", "row_id": rid, "group": "cheese_probe",
                "weight": 50, "body": json.dumps(body, ensure_ascii=False),
                "shard": "probes",
            })
        result = svc.apply_batch(ops, note="seed cheese weighted audit")
        version = result.version if hasattr(result, "version") else svc.latest_version()
        seed = self._seed_with_p0_first(svc, int(version))
        self.state["seed_hex"] = seed.hex()
        self.state["source_version_seed"] = int(version)
        self.save()
        return {"version": int(version), "seed_hex": seed.hex()}

    def _seed_with_p0_first(self, svc: SourceService, version: int) -> bytes:
        # Deterministically choose a seed for which the actual exact sampler
        # ranks P0 first.  This is still the original sampler's real output.
        from ..dataset import candidates_for_version
        for counter in range(10_000):
            seed = bytes([0x43, 0x48, 0x45, 0x45, 0x53]) + counter.to_bytes(27, "big")
            cands = candidates_for_version(svc, version, seed, groups={"cheese_probe"})
            first = select_top(cands, 1)[0]
            if first.row_id == "probe-P0-left4":
                return seed
        raise RuntimeError("could not find deterministic seed ranking P0 first")

    def build_initial_sample(self) -> Dict[str, Any]:
        svc = self.service()
        version = self.state["source_version_seed"]
        seed = bytes.fromhex(self.state["seed_hex"])
        out = os.path.join(self.root, "sample-v1")
        manifest, records = build_dataset(
            svc, version, seed, {"cheese_case": 10, "cheese_probe": 1}, out
        )
        self.state["datasets"]["v1"] = out
        self.state["latest_dataset"] = "v1"
        first = next(r for r in records if r["group"] == "cheese_probe")
        self.state["sample_first_row_id"] = first["row_id"]
        self.event("initial_sample_built", dataset=out,
                   source_version=version, first_row_id=first["row_id"])
        return {"manifest": manifest, "records": records, "dir": out}

    def qc_dataset_key(self, key: str, verify: bool = True) -> Dict[str, Any]:
        path = self.state["datasets"][key]
        return quality_check_dataset(path, source_db=self.db_path, verify=verify,
                                    base_case=None)

    def correct_first_probe_to_depth1(self) -> Dict[str, Any]:
        svc = self.service()
        rid = self.state["sample_first_row_id"]
        body = {
            "record_type": "probe", "probe_id": "P-left-first-hole",
            "block_id": "B", "entry_face": "left", "grid_point": [0, 0],
            "direction": [1, 0, 0], "first_hole_depth": 1,
            "correction": "only first-hole depth corrected from 4 to 1",
        }
        res = svc.apply_batch([
            {"op": "set_body", "row_id": rid,
             "body": json.dumps(body, ensure_ascii=False)}
        ], note="correct sampled first probe depth to 1")
        v2 = int(res.version)
        old = self.state["datasets"]["v1"]
        out = os.path.join(self.root, "sample-v2-corrected-depth1")
        manifest, records, report = refresh_dataset(svc, old, v2, out_dir=out)
        self.state["datasets"]["v2_depth1"] = out
        self.event("body_corrected_depth1", row_id=rid, source_version=v2,
                   refresh_report=report)
        return {"version": v2, "dir": out, "refresh_report": report}

    def restore_first_probe(self) -> Dict[str, Any]:
        svc = self.service()
        rid = self.state["sample_first_row_id"]
        body = {
            "record_type": "probe", "probe_id": "P-left-first-hole",
            "block_id": "B", "entry_face": "left", "grid_point": [0, 0],
            "direction": [1, 0, 0], "first_hole_depth": 4,
            "restoration": "original depth-four record restored",
        }
        res = svc.apply_batch([
            {"op": "set_body", "row_id": rid,
             "body": json.dumps(body, ensure_ascii=False)}
        ], note="restore original probe depth four")
        v3 = int(res.version)
        old = self.state["datasets"]["v2_depth1"]
        out = os.path.join(self.root, "sample-v3-restored")
        manifest, records, report = refresh_dataset(svc, old, v3, out_dir=out)
        self.state["datasets"]["v3_restored"] = out
        self.event("original_record_restored", row_id=rid, source_version=v3)
        return {"version": v3, "dir": out, "refresh_report": report}

    def withdraw_first_and_replace(self) -> Dict[str, Any]:
        svc = self.service()
        rid = self.state["sample_first_row_id"]
        res = svc.apply_batch([
            {"op": "retract", "row_id": rid}
        ], note="withdraw sample first name")
        v4 = int(res.version)
        old = self.state["datasets"]["v3_restored"]
        out = os.path.join(self.root, "sample-v4-replacement")
        manifest, records, report = refresh_dataset(svc, old, v4, out_dir=out)
        probe_records = [r for r in records if r["group"] == "cheese_probe"]
        replacement = probe_records[0] if probe_records else None
        self.state["datasets"]["v4_replacement"] = out
        full_out = os.path.join(self.root, "sample-v4-full-rebuild")
        seed = bytes.fromhex(self.state["seed_hex"])
        build_dataset(svc, v4, seed,
                      {"cheese_case": 10, "cheese_probe": 1}, full_out)
        comparison = compare_datasets(out, full_out)
        self.state["datasets"]["v4_full"] = full_out
        self.state["replacement_matches_full_rebuild"] = comparison.get("equal", False)
        self.state["withdrawn_first_row_id"] = rid
        self.state["replacement_row_id"] = replacement["row_id"] if replacement else None
        self.state["latest_dataset"] = "v4_replacement"
        self.event("first_withdrawn_replaced", withdrawn=rid,
                   replacement=self.state["replacement_row_id"],
                   source_version=v4, refresh_report=report,
                   matches_full_rebuild=comparison.get("equal", False))
        return {"version": v4, "dir": out, "replacement": replacement,
                "refresh_report": report, "full_rebuild_comparison": comparison}

    # -- cuts/labels ------------------------------------------------------

    def seal_and_saw_midline(self, dataset_key: str = "v1") -> Dict[str, Any]:
        cut = {
            "cut_id": "saw-midline",
            "block_id": "B",
            "plane": {"id": "mid", "axis": "x", "at": 2},
            "left_id": "B-left",
            "right_id": "B-right",
            "source_dataset": self.state["datasets"][dataset_key],
            "source_version": self.state.get("source_version_seed", 1),
            "signed_label": {
                "box_id": "BOX-TWO-CELL-NO-HOLE",
                "strip_id": "two-cell-strip",
                "piece_id": "B-left",
            },
        }
        self.state["cuts"].append(cut)
        label = {
            "box_id": "BOX-TWO-CELL-NO-HOLE",
            "strip_id": "two-cell-strip",
            "named_piece_id": "B-left",
            "cut_id": cut["cut_id"],
            "dataset_key": dataset_key,
            "substitution_allowed": False,
        }
        self.state["labels"].append(label)
        self.event("midline_sealed_and_sawn", cut=cut, label=label)
        self.save()
        return cut

    def record_cut(self, cut: Mapping[str, Any]) -> Dict[str, Any]:
        item = dict(cut)
        if not item.get("cut_id"):
            item["cut_id"] = f"cut-{len(self.state['cuts']) + 1}"
        self.state["cuts"].append(item)
        self.event("cut_recorded", cut=item)
        return item

    def seal_plan(self, plan: Mapping[str, Any], plan_id: Optional[str] = None) -> Dict[str, Any]:
        plan_id = plan_id or f"sealed-plan-{len(self.state.get('plans', [])) + 1}"
        self.state.setdefault("plans", {})[plan_id] = plan
        for label in plan.get("labels", []):
            stored = dict(label)
            stored["plan_id"] = plan_id
            self.state["labels"].append(stored)
        self.event("plan_sealed", plan_id=plan_id,
                   boxes=plan.get("promised_box_ids", []))
        self.save()
        return {"plan_id": plan_id, "labels": plan.get("labels", [])}

    def plan_remaining_from_dataset(self, dataset_key: str, mode: str = "sealed",
                                   **kwargs) -> Dict[str, Any]:
        qc_report = self.qc_dataset_key(dataset_key, verify=False)
        boxes = kwargs.pop("boxes", self.state["case"]["boxes"])
        return plan_order(qc_report, boxes=boxes, existing_cuts=self.state["cuts"],
                          mode=mode, **kwargs)

    def recheck(self) -> Dict[str, Any]:
        """Separately audit the old signed cut and new-version remainder."""
        old_qc = self.qc_dataset_key("v1", verify=True)
        corrected_qc = self.qc_dataset_key("v2_depth1", verify=False)
        restored_qc = self.qc_dataset_key("v3_restored", verify=False)
        new_qc = self.qc_dataset_key("v4_replacement", verify=True)

        case = self.state["case"]
        boxes = case["boxes"]
        blocks = case["blocks"]
        pieces, history = reconstruct_pieces(blocks, self.state["cuts"])
        labeled = pieces.get("B-left")
        requirement = boxes[0]["strips"][0]
        old_models = old_qc["possible_models_by_block"]["B"]
        from .geometry import universal_fit
        old_label_fit = universal_fit(labeled, requirement, old_models) if labeled else None

        corrected_plan = plan_order(
            corrected_qc, boxes=boxes, existing_cuts=self.state["cuts"],
            mode="sealed", cut_budget=0,
            fixtures=blocks[0]["fixtures"],
        )
        restored_plan = plan_order(
            restored_qc, boxes=boxes, existing_cuts=self.state["cuts"],
            mode="sealed", cut_budget=0,
            fixtures=blocks[0]["fixtures"],
        )
        new_plan = plan_order(
            new_qc, boxes=boxes, existing_cuts=self.state["cuts"],
            mode="sealed", cut_budget=0,
            fixtures=blocks[0]["fixtures"],
        )
        result = {
            "old_version_cut": {
                "dataset": self.state["datasets"]["v1"],
                "cut_history": history,
                "labeled_piece": labeled,
                "label_fit_under_old_sample": old_label_fit,
                "supported": bool(old_label_fit and old_label_fit["safe"]),
            },
            "corrected_depth1_remainder": {
                "possible_models": corrected_qc["possible_model_ids_by_block"],
                "plan_status": corrected_plan["status"],
                "complete_box_count": corrected_plan["complete_box_count"],
            },
            "restored_remainder": {
                "possible_models": restored_qc["possible_model_ids_by_block"],
                "plan_status": restored_plan["status"],
                "piece": restored_plan["plan"]["assignments"][0]["piece_id"]
                if restored_plan["plan"].get("assignments") else None,
            },
            "new_version_remainder": {
                "dataset": self.state["datasets"]["v4_replacement"],
                "possible_models": new_qc["possible_model_ids_by_block"],
                "plan_status": new_plan["status"],
                "complete_box_count": new_plan["complete_box_count"],
                "piece": new_plan["plan"]["assignments"][0]["piece_id"]
                if new_plan["plan"].get("assignments") else None,
                "plan": new_plan,
            },
            "identity_invariant": (
                "the recorded cut produces B-left and B-right; reopen never "
                "merges them back into uncut B"
            ),
        }
        self.state["last_recheck"] = result
        self.save()
        return result


def build_weighted_audit(root: str, fresh: bool = True) -> CheeseDesk:
    desk = CheeseDesk.create(root, fresh=fresh)
    desk.seed_four_cell_weighted_case()
    desk.build_initial_sample()
    desk.seal_and_saw_midline("v1")
    desk.correct_first_probe_to_depth1()
    desk.restore_first_probe()
    desk.withdraw_first_and_replace()
    desk.recheck()
    return desk


def open_desk(root: str) -> CheeseDesk:
    return CheeseDesk.reopen(root)


def replay_original_case(root: Optional[str] = None) -> Dict[str, Any]:
    """Run pure examples plus the real weighted-source audit in one report."""
    from .scenarios import run_named_examples
    examples = run_named_examples()
    audit_root = root or os.path.join("demo", "_work", "cheese_audit")
    desk = build_weighted_audit(audit_root)
    report = {
        "examples": examples,
        "audit_root": desk.root,
        "audit": desk.state["last_recheck"],
        "events": desk.state["events"],
    }
    return report
