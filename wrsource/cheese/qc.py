"""Quality-check filtering for weighted probe samples and candidate models."""

from __future__ import annotations

import itertools
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .geometry import (CheeseInputError, FACE_TABLE, normalize_block,
                       normalize_face, normalize_plane, normalize_size)

Matrix = Tuple[Tuple[int, int, int], ...]


def _rotation_matrices() -> List[Tuple[str, Matrix]]:
    out: List[Tuple[str, Matrix]] = []
    for perm in itertools.permutations(range(3)):
        perm_sign = 1
        for i in range(3):
            for j in range(i + 1, 3):
                if perm[i] > perm[j]:
                    perm_sign *= -1
        for signs in itertools.product((-1, 1), repeat=3):
            sign_product = signs[0] * signs[1] * signs[2]
            # Determinant must be +1: a physical rotation, not a reflection.
            if perm_sign * sign_product != 1:
                continue
            m = [[0, 0, 0] for _ in range(3)]
            for row in range(3):
                col = perm[row]
                m[row][col] = signs[row]
            mid = "m" + "".join(str(x) for row in m for x in row)
            out.append((mid, tuple(tuple(row) for row in m)))
    # Stable order with identity first.
    out.sort(key=lambda item: (item[1] != ((1, 0, 0), (0, 1, 0), (0, 0, 1)), item[0]))
    return out


ROTATIONS: List[Tuple[str, Matrix]] = _rotation_matrices()
ROTATION_BY_ID = {oid: matrix for oid, matrix in ROTATIONS}


def orientation_matrix(value: Any) -> Tuple[str, Matrix]:
    if value is None:
        return "identity", ROTATION_BY_ID["m100010001"]
    if isinstance(value, Mapping):
        value = value.get("matrix", value.get("orientation", "identity"))
    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "id": "identity", "as_is": "identity", "original": "identity",
            "none": "identity", "0": "identity", "north": "identity",
            "flip": "turn_x", "flipped": "turn_x", "turn": "turn_x",
            "rotate_180": "turn_x", "rot180": "turn_x",
            "flip_x": "turn_x", "reverse_x": "turn_x", "left_right": "turn_x",
            "rotate_180_y": "turn_x", "turn_x": "turn_x", "x_reverse": "turn_x",
        }
        key = aliases.get(key, key)
        if key == "identity":
            return "identity", ROTATION_BY_ID["m100010001"]
        if key in ("turn_x", "flip_left_right", "reverse_left_right"):
            m = ((-1, 0, 0), (0, 1, 0), (0, 0, -1))
            return "turn_x", m
        if key in ROTATION_BY_ID:
            return key, ROTATION_BY_ID[key]
        # Accept e.g. "-100010001".
        if key.startswith("m") and key[1:].isdigit() and len(key) == 10:
            mid = key
            return mid, ROTATION_BY_ID[mid]
        raise CheeseInputError(f"unknown mounting orientation {value!r}")
    if isinstance(value, int):
        oid, matrix = ROTATIONS[int(value)]
        return oid, matrix
    if isinstance(value, Sequence):
        rows = tuple(tuple(int(x) for x in row) for row in value)  # type: ignore
        if len(rows) != 3 or any(len(r) != 3 for r in rows):
            raise CheeseInputError("orientation matrix must be 3x3")
        mid = "m" + "".join(str(x) for row in rows for x in row)
        return mid, rows  # type: ignore[return-value]
    raise CheeseInputError(f"invalid orientation {value!r}")


def normalize_probe(raw: Mapping[str, Any]) -> Dict[str, Any]:
    pid = str(raw.get("probe_id") or raw.get("record_id") or raw.get("id")
              or raw.get("name") or raw.get("记录号") or raw.get("编号"))
    block_id = str(raw.get("block_id") or raw.get("block") or raw.get("named_block")
                   or raw.get("具名块") or raw.get("块名"))
    if not pid:
        raise CheeseInputError("probe record id is required")
    if not block_id:
        raise CheeseInputError(f"probe {pid!r} has no block_id")

    face_value = raw.get("entry_face", raw.get(
        "face", raw.get("entry", raw.get("入口面", "left"))))
    face = normalize_face(face_value)
    grid = raw.get("grid_point", raw.get("grid", raw.get(
        "point", raw.get("position", raw.get("格点", [0, 0])))))
    if isinstance(grid, Mapping):
        grid = [grid.get("y", grid.get("v", 0)), grid.get("z", grid.get("w", 0))]
    grid = [int(x) for x in grid]
    if len(grid) == 3:
        # A full coordinate is accepted; transverse extraction happens later.
        grid3 = grid
        grid = None
    else:
        if len(grid) != 2:
            raise CheeseInputError(f"probe {pid!r} grid_point must have two coordinates")
        grid3 = None

    direction = raw.get("direction", raw.get("dir"))
    if direction is not None:
        if isinstance(direction, str):
            dtext = direction.strip().lower().replace("_", "-").replace(" ", "-")
            if dtext in ("in", "inward", "into", "向内", "探入"):
                axis, outward_sign = FACE_TABLE[face]
                direction_vec = [0, 0, 0]
                direction_vec[axis] = -outward_sign
            else:
                dface = normalize_face(direction)
                axis, sign = FACE_TABLE[dface]
                direction_vec = [0, 0, 0]
                # A direction named "right" travels toward the right face,
                # i.e. along that face's outward normal sign.
                direction_vec[axis] = sign
        else:
            direction_vec = [int(x) for x in direction]
            if len(direction_vec) != 3:
                if len(direction_vec) == 1:
                    direction_vec = [direction_vec[0], 0, 0]
                else:
                    raise CheeseInputError("probe direction must be a 3-vector")
    else:
        axis, outward_sign = FACE_TABLE[face]
        direction_vec = [0, 0, 0]
        direction_vec[axis] = -outward_sign

    solid = bool(raw.get("solid", raw.get("全程实心", False)))
    has_depth_key = any(k in raw for k in (
        "first_hole_depth", "first_hole", "depth", "first_hole_cell",
        "首孔深度", "首孔格", "深度"))
    depth = raw.get(
        "first_hole_depth",
        raw.get("first_hole", raw.get(
            "depth", raw.get("first_hole_cell", raw.get(
                "首孔深度", raw.get("首孔格", raw.get("深度")))))))
    if isinstance(depth, Mapping):
        depth = depth.get("depth", depth.get("cell"))
    if raw.get("result") == "solid" or raw.get("outcome") == "solid":
        solid = True
        depth = None

    result_known = True
    if solid:
        expected_depth = None
    elif depth is not None:
        expected_depth = int(depth)
        if expected_depth <= 0:
            raise CheeseInputError(f"probe {pid!r} depth must be a positive cell ordinal")
    elif has_depth_key:
        # Explicit null means the full traverse was solid.
        expected_depth = None
        solid = True
    else:
        expected_depth = None
        result_known = False

    return {
        "probe_id": pid,
        "block_id": block_id,
        "entry_face": face,
        "grid_point": grid,
        "grid_coord": grid3,
        "direction": tuple(direction_vec),
        "expected_depth": expected_depth,
        "expected_solid": solid,
        "result_known": result_known,
        "rig_id": raw.get("rig_id") or raw.get("cradle_id") or raw.get("rack_id")
        or raw.get("托架") or raw.get("架") or raw.get("同架"),
        "orientation": raw.get("orientation", raw.get("mounting_orientation")),
        "source_row_id": raw.get("source_row_id") or raw.get("row_id"),
        "source_version": raw.get("source_version"),
        "raw": dict(raw),
    }


def normalize_case(data: Any, validate_limits: bool = True) -> Dict[str, Any]:
    if data is None:
        return {"blocks": [], "probes": [], "rigs": {}, "boxes": [], "fixtures": []}
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if "case" in data and isinstance(data["case"], Mapping) and "blocks" not in data:
        base = dict(data["case"])
        for k, v in data.items():
            if k != "case":
                base.setdefault(k, v)
        data = base

    raw_blocks = data.get("blocks", data.get("cheese_blocks", []))
    blocks = [normalize_block(b, validate_limits=validate_limits) for b in raw_blocks]
    if validate_limits and len(blocks) > 4:
        raise CheeseInputError(f"at most four physical blocks are supported, got {len(blocks)}")
    block_ids = {b["block_id"] for b in blocks}

    probes = [normalize_probe(p) for p in data.get("probes", data.get("records", data.get("probe_records", [])))]
    for p in probes:
        if p["block_id"] not in block_ids:
            raise CheeseInputError(
                f"probe {p['probe_id']!r} names unknown block {p['block_id']!r}"
            )

    rigs: Dict[str, Dict[str, Any]] = {}
    for raw_rig in data.get("rigs", data.get("cradles", [])) or []:
        if isinstance(raw_rig, str):
            rigs[raw_rig] = {"rig_id": raw_rig, "orientations": None}
            continue
        rid = str(raw_rig.get("rig_id") or raw_rig.get("id") or raw_rig.get("name"))
        orientations = raw_rig.get("orientations", raw_rig.get("allowed_orientations"))
        rigs[rid] = {"rig_id": rid, "orientations": orientations}
    for p in probes:
        if p["rig_id"] and p["rig_id"] not in rigs:
            rigs[p["rig_id"]] = {"rig_id": p["rig_id"], "orientations": None}

    boxes = data.get("boxes", data.get("display_boxes", data.get("展示盒", []))) or []
    fixtures = data.get("fixtures", data.get("allowed_planes", [])) or []
    return {
        "blocks": blocks,
        "probes": probes,
        "rigs": rigs,
        "boxes": boxes,
        "fixtures": fixtures,
        "station_beats": data.get("station_beats", data.get("beats")),
        "cut_budget": data.get("cut_budget"),
        "mode": data.get("mode", data.get("delivery")),
        "name": data.get("name", data.get("case_id")),
    }


def _nominal_anchor(probe: Mapping[str, Any], nom_size: Tuple[int, int, int],
                    matrix: Matrix) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    axis, outward_sign = FACE_TABLE[probe["entry_face"]]
    anchor = [0, 0, 0]
    anchor[axis] = 0 if outward_sign < 0 else nom_size[axis] - 1

    if probe.get("grid_coord") is not None:
        gc = probe["grid_coord"]
        # Full coordinate is already in nominal frame.
        anchor = [int(gc[0]), int(gc[1]), int(gc[2])]
    else:
        transverse = [a for a in range(3) if a != axis]
        gp = probe.get("grid_point") or [0, 0]
        anchor[transverse[0]] = int(gp[0])
        anchor[transverse[1]] = int(gp[1])

    direction = list(probe["direction"])
    return tuple(anchor), tuple(direction)  # type: ignore[return-value]


def transformed_ray(probe: Mapping[str, Any], size: Sequence[int],
                    orientation: Tuple[str, Matrix]) -> List[Tuple[int, int, int]]:
    _, matrix = orientation
    size = tuple(int(x) for x in size)
    # Nominal axis j maps through matrix[row=i][col=j].  Recover nominal size.
    nom_size = [1, 1, 1]
    for i in range(3):
        for j in range(3):
            if matrix[i][j] != 0:
                nom_size[j] = size[i]
    anchor_nom, direction_nom = _nominal_anchor(probe, nom_size, matrix)  # type: ignore[arg-type]

    anchor_local = [0, 0, 0]
    direction_local = [0, 0, 0]
    for i in range(3):
        for j in range(3):
            sign = matrix[i][j]
            if sign:
                if sign == 1:
                    anchor_local[i] = anchor_nom[j]
                else:
                    anchor_local[i] = nom_size[j] - 1 - anchor_nom[j]
                direction_local[i] = sign * direction_nom[j]

    ray = []
    cur = anchor_local
    while all(0 <= cur[i] < size[i] for i in range(3)):
        ray.append((cur[0], cur[1], cur[2]))
        nxt = [cur[i] + direction_local[i] for i in range(3)]
        if nxt == cur or any(not isinstance(v, int) for v in nxt):
            break
        cur = nxt
    return ray


def probe_matches_model(probe: Mapping[str, Any], block: Mapping[str, Any],
                        model: Mapping[str, Any],
                        orientation: Tuple[str, Matrix]) -> Dict[str, Any]:
    oid, _ = orientation
    ray = transformed_ray(probe, block["size"], orientation)
    if not ray:
        return {"ok": False, "orientation": oid, "actual_depth": None,
                "reason": "probe ray misses block"}
    holes = {tuple(h) for h in model.get("holes", [])}
    actual_depth = None
    hit = None
    for depth, coord in enumerate(ray, start=1):
        if coord in holes:
            actual_depth = depth
            hit = coord
            break
    if not probe.get("result_known", True):
        ok = True
        reason = "result-less record does not constrain"
    elif probe["expected_solid"]:
        ok = actual_depth is None
        reason = "" if ok else f"expected solid but hit {hit} at depth {actual_depth}"
    else:
        ok = actual_depth == probe["expected_depth"]
        reason = "" if ok else (
            f"expected first hole depth {probe['expected_depth']}, got {actual_depth}"
        )
    return {
        "ok": ok,
        "orientation": oid,
        "actual_depth": actual_depth,
        "hit": list(hit) if hit else None,
        "ray": [list(c) for c in ray],
        "reason": reason,
    }


def _context_orientations(case: Mapping[str, Any], probe: Mapping[str, Any]
                          ) -> List[Tuple[str, Matrix]]:
    if not probe.get("rig_id"):
        if probe.get("orientation") is not None:
            return [orientation_matrix(probe["orientation"])]
        return [orientation_matrix("identity")]
    rig = case["rigs"].get(probe["rig_id"], {})
    allowed = rig.get("orientations")
    if allowed is None:
        return ROTATIONS
    if isinstance(allowed, str):
        allowed = [allowed]
    return [orientation_matrix(o) for o in allowed]


def compatible_orientation_sets(case: Mapping[str, Any]) -> Dict[Tuple[str, str], set]:
    block_by_id = {b["block_id"]: b for b in case["blocks"]}
    sets: Dict[Tuple[str, str], set] = {}
    for probe in case["probes"]:
        block = block_by_id[probe["block_id"]]
        orientations = _context_orientations(case, probe)
        for model in block["models"]:
            good = set()
            for o in orientations:
                result = probe_matches_model(probe, block, model, o)
                if result["ok"]:
                    good.add(o[0])
            sets[(probe["probe_id"], model["model_id"])] = good
    return sets


def quality_check(data: Any, validate_limits: bool = True,
                  base_case: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return possible models per block and structured contradictions.

    The check enumerates a small constraint-satisfaction problem: one model per
    named block and one orientation per rig.  A model is reported possible if
    it belongs to at least one globally consistent assignment.
    """
    if isinstance(data, list) or (
        isinstance(data, Mapping) and (
            "sample_records" in data or "samples" in data or
            ("records" in data and data["records"] and
             isinstance(data["records"][0], Mapping) and "body" in data["records"][0])
        )
    ):
        from .sample_io import case_from_sample
        if isinstance(data, Mapping):
            records = data.get("sample_records", data.get("samples", data.get("records")))
            base = base_case or {k: v for k, v in data.items()
                                 if k not in ("sample_records", "samples", "records")}
        else:
            records = data
            base = base_case
        data = case_from_sample(records, base)
    case = normalize_case(data, validate_limits=validate_limits)
    blocks = case["blocks"]
    compat = compatible_orientation_sets(case)
    probes_by_block: Dict[str, List[Mapping[str, Any]]] = {b["block_id"]: [] for b in blocks}
    for p in case["probes"]:
        probes_by_block[p["block_id"]].append(p)

    def context_id(p: Mapping[str, Any]) -> str:
        return p["rig_id"] or f"single:{p['probe_id']}"

    def has_world(fixed: Optional[Dict[str, str]] = None) -> bool:
        fixed = dict(fixed or {})
        assignment: Dict[str, str] = {}
        context_sets: Dict[str, set] = {}

        def assign(block_id: str, model_id: str) -> bool:
            assignment[block_id] = model_id
            for p in probes_by_block[block_id]:
                cid = context_id(p)
                good = compat[(p["probe_id"], model_id)]
                if cid in context_sets:
                    context_sets[cid] = context_sets[cid] & good
                else:
                    allowed = {o[0] for o in _context_orientations(case, p)}
                    context_sets[cid] = allowed & good
                if not context_sets[cid]:
                    return False
            return True

        def undo(block_id: str, snapshot: Dict[str, set]) -> None:
            assignment.pop(block_id, None)
            context_sets.clear()
            context_sets.update(snapshot)

        # Fixed assignments first.
        snapshot0 = {k: set(v) for k, v in context_sets.items()}
        for bid, mid in fixed.items():
            if not assign(bid, mid):
                return False

        def dfs() -> bool:
            if len(assignment) == len(blocks):
                return True
            unassigned = [b for b in blocks if b["block_id"] not in assignment]
            # MRV-ish: test each block's currently viable model count.
            best = None
            best_options = None
            for block in unassigned:
                options = []
                for model in block["models"]:
                    mid = model["model_id"]
                    viable = True
                    for p in probes_by_block[block["block_id"]]:
                        cid = context_id(p)
                        good = compat[(p["probe_id"], mid)]
                        base = context_sets.get(cid)
                        if base is not None and not (base & good):
                            viable = False
                            break
                    if viable:
                        options.append(mid)
                if not options:
                    return False
                if best_options is None or len(options) < len(best_options):
                    best, best_options = block, options
            assert best is not None and best_options is not None
            for mid in best_options:
                snap = {k: set(v) for k, v in context_sets.items()}
                if assign(best["block_id"], mid) and dfs():
                    return True
                undo(best["block_id"], snap)
            return False

        return dfs()

    possible: Dict[str, List[str]] = {}
    possible_models: Dict[str, List[Dict[str, Any]]] = {}
    for block in blocks:
        ids = []
        for model in block["models"]:
            mid = model["model_id"]
            if has_world({block["block_id"]: mid}):
                ids.append(mid)
        possible[block["block_id"]] = ids
        possible_models[block["block_id"]] = [
            {k: v for k, v in m.items() if k != "hole_set"}
            for m in block["models"] if m["model_id"] in ids
        ]

    global_ok = has_world() if blocks else True
    conflicts = _diagnose_conflicts(case, compat, possible, global_ok)
    status = "ok" if global_ok and all(possible[b["block_id"]] for b in blocks) else "conflict"
    report = {
        "ok": status == "ok",
        "status": status,
        "valid": status == "ok",
        "globally_consistent": global_ok,
        "source_version": (data or {}).get("source_version") if isinstance(data, Mapping) else None,
        "sample_row_ids": (data or {}).get("sample_row_ids") if isinstance(data, Mapping) else [],
        "blocks": [
            {
                "block_id": b["block_id"],
                "size": b["size"],
                "status": "ok" if possible[b["block_id"]] else "conflict",
                "possible_model_ids": possible[b["block_id"]],
                "possible_models": possible_models[b["block_id"]],
                "conflicting_readings": [
                    p["probe_id"] for p in probes_by_block[b["block_id"]]
                ] if not possible[b["block_id"]] else [],
            }
            for b in blocks
        ],
        "possible_model_ids_by_block": possible,
        "possible_models_by_block": possible_models,
        "conflicts": conflicts,
        "rig_orientation_conflicts": [c for c in conflicts if c["type"] == "rig_orientation"],
        "case": {
            # JSON-safe normalized subset used by the slicer.
            "blocks": [
                {k: v for k, v in b.items() if k != "models"} | {
                    "models": possible_models[b["block_id"]]
                }
                for b in blocks
            ],
            "boxes": case["boxes"],
            "fixtures": case["fixtures"],
            "station_beats": case["station_beats"],
            "cut_budget": case["cut_budget"],
            "mode": case["mode"],
            "name": case["name"],
        },
        "normalized_case": _json_safe_case(case),
    }
    report.update({
        "compatible_models_by_block": possible,
        "surviving_models_by_block": possible,
        "valid_models_by_block": possible,
        "models_by_block": possible_models,
        "contradictions": conflicts,
    })
    return report


def _json_safe_case(case: Mapping[str, Any]) -> Dict[str, Any]:
    blocks = []
    for b in case["blocks"]:
        bb = dict(b)
        bb["models"] = [{k: v for k, v in m.items() if k != "hole_set"} for m in b["models"]]
        blocks.append(bb)
    probes = []
    for p in case["probes"]:
        pp = dict(p)
        pp.pop("raw", None)
        probes.append(pp)
    return {
        "blocks": blocks,
        "probes": probes,
        "rigs": case["rigs"],
        "boxes": case["boxes"],
        "fixtures": case["fixtures"],
        "station_beats": case["station_beats"],
        "cut_budget": case["cut_budget"],
        "mode": case["mode"],
        "name": case["name"],
    }


def _diagnose_conflicts(case: Mapping[str, Any], compat: Mapping[Tuple[str, str], set],
                        possible: Mapping[str, List[str]], global_ok: bool) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    block_by_id = {b["block_id"]: b for b in case["blocks"]}

    # Records which match no candidate model even when orientation is free.
    for p in case["probes"]:
        block = block_by_id[p["block_id"]]
        options = set()
        for m in block["models"]:
            options |= compat[(p["probe_id"], m["model_id"])]
        if not options:
            conflicts.append({
                "type": "record_model_conflict",
                "probe_id": p["probe_id"],
                "block_id": p["block_id"],
                "rig_id": p.get("rig_id"),
                "message": "reading is incompatible with every candidate model/orientation",
            })

    # Rig common-orientation diagnosis.
    rig_ids = sorted({p["rig_id"] for p in case["probes"] if p.get("rig_id")})
    for rid in rig_ids:
        records = [p for p in case["probes"] if p["rig_id"] == rid]
        individually = True
        per_record_options = {}
        for p in records:
            block = block_by_id[p["block_id"]]
            opts = set()
            for m in block["models"]:
                opts |= compat[(p["probe_id"], m["model_id"])]
            per_record_options[p["probe_id"]] = sorted(opts)
            if not opts:
                individually = False

        common = _rig_has_common_orientation(case, rid, compat)
        if not common:
            conflicts.append({
                "type": "rig_orientation",
                "rig_id": rid,
                "record_ids": [p["probe_id"] for p in records],
                "each_reading_can_pass": individually,
                "common_orientation_exists": False,
                "orientation_options": per_record_options,
                "message": (
                    "each reading can pass after remounting, but the records "
                    "cannot share one rig orientation"
                    if individually else
                    "rig has no orientation/model combination satisfying all records"
                ),
            })

    if not global_ok and not conflicts:
        conflicts.append({
            "type": "combined_model_orientation_conflict",
            "message": "no single assignment of block models and rig orientations satisfies the sample",
        })
    return conflicts


def _rig_has_common_orientation(case: Mapping[str, Any], rig_id: str,
                                compat: Mapping[Tuple[str, str], set]) -> bool:
    records = [p for p in case["probes"] if p["rig_id"] == rig_id]
    orientation_ids = {o[0] for o in _context_orientations(case, records[0])}
    block_choices: Dict[str, List[str]] = {}
    for p in records:
        block = next(b for b in case["blocks"] if b["block_id"] == p["block_id"])
        block_choices.setdefault(p["block_id"], [m["model_id"] for m in block["models"]])

    # Enumerate orientations first; for each, check whether some model of every
    # involved block satisfies all rig records targeting that block.
    for oid in orientation_ids:
        ok = True
        for bid, mids in block_choices.items():
            block_records = [p for p in records if p["block_id"] == bid]
            if not any(all(oid in compat[(p["probe_id"], mid)] for p in block_records)
                       for mid in mids):
                ok = False
                break
        if ok:
            return True
    return False


def explain_plane(qc_report: Mapping[str, Any], block_id: str,
                  plane: Any) -> Dict[str, Any]:
    """Explain whether a cut plane is guaranteed to show no holes."""
    block_info = next((b for b in qc_report["blocks"] if b["block_id"] == block_id), None)
    if block_info is None:
        raise CheeseInputError(f"unknown block {block_id!r}")
    size = block_info["size"]
    p = normalize_plane(plane, size)
    axis, at = p["axis_index"], p["at"]
    layers = [at - 1, at]
    explanations = []
    guaranteed = True
    for model in block_info.get("possible_models", []):
        witnesses = []
        for coord in model.get("holes", []):
            coord = tuple(coord)
            if coord[axis] in layers:
                witnesses.append(list(coord))
        if witnesses:
            guaranteed = False
        explanations.append({
            "model_id": model["model_id"],
            "visible_hole_voxels": witnesses,
            "plane_is_hole_free": not witnesses,
        })
    if not block_info.get("possible_models"):
        guaranteed = False
    return {
        "block_id": block_id,
        "plane": p,
        "adjacent_layers": layers,
        "necessarily_hole_free": guaranteed,
        "guaranteed": guaranteed,
        "models": explanations,
        "explanation": (
            "all surviving models have hole-free cut faces"
            if guaranteed else
            "at least one surviving model may show a hole on this cut face"
        ),
    }


def cuts_revealing_holes(qc_report: Mapping[str, Any], block_id: str,
                         planes: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """List permitted/grid cuts on which each surviving model shows a hole."""
    block_info = next((b for b in qc_report["blocks"] if b["block_id"] == block_id), None)
    if block_info is None:
        raise CheeseInputError(f"unknown block {block_id!r}")
    size = block_info["size"]
    if planes is None:
        raw_planes = []
        for axis, n in enumerate(size):
            for at in range(1, int(n)):
                raw_planes.append({"axis_index": axis, "axis": "xyz"[axis], "at": at})
    else:
        raw_planes = [normalize_plane(p, size) for p in planes]

    by_model = {}
    for model in block_info.get("possible_models", []):
        hits = []
        for plane in raw_planes:
            axis, at = int(plane["axis_index"]), int(plane["at"])
            witnesses = [list(c) for c in model.get("holes", [])
                         if tuple(c)[axis] in (at - 1, at)]
            if witnesses:
                hits.append({"plane": plane, "visible_hole_voxels": witnesses})
        by_model[model["model_id"]] = hits
    common = None
    for hits in by_model.values():
        keys = {(h["plane"]["axis"], h["plane"]["at"]) for h in hits}
        common = keys if common is None else common & keys
    return {
        "block_id": block_id,
        "by_model": by_model,
        "common_revealing_cuts": sorted([list(k) for k in (common or set())]),
    }


def check_block(models: Sequence[Mapping[str, Any]],
                probes: Optional[Sequence[Mapping[str, Any]]] = None,
                size: Any = (4, 1, 1), block_id: str = "B",
                boxes: Any = None, **kwargs) -> Dict[str, Any]:
    """Convenience API for one named block's models and probe records."""
    block = dict(kwargs.pop("block", {}))
    block.update({
        "block_id": block_id,
        "size": size,
        "candidate_models": list(models),
    })
    case = {"blocks": [block], "probes": list(probes or []), "boxes": boxes or []}
    case.update(kwargs)
    return quality_check(case)


# Common API aliases.
run_qc = quality_check
quality_check_models = quality_check
filter_models = quality_check
filter_compatible_models = quality_check
check_plane = explain_plane
plane_guaranteed_hole_free = explain_plane
