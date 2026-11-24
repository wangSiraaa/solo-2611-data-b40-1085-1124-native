"""Guaranteed cutting and box-allocation planner.

Two delivery contracts are supported:

``sealed`` / ``labels`` / ``no_table``
    Every cut and every named strip-to-box assignment must be fixed before the
    internal model is known.  A plan is accepted only if it fits every
    surviving model.

``table`` / ``face_table`` / ``adaptive``
    Rent an inspection table, make one first cut, observe only the visible
    cells on the two fresh faces, then follow an observation-specific fixed
    plan.  The table consumes one workstation beat.
"""

from __future__ import annotations

import copy
import itertools
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .geometry import (CheeseInputError, initial_piece, normalize_block,
                       normalize_plane, original_faces, piece_size,
                       plane_applies, shape_dimensions, split_piece,
                       universal_fit)
from .qc import quality_check

ReqKey = str


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------

def _as_qc(data: Any) -> Dict[str, Any]:
    if isinstance(data, Mapping) and "possible_models_by_block" in data:
        return dict(data)
    return quality_check(data)


def _blocks_from(qc: Mapping[str, Any], overrides: Optional[Sequence[Mapping]] = None
                 ) -> List[Dict[str, Any]]:
    if overrides is not None:
        return [normalize_block(b) for b in overrides]
    case = qc.get("normalized_case") or qc.get("case")
    if case and case.get("blocks"):
        # normalized_case has all models; qc.case may only have survivors.
        source_blocks = qc.get("normalized_case", {}).get("blocks") or case["blocks"]
        return [normalize_block(b) for b in source_blocks]
    if qc.get("blocks") and isinstance(qc["blocks"][0], Mapping):
        raw_blocks = []
        for b in qc["blocks"]:
            bb = dict(b)
            if "models" not in bb and "candidate_models" not in bb:
                bb["models"] = bb.get("possible_models", [])
            raw_blocks.append(bb)
        return [normalize_block(b) for b in raw_blocks]
    raise CheeseInputError("cutting planner needs block geometry/models")


def _models_by_block(qc: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]]
                    ) -> Dict[str, List[Mapping[str, Any]]]:
    if "possible_models_by_block" in qc:
        raw = {b: list(m) for b, m in qc["possible_models_by_block"].items()}
        all_models = {
            b["block_id"]: {m["model_id"]: m for m in b.get("models", [])}
            for b in blocks
        }
        resolved = {}
        for bid, items in raw.items():
            resolved[bid] = [
                item if isinstance(item, Mapping)
                else all_models.get(bid, {}).get(str(item), {"model_id": str(item)})
                for item in items
            ]
        return resolved
    # A raw case: QC it first.
    report = quality_check(qc)
    return {b: list(m) for b, m in report["possible_models_by_block"].items()}


def normalize_boxes(raw_boxes: Any) -> List[Dict[str, Any]]:
    boxes = []
    if isinstance(raw_boxes, Mapping):
        raw_boxes = [raw_boxes]
    for i, raw in enumerate(raw_boxes or []):
        box_id = str(raw.get("box_id") or raw.get("id") or raw.get("name")
                     or raw.get("盒号") or f"box-{i + 1}")
        strips_raw = raw.get("strips", raw.get(
            "required", raw.get("requirements", raw.get("条", raw.get("所需条")))))
        if isinstance(strips_raw, Mapping):
            strips_raw = [strips_raw]
        if not strips_raw and any(k in raw for k in ("shape", "dimensions", "length")):
            strips_raw = [dict(raw)]
        strips = []
        for j, sraw in enumerate(strips_raw):
            s = dict(sraw)
            strip_id = str(s.get("strip_id") or s.get("id") or s.get("name")
                           or s.get("条号") or f"strip-{j + 1}")
            s["strip_id"] = strip_id
            s["box_id"] = box_id
            s["key"] = f"{box_id}:{strip_id}"
            strips.append(s)
        boxes.append({"box_id": box_id, "strips": strips, "raw": dict(raw)})
    return boxes


def _fixtures_by_block(blocks: Sequence[Mapping[str, Any]],
                       extra: Any = None) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {b["block_id"]: list(b.get("fixtures", [])) for b in blocks}
    extras: List[Any] = []
    if isinstance(extra, Mapping) and all(isinstance(v, Sequence) and not isinstance(v, (str, bytes))
                                          for v in extra.values()):
        for bid, planes in extra.items():
            for plane in planes:
                if isinstance(plane, Mapping):
                    item = dict(plane)
                    item.setdefault("block_id", bid)
                else:
                    item = {"block_id": bid, "plane": plane}
                extras.append(item)
    elif extra is not None:
        extras = list(extra) if isinstance(extra, Sequence) and not isinstance(extra, (str, bytes)) else [extra]
    for fraw in extras:
        if isinstance(fraw, str):
            fraw = {"plane": fraw}
        bid = fraw.get("block_id") or fraw.get("block")
        plane_value = fraw.get("plane", fraw)
        if bid:
            size = next(b["size"] for b in blocks if b["block_id"] == bid)
            p = normalize_plane(plane_value, size)
            p.setdefault("id", str(fraw.get("id") or fraw.get("fixture_id") or p.get("id") or f"plane-{p['axis']}{p['at']}"))
            p["block_id"] = bid
            if fraw.get("piece_id"):
                p["piece_id"] = fraw["piece_id"]
            out.setdefault(bid, []).append(p)
        else:
            for b in blocks:
                p = normalize_plane(plane_value, b["size"])
                p.setdefault("id", str(fraw.get("id") or p.get("id") or f"plane-{p['axis']}{p['at']}"))
                p["block_id"] = b["block_id"]
                out[b["block_id"]].append(p)
    # De-duplicate by axis/at.
    for bid, planes in out.items():
        seen = set()
        uniq = []
        for p in planes:
            k = (p["axis_index"], p["at"])
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        out[bid] = uniq
    return out


def reconstruct_pieces(blocks: Sequence[Mapping[str, Any]],
                       cut_records: Optional[Sequence[Mapping[str, Any]]]
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    block_by_id = {b["block_id"]: b for b in blocks}
    pieces: Dict[str, Dict[str, Any]] = {
        b["block_id"]: initial_piece(b) for b in blocks
    }
    history: List[Dict[str, Any]] = []
    if cut_records is None:
        cut_records = []
    if isinstance(cut_records, Mapping):
        cut_records = [cut_records]
    records = sorted(
        list(cut_records),
        key=lambda c: c.get("seq", c.get("cut_seq", 0)) if isinstance(c, Mapping) else 0,
    )
    for index, raw_cut in enumerate(records):
        cut = dict(raw_cut)
        cut_id = str(cut.get("cut_id") or cut.get("id") or f"cut-{index + 1}")
        bid = str(cut.get("block_id") or cut.get("source_block_id") or cut.get("block") or "")
        target_id = cut.get("piece_id") or cut.get("target_piece_id")
        plane_raw = (cut.get("plane") or cut.get("cut_plane")
                     or cut.get("plane_spec") or cut.get("切面") or cut)
        if not bid and target_id and target_id in pieces:
            bid = pieces[target_id]["source_block_id"]
        if bid not in block_by_id:
            raise CheeseInputError(f"cut {cut_id!r} references unknown block {bid!r}")
        plane = normalize_plane(plane_raw, block_by_id[bid]["size"])
        plane["block_id"] = bid

        if target_id is not None:
            if target_id not in pieces:
                raise CheeseInputError(
                    f"cut {cut_id!r} targets missing/split piece {target_id!r}; "
                    "already sawn pieces cannot be rearranged into an uncut block"
                )
            target = pieces[target_id]
        else:
            candidates = [p for p in pieces.values()
                          if p["source_block_id"] == bid and plane_applies(p, plane)]
            if len(candidates) != 1:
                raise CheeseInputError(
                    f"cut {cut_id!r} is ambiguous: {len(candidates)} current pieces match"
                )
            target = candidates[0]

        child_ids = None
        if cut.get("child_ids"):
            child_ids = tuple(cut["child_ids"])  # type: ignore
        elif cut.get("negative_id") or cut.get("positive_id"):
            child_ids = (
                cut.get("negative_id") or f"{target['piece_id']}.minus",
                cut.get("positive_id") or f"{target['piece_id']}.plus",
            )
        elif plane["axis_index"] == 0:
            child_ids = (f"{target['piece_id']}.left", f"{target['piece_id']}.right")
        elif plane["axis_index"] == 2:
            child_ids = (f"{target['piece_id']}.bottom", f"{target['piece_id']}.top")
        a, b = split_piece(target, plane, cut_id, child_ids)
        # Preserve caller-friendly half aliases if supplied.
        if cut.get("left_id") and plane["axis_index"] == 0:
            a["piece_id"] = cut["left_id"]
        if cut.get("right_id") and plane["axis_index"] == 0:
            b["piece_id"] = cut["right_id"]
        pieces.pop(target["piece_id"], None)
        pieces[a["piece_id"]] = a
        pieces[b["piece_id"]] = b
        item = {
            "cut_id": cut_id,
            "source_block_id": bid,
            "target_piece_id": target["piece_id"],
            "plane": {k: v for k, v in plane.items() if k != "axis_index"} | {"axis_index": plane["axis_index"]},
            "negative_piece_id": a["piece_id"],
            "positive_piece_id": b["piece_id"],
            "source_version": cut.get("source_version"),
            "signed_label": cut.get("signed_label"),
            "note": cut.get("note"),
        }
        history.append(item)
    return pieces, history


# ---------------------------------------------------------------------------
# Fixed/non-adaptive planning
# ---------------------------------------------------------------------------

def _cut_options(piece: Mapping[str, Any], planes: Sequence[Mapping[str, Any]],
                 allow_any: bool) -> List[Dict[str, Any]]:
    options = [p for p in planes if plane_applies(piece, p)]
    if options or not allow_any:
        return options
    options = []
    for axis in range(3):
        n = piece_size(piece)[axis]
        lo = int(piece["bounds"][axis][0])
        for offset in range(1, n):
            at = lo + offset
            options.append({
                "id": f"any-{['x', 'y', 'z'][axis]}{at}",
                "axis": ["x", "y", "z"][axis],
                "axis_index": axis,
                "at": at,
            })
    return options


def solve_requirements(
    pieces: Mapping[str, Dict[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    models_by_block: Mapping[str, Sequence[Mapping[str, Any]]],
    fixtures: Mapping[str, Sequence[Mapping[str, Any]]],
    budget: int,
    allow_any_cut: bool = False,
) -> Optional[Dict[str, Any]]:
    reqs = tuple(sorted(requirements, key=lambda r: r["key"]))
    fit_cache: Dict[Tuple[str, Tuple, str], Dict[str, Any]] = {}
    yield_cache: Dict[Tuple[str, Tuple, str], bool] = {}

    def fit(req: Mapping[str, Any], piece: Mapping[str, Any]) -> Dict[str, Any]:
        bounds_key = tuple((a, b) for a, b in piece["bounds"])
        ck = (req["key"], bounds_key, piece["source_block_id"])
        if ck not in fit_cache:
            models = models_by_block.get(piece["source_block_id"], [])
            fit_cache[ck] = universal_fit(piece, req, models)
        return fit_cache[ck]

    def can_yield(req: Mapping[str, Any], piece: Mapping[str, Any]) -> bool:
        """Whether any rectangular subregion of this piece can be safe."""
        bounds_key = tuple(tuple(bd) for bd in piece["bounds"])
        ck = (req["key"], bounds_key, piece["source_block_id"])
        if ck in yield_cache:
            return yield_cache[ck]
        if fit(req, piece)["safe"]:
            yield_cache[ck] = True
            return True
        models = models_by_block.get(piece["source_block_id"], [])
        if not models:
            yield_cache[ck] = False
            return False
        wanted = shape_dimensions(req)
        actual = piece_size(piece)
        dim_choices = {wanted}
        if req.get("allow_rotation", True):
            dim_choices = set(itertools.permutations(wanted))  # type: ignore[arg-type]
        source_size = piece.get("source_block_size")
        ok = False
        for dims in dim_choices:
            if any(d > a for d, a in zip(dims, actual)):
                continue
            starts = []
            for axis in range(3):
                lo = int(piece["bounds"][axis][0])
                hi = int(piece["bounds"][axis][1])
                starts.append(range(lo, hi - int(dims[axis]) + 1))
            for x0, y0, z0 in itertools.product(*starts):
                start = (x0, y0, z0)
                region_bounds = [
                    [start[a], start[a] + int(dims[a])] for a in range(3)
                ]
                candidate = dict(piece)
                candidate["bounds"] = region_bounds
                candidate["size"] = list(dims)
                if source_size:
                    candidate["original_faces"] = original_faces(source_size, region_bounds)
                if universal_fit(candidate, req, models)["safe"]:
                    ok = True
                    break
            if ok:
                break
        yield_cache[ck] = ok
        return ok

    bad: set = set()

    def dfs(piece_map: Dict[str, Dict[str, Any]], remaining: Tuple[str, ...], cuts_left: int
            ) -> Optional[Tuple[Tuple[Dict[str, Any], ...], Tuple[Dict[str, Any], ...],
                                Dict[str, Dict[str, Any]]]]:
        piece_ids = tuple(sorted(piece_map))
        if not remaining:
            return ((), (), piece_map)
        geometry_state = tuple(sorted(
            (pid, tuple(tuple(bd) for bd in piece_map[pid]["bounds"]))
            for pid in piece_ids
        ))
        state = (geometry_state, remaining, cuts_left)
        if state in bad:
            return None
        if len(remaining) > len(piece_ids) + cuts_left:
            bad.add(state)
            return None

        req_by_key = {r["key"]: r for r in reqs}
        # MRV: requirement with fewest currently safe whole pieces.
        selected_key = None
        safe_here: List[str] = []
        best_count = 10**9
        for key in remaining:
            req = req_by_key[key]
            safe = [pid for pid in piece_ids if fit(req, piece_map[pid])["safe"]]
            if len(safe) < best_count:
                best_count = len(safe)
                selected_key = key
                safe_here = safe
                if best_count == 0:
                    break
        assert selected_key is not None
        selected = req_by_key[selected_key]

        # 1) Use an already available safe piece.
        for pid in sorted(safe_here, key=lambda x: (piece_size(piece_map[x]), x)):
            remaining_map = {k: v for k, v in piece_map.items() if k != pid}
            sub = dfs(remaining_map,
                      tuple(k for k in remaining if k != selected_key), cuts_left)
            if sub is not None:
                assignments = ({"requirement_key": selected_key, "box_id": selected["box_id"],
                                "strip_id": selected["strip_id"], "piece_id": pid,
                                "fixed": True},) + sub[1]
                final_map = {pid: piece_map[pid]}
                final_map.update(sub[2])
                return (sub[0], assignments, final_map)

        # 2) Make one permitted cut, then continue.
        if cuts_left > 0:
            remaining_reqs = [req_by_key[k] for k in remaining]
            for pid in piece_ids:
                piece = piece_map[pid]
                if not any(can_yield(r, piece) for r in remaining_reqs):
                    continue
                planes = fixtures.get(piece["source_block_id"], [])
                for plane in _cut_options(piece, planes, allow_any_cut):
                    cut_id = str(plane.get("id") or f"{plane['axis']}{plane['at']}")
                    try:
                        a, b = split_piece(piece, plane, cut_id)
                    except CheeseInputError:
                        continue
                    if not any(can_yield(r, a) or can_yield(r, b)
                               for r in remaining_reqs):
                        continue
                    child_map = dict(piece_map)
                    child_map.pop(pid, None)
                    child_map[a["piece_id"]] = a
                    child_map[b["piece_id"]] = b
                    sub = dfs(child_map, remaining, cuts_left - 1)
                    if sub is not None:
                        action = {
                            "cut_id": cut_id,
                            "source_block_id": piece["source_block_id"],
                            "target_piece_id": pid,
                            "plane": {k: v for k, v in plane.items()},
                            "negative_piece_id": a["piece_id"],
                            "positive_piece_id": b["piece_id"],
                        }
                        return ((action,) + sub[0], sub[1], sub[2])
        bad.add(state)
        return None

    result = dfs(dict(pieces), tuple(r["key"] for r in reqs), int(budget))
    if result is None:
        return None
    cuts, assignments, final_pieces = result
    fit_reports = {}
    assn_by_req = {a["requirement_key"]: a["piece_id"] for a in assignments}
    for req in reqs:
        pid = assn_by_req[req["key"]]
        fit_reports[req["key"]] = fit(req, final_pieces[pid])
    return {
        "feasible": True,
        "cuts": list(cuts),
        "assignments": list(assignments),
        "fit_reports": fit_reports,
        "pieces_after": list(final_pieces.values()),
        "cuts_used": len(cuts),
    }


def _box_subset_plan(
    boxes: Sequence[Mapping[str, Any]],
    pieces: Mapping[str, Dict[str, Any]],
    models_by_block: Mapping[str, Sequence[Mapping[str, Any]]],
    fixtures: Mapping[str, Sequence[Mapping[str, Any]]],
    budget: int,
    allow_any_cut: bool,
    target_ids: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    target_set = set(target_ids) if target_ids is not None else {b["box_id"] for b in boxes}
    reqs = [s for b in boxes if b["box_id"] in target_set for s in b["strips"]]
    solved = solve_requirements(pieces, reqs, models_by_block, fixtures, budget, allow_any_cut)
    if solved is None:
        return None
    return {
        "promised_box_ids": [b["box_id"] for b in boxes if b["box_id"] in target_set],
        "requirements": reqs,
        **solved,
    }


def _apply_used_pieces(piece_map: Dict[str, Dict[str, Any]],
                       existing_assignments: Any = None,
                       used_piece_ids: Optional[Sequence[str]] = None
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    used = set(used_piece_ids or [])
    records = []
    for item in existing_assignments or []:
        if isinstance(item, str):
            used.add(item)
            records.append({"piece_id": item})
        elif isinstance(item, Mapping):
            pid = item.get("piece_id") or item.get("named_piece_id")
            if pid:
                used.add(pid)
                records.append(dict(item))
    for pid in used:
        piece_map.pop(pid, None)
    return piece_map, records


def plan_sealed(qc_report: Mapping[str, Any],
                boxes: Any = None,
                existing_cuts: Optional[Sequence[Mapping[str, Any]]] = None,
                fixtures: Any = None,
                cut_budget: Optional[int] = None,
                station_beats: Optional[int] = None,
                allow_any_cut: bool = False,
                target_box_ids: Optional[Sequence[str]] = None,
                blocks: Optional[Sequence[Mapping[str, Any]]] = None,
                existing_assignments: Any = None,
                used_piece_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    qc = _as_qc(qc_report)
    block_defs = _blocks_from(qc, blocks)
    models = _models_by_block(qc, block_defs)
    box_defs = normalize_boxes(boxes if boxes is not None else qc.get("case", {}).get("boxes"))
    piece_map, cut_history = reconstruct_pieces(block_defs, existing_cuts)
    piece_map, prior_assignments = _apply_used_pieces(
        piece_map, existing_assignments, used_piece_ids)
    fixture_map = _fixtures_by_block(block_defs, fixtures if fixtures is not None else qc.get("normalized_case", {}).get("fixtures"))

    if cut_budget is None:
        cut_budget = station_beats
    if cut_budget is None:
        cut_budget = 16
    cut_budget = int(cut_budget)

    # Find maximum number of complete boxes, preserving input priority on ties.
    chosen: Optional[Dict[str, Any]] = None
    chosen_ids: List[str] = []
    if target_box_ids is not None:
        combos = [tuple(target_box_ids)]
    else:
        all_ids = [b["box_id"] for b in box_defs]
        combos = []
        # Order by descending box count, then input priority within a count.
        for k in range(len(all_ids), 0, -1):
            combos.extend(itertools.combinations(all_ids, k))
    for combo in combos:
        candidate = _box_subset_plan(
            box_defs, piece_map, models, fixture_map, cut_budget,
            allow_any_cut, target_ids=list(combo),
        )
        if candidate is not None:
            chosen = candidate
            chosen_ids = list(combo)
            break

    if chosen is None:
        chosen = {
            "feasible": False,
            "promised_box_ids": [],
            "cuts": [],
            "assignments": [],
            "fit_reports": {},
            "pieces_after": list(piece_map.values()),
            "cuts_used": 0,
        }
    labels = []
    for a in chosen.get("assignments", []):
        labels.append({
            "type": "sealed_strip_label",
            "box_id": a["box_id"],
            "strip_id": a["strip_id"],
            "named_piece_id": a["piece_id"],
            "substitution_allowed": False,
            "cut_line_ids": [c["cut_id"] for c in chosen.get("cuts", [])],
        })
    return {
        "mode": "sealed",
        "table_used": False,
        "feasible": chosen["feasible"],
        "can_promise": chosen["feasible"],
        "status": "feasible" if chosen["feasible"] else "no_feasible_box",
        "complete_box_count": len(chosen["promised_box_ids"]),
        "promised_box_ids": chosen["promised_box_ids"],
        "first_cut": chosen.get("cuts", [None])[0] if chosen.get("cuts") else None,
        "cuts": chosen.get("cuts", []),
        "assignments": chosen.get("assignments", []),
        "plan": chosen,
        "labels": labels,
        "existing_cut_history": cut_history,
        "pieces_before_new_cuts": list(piece_map.values()),
        "cut_budget": cut_budget,
        "station_beats": station_beats,
        "qc_status": qc.get("status"),
        "possible_model_ids_by_block": qc.get("possible_model_ids_by_block", {}),
    }


# ---------------------------------------------------------------------------
# Inspection-table/adaptive planning
# ---------------------------------------------------------------------------

def cut_observation(models: Sequence[Mapping[str, Any]], piece: Mapping[str, Any],
                    plane: Mapping[str, Any]) -> Dict[str, Any]:
    axis, at = int(plane["axis_index"]), int(plane["at"])
    transverse = [a for a in range(3) if a != axis]

    def cells(layer: int, side: str):
        out = []
        ranges = []
        for a in transverse:
            lo, hi = piece["bounds"][a]
            ranges.append(range(int(lo), int(hi)))
        for c0 in ranges[0]:
            for c1 in ranges[1]:
                coord = [0, 0, 0]
                coord[axis] = layer
                coord[transverse[0]] = c0
                coord[transverse[1]] = c1
                out.append(tuple(coord))
        return [{"side": side, "coord": list(c)} for c in out]

    face_cells = cells(at - 1, "negative_half_fresh_face") + cells(at, "positive_half_fresh_face")
    model_observations = {}
    groups: Dict[str, List[str]] = {}
    for model in models:
        holes = {tuple(h) for h in model.get("holes", [])}
        pattern = tuple(1 if tuple(c["coord"]) in holes else 0 for c in face_cells)
        key = "obs-" + "".join(str(x) for x in pattern)
        model_observations[model["model_id"]] = {
            "key": key,
            "visible_hole_pattern": list(pattern),
            "visible_holes": [list(c["coord"]) for c in face_cells if tuple(c["coord"]) in holes],
        }
        groups.setdefault(key, []).append(model["model_id"])
    return {
        "plane": {k: v for k, v in plane.items()},
        "cells": face_cells,
        "model_observations": model_observations,
        "groups": groups,
    }


def plan_table(qc_report: Mapping[str, Any],
               boxes: Any = None,
               existing_cuts: Optional[Sequence[Mapping[str, Any]]] = None,
               fixtures: Any = None,
               station_beats: Optional[int] = None,
               table_cost: int = 1,
               cut_budget: Optional[int] = None,
               allow_any_cut: bool = False,
               target_box_ids: Optional[Sequence[str]] = None,
               blocks: Optional[Sequence[Mapping[str, Any]]] = None,
               existing_assignments: Any = None,
               used_piece_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    qc = _as_qc(qc_report)
    block_defs = _blocks_from(qc, blocks)
    all_models = _models_by_block(qc, block_defs)
    box_defs = normalize_boxes(boxes if boxes is not None else qc.get("case", {}).get("boxes"))
    piece_map, cut_history = reconstruct_pieces(block_defs, existing_cuts)
    piece_map, prior_assignments = _apply_used_pieces(
        piece_map, existing_assignments, used_piece_ids)
    fixture_map = _fixtures_by_block(block_defs, fixtures if fixtures is not None else qc.get("normalized_case", {}).get("fixtures"))

    # station_beats counts cut beats plus the one beat occupied by the table.
    if station_beats is None:
        station_beats = qc.get("normalized_case", {}).get("station_beats")
    if cut_budget is not None:
        total_cut_budget = int(cut_budget)
        station_beats = total_cut_budget + int(table_cost) if station_beats is None else station_beats
    if station_beats is None:
        station_beats = 3
    station_beats = int(station_beats)
    first_and_downstream_budget = station_beats - int(table_cost)
    if first_and_downstream_budget < 1:
        return {
            "mode": "table", "table_used": True, "feasible": False,
            "status": "no_workstation_beat_for_cut_or_table",
            "complete_box_count": 0, "promised_box_ids": [], "branches": [],
            "station_beats": station_beats, "table_cost": table_cost,
            "existing_cut_history": cut_history,
            "pieces_before_new_cuts": list(piece_map.values()),
        }

    all_ids = [b["box_id"] for b in box_defs]
    target_combos: List[Tuple[str, ...]]
    if target_box_ids is not None:
        target_combos = [tuple(target_box_ids)]
    else:
        target_combos = []
        for k in range(len(all_ids), 0, -1):
            target_combos.extend(itertools.combinations(all_ids, k))
            # We still need test combos at this k; stop after this size if any.
            # The loop below returns first feasible; but cannot know here.
            if target_combos:
                # Only combinations of current maximum remaining size first.
                pass

    # Enumerate first-cut choices in deterministic block/plane order.
    first_cuts = []
    for pid in sorted(piece_map):
        piece = piece_map[pid]
        for plane in _cut_options(piece, fixture_map.get(piece["source_block_id"], []), allow_any_cut):
            first_cuts.append((pid, plane))

    best = None
    # Preserve maximum-box objective by trying larger subsets first.
    ordered_combos = []
    if target_box_ids is not None:
        ordered_combos = [tuple(target_box_ids)]
    else:
        for k in range(len(all_ids), 0, -1):
            ordered_combos.extend(itertools.combinations(all_ids, k))

    for combo in ordered_combos:
        reqs = [s for b in box_defs if b["box_id"] in combo for s in b["strips"]]
        for pid, plane in first_cuts:
            piece = piece_map[pid]
            source = piece["source_block_id"]
            source_models = all_models.get(source, [])
            observation = cut_observation(source_models, piece, plane)
            try:
                a, bpiece = split_piece(piece, plane, "inspection")
            except CheeseInputError:
                continue
            after_cut = dict(piece_map)
            after_cut.pop(pid, None)
            after_cut[a["piece_id"]] = a
            after_cut[bpiece["piece_id"]] = bpiece
            downstream_budget = first_and_downstream_budget - 1
            if downstream_budget < 0:
                continue

            branches = []
            all_feasible = True
            for obs_key, model_ids in sorted(observation["groups"].items()):
                conditioned = dict(all_models)
                conditioned[source] = [m for m in source_models if m["model_id"] in set(model_ids)]
                solved = solve_requirements(
                    after_cut, reqs, conditioned, fixture_map,
                    downstream_budget, allow_any_cut,
                )
                if solved is None:
                    all_feasible = False
                    break
                branches.append({
                    "observation_key": obs_key,
                    "model_ids": model_ids,
                    "visible_holes": observation["model_observations"],
                    "plan": {
                        "promised_box_ids": list(combo),
                        **solved,
                    },
                })
            if all_feasible:
                best = {
                    "promised_box_ids": list(combo),
                    "complete_box_count": len(combo),
                    "inspection_cut": {
                        "source_block_id": source,
                        "target_piece_id": pid,
                        "plane": plane,
                        "negative_piece_id": a["piece_id"],
                        "positive_piece_id": bpiece["piece_id"],
                        "block_order_choice": source,
                    },
                    "observation": observation,
                    "branches": branches,
                    "station_beats": station_beats,
                    "table_cost": table_cost,
                    "cut_beats_available": first_and_downstream_budget,
                }
                break
        if best is not None:
            break

    if best is None:
        return {
            "mode": "table", "table_used": True, "feasible": False,
            "status": "no_guaranteed_adaptive_plan",
            "complete_box_count": 0, "promised_box_ids": [], "branches": [],
            "station_beats": station_beats, "table_cost": table_cost,
            "existing_cut_history": cut_history,
            "pieces_before_new_cuts": list(piece_map.values()),
            "first_cut_options": [
                {"piece_id": pid, "plane": p} for pid, p in first_cuts
            ],
        }
    return {
        "mode": "table",
        "table_used": True,
        "feasible": True,
        "status": "feasible",
        **best,
        "existing_cut_history": cut_history,
        "pieces_before_new_cuts": list(piece_map.values()),
    }


def _looks_like_cut_records(value: Any) -> bool:
    if isinstance(value, Mapping):
        return ("plane" in value or "cut_id" in value or "target_piece_id" in value
                or ("axis" in value and "at" in value))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        return False
    first = value[0]
    return isinstance(first, Mapping) and (
        "plane" in first or "cut_id" in first or "target_piece_id" in first
        or ("axis" in first and "at" in first)
    )


def _looks_like_boxes(value: Any) -> bool:
    if isinstance(value, Mapping):
        return ("strips" in value or "required" in value
                or "requirements" in value or "shape" in value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        return False
    first = value[0]
    return isinstance(first, Mapping) and (
        "strips" in first or "required" in first or "requirements" in first
        or "box_id" in first and "shape" in first
    )


def plan_order(qc_report: Any = None,
               boxes: Any = None,
               existing_cuts: Optional[Sequence[Mapping[str, Any]]] = None,
               cuts: Optional[Sequence[Mapping[str, Any]]] = None,
               mode: Optional[str] = None,
               delivery: Optional[str] = None,
               station_beats: Optional[int] = None,
               beats: Optional[int] = None,
               cut_budget: Optional[int] = None,
               fixtures: Any = None,
               use_table: Optional[bool] = None,
               allow_any_cut: bool = False,
               target_box_ids: Optional[Sequence[str]] = None,
               blocks: Optional[Sequence[Mapping[str, Any]]] = None,
               existing_assignments: Any = None,
               used_piece_ids: Optional[Sequence[str]] = None,
               **kwargs) -> Dict[str, Any]:
    """Plan an order using either sealed labels or the inspection table."""
    # Tolerate the intuitive positional order plan_order(qc, cuts, boxes).
    if _looks_like_cut_records(boxes) and existing_cuts is None and cuts is None:
        cuts = boxes
        boxes = None
    if _looks_like_boxes(existing_cuts) and boxes is None:
        boxes = existing_cuts
        existing_cuts = None
    elif _looks_like_boxes(existing_cuts) and _looks_like_cut_records(boxes):
        # qc, cuts, boxes was used: boxes currently holds cuts and
        # existing_cuts holds the box list.
        cuts, boxes = boxes, existing_cuts
        existing_cuts = None

    if isinstance(qc_report, Mapping) and boxes is None:
        boxes = qc_report.get("boxes") or qc_report.get("display_boxes")
    mode = mode or delivery
    if mode is None and isinstance(qc_report, Mapping):
        mode = qc_report.get("mode") or qc_report.get("delivery")
    if use_table is True:
        mode = "table"
    if use_table is False and mode is None:
        mode = "sealed"
    mode = str(mode or "sealed").lower().replace("-", "_")
    if station_beats is None:
        station_beats = (beats or kwargs.get("workstation_beats")
                         or kwargs.get("work_beats") or kwargs.get("stations")
                         or kwargs.get("station_slots") or kwargs.get("拍"))
    if cut_budget is None:
        cut_budget = (kwargs.get("max_cuts") or kwargs.get("cut_limit")
                      or kwargs.get("max_new_cuts") or kwargs.get("budget")
                      or kwargs.get("cut_slots"))
    if cut_budget is not None:
        cut_budget = int(cut_budget)
    cut_records = existing_cuts if existing_cuts is not None else cuts
    if cut_records is None:
        cut_records = kwargs.get("real_cut_records") or kwargs.get("cut_records")

    common = dict(
        qc_report=qc_report, boxes=boxes, existing_cuts=cut_records,
        fixtures=fixtures, allow_any_cut=allow_any_cut,
        target_box_ids=target_box_ids, blocks=blocks,
        existing_assignments=existing_assignments,
        used_piece_ids=used_piece_ids,
    )
    table_names = {"table", "face_table", "inspection_table", "adaptive",
                   "照面台", "租照面台", "table_delivery", "inspect_and_assign"}
    sealed_names = {"sealed", "labels", "fixed", "no_table", "ordinary",
                    "普通单", "sealed_labels", "sealed_label", "原签",
                    "固定签", "封签", "label"}
    auto_names = {"auto", "best", "choose", "selected", "自动", "自选"}

    if mode in auto_names:
        sealed_budget = cut_budget if cut_budget is not None else station_beats
        sealed = plan_sealed(cut_budget=sealed_budget, **common)
        if sealed.get("feasible"):
            sealed["selected_delivery"] = "sealed"
            sealed["delivery_reason"] = "fixed labels already guarantee the maximum box set"
            return sealed
        table = plan_table(station_beats=station_beats, cut_budget=cut_budget, **common)
        if table.get("feasible"):
            table["selected_delivery"] = "table"
            table["delivery_reason"] = "observation is required before naming the safe half"
            table["sealed_attempt_status"] = sealed.get("status")
            return table
        sealed["alternative_table_status"] = table.get("status")
        sealed["selected_delivery"] = "sealed"
        return sealed

    if mode in table_names:
        return plan_table(station_beats=station_beats, cut_budget=cut_budget, **common)
    if mode in sealed_names:
        return plan_sealed(station_beats=station_beats, cut_budget=cut_budget, **common)
    raise CheeseInputError(f"unknown delivery mode {mode!r}")


# Friendly aliases.
plan_slices = plan_order
plan_cuts = plan_order
slice_plan = plan_order
plan_cutting = plan_order
cutting_plan = plan_order
solve_boxes = plan_order
plan_for_boxes = plan_order


def decide_remaining_boxes(qc_report: Any,
                           cut_records: Optional[Sequence[Mapping[str, Any]]] = None,
                           boxes: Any = None, **kwargs) -> Dict[str, Any]:
    """QC first, existing real cuts second, box requirements third."""
    return plan_order(qc_report, boxes=boxes, existing_cuts=cut_records, **kwargs)


def plan_remaining(qc_report: Any, cut_records: Optional[Sequence[Mapping[str, Any]]] = None,
                   boxes: Any = None, **kwargs) -> Dict[str, Any]:
    return decide_remaining_boxes(qc_report, cut_records, boxes, **kwargs)


def qc_and_plan(case: Any, **kwargs) -> Dict[str, Any]:
    """Convenience entry: quality-check a case/sample and plan in one call."""
    from .qc import quality_check
    qc_report = quality_check(case)
    result = plan_order(qc_report, **kwargs)
    result["qc"] = {
        "status": qc_report["status"],
        "possible_model_ids_by_block": qc_report["possible_model_ids_by_block"],
        "conflicts": qc_report["conflicts"],
    }
    return result
