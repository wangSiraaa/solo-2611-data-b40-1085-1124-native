"""Grid geometry and normalized cheese models for the quality desk.

The module is intentionally JSON-friendly: public inputs may be dictionaries
and public results are dictionaries/lists/strings.  Coordinates are zero-based
voxel coordinates ``[x, y, z]``; cut plane ``at=k`` is the boundary between
voxel layers ``k-1`` and ``k``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

Coord = Tuple[int, int, int]
Bounds = Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]

AXIS_NAMES = {"x": 0, "y": 1, "z": 2, "0": 0, "1": 1, "2": 2, 0: 0, 1: 1, 2: 2}

FACE_TABLE: Dict[str, Tuple[int, int]] = {
    "x-": (0, -1), "left": (0, -1), "l": (0, -1), "west": (0, -1),
    "左": (0, -1), "左面": (0, -1), "左侧": (0, -1), "左边": (0, -1),
    "x+": (0, 1), "right": (0, 1), "r": (0, 1), "east": (0, 1),
    "右": (0, 1), "右面": (0, 1), "右侧": (0, 1), "右边": (0, 1),
    "y-": (1, -1), "front": (1, -1), "south": (1, -1),
    "前": (1, -1), "前面": (1, -1), "正面": (1, -1),
    "y+": (1, 1), "back": (1, 1), "north": (1, 1),
    "后": (1, 1), "后面": (1, 1), "背面": (1, 1),
    "z-": (2, -1), "bottom": (2, -1), "down": (2, -1), "lower": (2, -1),
    "下": (2, -1), "下面": (2, -1), "底": (2, -1), "底面": (2, -1),
    "z+": (2, 1), "top": (2, 1), "up": (2, 1), "upper": (2, 1),
    "上": (2, 1), "上面": (2, 1), "顶": (2, 1), "顶面": (2, 1),
}

CANONICAL_FACES = ("x-", "x+", "y-", "y+", "z-", "z+")
FACE_ALIASES = {
    "left": "x-", "right": "x+", "front": "y-", "back": "y+",
    "bottom": "z-", "top": "z+",
}


class CheeseInputError(ValueError):
    """Raised when a cheese case/cut/box document is malformed."""


def as_coord(value: Any, size: Optional[Sequence[int]] = None) -> Coord:
    if isinstance(value, str):
        value = [int(x) for x in value.replace(",", " ").split()]
    if not isinstance(value, (list, tuple)):
        raise CheeseInputError(f"voxel coordinate must be a list, got {value!r}")
    if len(value) == 2:
        value = (value[0], value[1], 0)
    if len(value) != 3:
        raise CheeseInputError(f"voxel coordinate must have 2 or 3 entries: {value!r}")
    coord = (int(value[0]), int(value[1]), int(value[2]))
    if any(v < 0 for v in coord):
        raise CheeseInputError(f"negative voxel coordinate: {coord}")
    if size is not None and any(c >= n for c, n in zip(coord, size)):
        raise CheeseInputError(f"coordinate {coord} outside size {tuple(size)}")
    return coord


def normalize_size(size: Any) -> Tuple[int, int, int]:
    if size is None:
        return (1, 1, 1)
    if isinstance(size, int):
        size = (size, 1, 1)
    elif isinstance(size, str):
        size = tuple(int(x) for x in size.lower().replace("x", " ").split())
    size = tuple(int(x) for x in size)
    if len(size) == 1:
        size = (size[0], 1, 1)
    elif len(size) == 2:
        size = (size[0], size[1], 1)
    elif len(size) != 3:
        raise CheeseInputError(f"size must have 1-3 dimensions: {size!r}")
    if any(n <= 0 for n in size):
        raise CheeseInputError(f"dimensions must be positive: {size}")
    return size  # type: ignore[return-value]


def normalize_face(face: Any) -> str:
    if face is None:
        raise CheeseInputError("face is required")
    key = str(face).strip().lower().replace("_", "-").replace(" ", "-")
    if key in FACE_TABLE:
        axis, sign = FACE_TABLE[key]
        return f"{['x', 'y', 'z'][axis]}{'-' if sign < 0 else '+'}"
    compact = key.replace("-", "").replace("+", "")
    if compact in AXIS_NAMES:
        axis = AXIS_NAMES[compact]
        sign = 1 if key.endswith("+") or key in ("top", "right", "back") else -1
        return f"{['x', 'y', 'z'][axis]}{'+' if sign > 0 else '-'}"
    raise CheeseInputError(f"unknown face {face!r}")


def face_axis_sign(face: Any) -> Tuple[int, int]:
    canonical = normalize_face(face)
    return FACE_TABLE[canonical]


def normalize_plane(plane: Any, size: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Normalize a cut plane.

    Accepted forms include ``{"axis": "x", "at": 2}``, ``"x=2"`` and ``[0, 2]``.
    ``"mid"`` is accepted when ``size`` is supplied.
    """
    plane_id = None
    if isinstance(plane, str):
        text = plane.strip().lower()
        if text in ("mid", "middle", "center", "centre", "中线"):
            if size is None:
                raise CheeseInputError("mid plane requires block size")
            axis = 0
            at = int(size[0]) // 2
        elif "@" in text or "=" in text:
            sep = "@" if "@" in text else "="
            a, v = text.split(sep, 1)
            axis = AXIS_NAMES[a.strip()]
            at = int(v.strip())
        else:
            raise CheeseInputError(f"invalid plane {plane!r}")
    elif isinstance(plane, Sequence) and not isinstance(plane, Mapping):
        if len(plane) != 2:
            raise CheeseInputError(f"plane pair must be [axis, at]: {plane!r}")
        axis = AXIS_NAMES[plane[0]]
        at = int(plane[1])
    elif isinstance(plane, Mapping):
        plane_id = plane.get("id") or plane.get("plane_id") or plane.get("fixture_id")
        axis_value = plane.get("axis", plane.get("normal", "x"))
        if isinstance(axis_value, str) and axis_value.strip().lower() in (
            "mid", "middle", "center", "centre"
        ):
            axis = 0
            if size is None:
                raise CheeseInputError("mid plane requires block size")
            at = int(size[0]) // 2
        else:
            axis = AXIS_NAMES[axis_value]
            at = int(plane.get("at", plane.get("position", plane.get("index"))))
    else:
        raise CheeseInputError(f"invalid plane {plane!r}")

    if size is not None:
        n = int(size[axis])
        if not 0 < at < n:
            raise CheeseInputError(
                f"plane {['x', 'y', 'z'][axis]}@{at} is outside 1..{n - 1}"
            )
    result = {"axis": ["x", "y", "z"][axis], "axis_index": axis, "at": at}
    if plane_id is not None:
        result["id"] = str(plane_id)
    return result


def normalize_model(raw: Mapping[str, Any], size: Tuple[int, int, int]) -> Dict[str, Any]:
    model_id = str(raw.get("model_id") or raw.get("id") or raw.get("name") or "model")
    holes_raw = raw.get("holes", raw.get("hole_voxels", raw.get(
        "eyes", raw.get("empty_eyes", raw.get("空眼", raw.get("孔位", []))))))
    cells = raw.get("hole_cells", raw.get("cells", raw.get("孔格", [])))
    holes: set[Coord] = set()

    for h in holes_raw or []:
        if isinstance(h, int):
            # Shorthand integer means a one-based cell number on the x axis.
            holes.add((int(h) - 1, 0, 0))
        elif isinstance(h, Mapping):
            if "cell" in h:
                holes.add((int(h["cell"]) - 1, int(h.get("y", 0)), int(h.get("z", 0))))
            else:
                holes.add(as_coord(h.get("coord", [h.get("x", 0), h.get("y", 0), h.get("z", 0)]), size))
        else:
            holes.add(as_coord(h, size))
    for c in cells or []:
        if isinstance(c, int):
            holes.add((int(c) - 1, 0, 0))
        else:
            holes.add(as_coord(c, size))

    outside = [c for c in holes if any(v >= n for v, n in zip(c, size))]
    if outside:
        raise CheeseInputError(f"model {model_id!r} has holes outside size: {outside}")

    wax_faces: Dict[str, Dict[str, Any]] = {}
    wax = raw.get("wax_faces", raw.get("wax", "all_outer"))
    if wax in ("all", "all_outer", "outer", True, None):
        wax_faces = {f: {"ok": True, "color": raw.get("wax_color")} for f in CANONICAL_FACES}
    elif isinstance(wax, Sequence) and not isinstance(wax, (str, bytes)):
        for f in wax:
            if isinstance(f, Mapping):
                canonical = normalize_face(f.get("face") or f.get("name"))
                wax_faces[canonical] = {
                    "ok": bool(f.get("ok", True)),
                    "color": f.get("color"),
                }
            else:
                wax_faces[normalize_face(f)] = {"ok": True, "color": None}
    elif isinstance(wax, Mapping):
        for f, value in wax.items():
            canonical = normalize_face(f)
            if isinstance(value, Mapping):
                wax_faces[canonical] = {
                    "ok": bool(value.get("ok", True)),
                    "color": value.get("color"),
                }
            else:
                wax_faces[canonical] = {"ok": bool(value), "color": None}
    else:
        raise CheeseInputError(f"unsupported wax specification: {wax!r}")

    # Convenience: colored_wax: {"left": "red"}.
    colored = (raw.get("colored_wax") or raw.get("wax_colors")
               or raw.get("display_wax") or {})
    for f, color in colored.items():
        canonical = normalize_face(f)
        item = wax_faces.setdefault(canonical, {"ok": True, "color": None})
        item["color"] = color
        item["ok"] = True

    wax_voxels_raw = raw.get("wax_voxels", raw.get(
        "outer_wax_voxels", raw.get("inedible_wax", raw.get("外蜡"))))
    wax_voxels: List[Coord] = []
    if isinstance(wax_voxels_raw, Sequence) and not isinstance(wax_voxels_raw, (str, bytes, Mapping)):
        for w in wax_voxels_raw:
            wax_voxels.append(as_coord(w, size) if not isinstance(w, int)
                              else (int(w) - 1, 0, 0))
    elif wax_voxels_raw is True or raw.get("outer_wax") is True:
        for x in range(size[0]):
            for y in range(size[1]):
                for z in range(size[2]):
                    if (x in (0, size[0] - 1) or y in (0, size[1] - 1)
                            or z in (0, size[2] - 1)):
                        wax_voxels.append((x, y, z))

    return {
        "model_id": model_id,
        "size": list(size),
        "holes": sorted(holes),
        "hole_set": holes,
        "wax_voxels": sorted(set(wax_voxels)),
        "wax_faces": wax_faces,
        "outer_wax_edible": bool(raw.get("outer_wax_edible", False)),
        "raw": {k: v for k, v in raw.items() if k not in ("hole_set",)},
    }


def normalize_block(raw: Mapping[str, Any], validate_limits: bool = True) -> Dict[str, Any]:
    block_id = str(raw.get("block_id") or raw.get("id") or raw.get("name"))
    if not block_id:
        raise CheeseInputError("block_id is required")
    size = normalize_size(raw.get("size") or raw.get("dimensions") or (1, 1, 1))
    if validate_limits:
        if max(size) > 5:
            raise CheeseInputError(f"block {block_id!r} side exceeds five cells: {size}")
    raw_models = raw.get("candidate_models", raw.get("models", []))
    if not raw_models:
        raise CheeseInputError(f"block {block_id!r} has no candidate models")
    models_raw = []
    for idx, m in enumerate(raw_models):
        if isinstance(m, Mapping):
            models_raw.append(m)
        elif isinstance(m, (list, tuple, set, frozenset)):
            models_raw.append({"model_id": f"model-{idx + 1}", "holes": list(m)})
        elif m is None:
            models_raw.append({"model_id": f"solid-{idx + 1}", "holes": []})
        else:
            models_raw.append({"model_id": f"model-{m}", "holes": [m]})
    models = [normalize_model(m, size) for m in models_raw]
    ids = [m["model_id"] for m in models]
    if len(ids) != len(set(ids)):
        raise CheeseInputError(f"duplicate model ids for block {block_id!r}: {ids}")
    fixtures = []
    for f in raw.get("fixtures", raw.get("allowed_planes", raw.get("planes", []))) or []:
        plane = normalize_plane(f, size)
        if isinstance(f, Mapping):
            plane.setdefault("id", str(f.get("id") or f.get("fixture_id") or f"plane-{plane['axis']}{plane['at']}"))
            if f.get("piece_id"):
                plane["piece_id"] = str(f["piece_id"])
        else:
            plane.setdefault("id", f"plane-{plane['axis']}{plane['at']}")
        plane["block_id"] = block_id
        fixtures.append(plane)
    return {
        "block_id": block_id,
        "size": list(size),
        "models": models,
        "fixtures": fixtures,
    }


def bounds_from_size(size: Sequence[int]) -> Bounds:
    return ((0, int(size[0])), (0, int(size[1])), (0, int(size[2])))


def initial_piece(block: Mapping[str, Any]) -> Dict[str, Any]:
    size = tuple(block["size"])
    return {
        "piece_id": block["block_id"],
        "source_block_id": block["block_id"],
        "source_block_size": list(size),
        "bounds": [list(x) for x in bounds_from_size(size)],
        "size": list(size),
        "cut_path": [],
        "original_faces": original_faces(size, bounds_from_size(size)),
    }


def original_faces(size: Sequence[int], bounds: Sequence[Sequence[int]]) -> List[str]:
    faces = []
    for axis in range(3):
        lo, hi = int(bounds[axis][0]), int(bounds[axis][1])
        if lo == 0:
            faces.append(f"{['x', 'y', 'z'][axis]}-")
        if hi == int(size[axis]):
            faces.append(f"{['x', 'y', 'z'][axis]}+")
    return faces


def piece_size(piece: Mapping[str, Any]) -> Tuple[int, int, int]:
    return tuple(int(hi) - int(lo) for lo, hi in piece["bounds"])  # type: ignore[return-value]


def plane_applies(piece: Mapping[str, Any], plane: Mapping[str, Any]) -> bool:
    axis = int(plane["axis_index"])
    lo, hi = piece["bounds"][axis]
    return int(lo) < int(plane["at"]) < int(hi)


def split_piece(
    piece: Mapping[str, Any],
    plane: Mapping[str, Any],
    cut_id: str,
    child_ids: Optional[Tuple[str, str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not plane_applies(piece, plane):
        raise CheeseInputError(
            f"plane {plane.get('id') or plane} does not split piece {piece['piece_id']}"
        )
    axis = int(plane["axis_index"])
    at = int(plane["at"])
    size = piece.get("source_size")
    # Prefer the source block size carried on the piece; callers normally add it.
    source_size = piece.get("source_block_size") or piece.get("source_size")
    if source_size is None:
        # Bounds are global, but original faces can still be inferred from root
        # metadata attached by reconstruct_pieces. Fall back to upper bounds.
        source_size = [max(int(b[1]), at) for b in piece["bounds"]]

    bounds_a = [list(x) for x in piece["bounds"]]
    bounds_b = [list(x) for x in piece["bounds"]]
    bounds_a[axis][1] = at
    bounds_b[axis][0] = at
    if child_ids is not None:
        base_a, base_b = child_ids
    elif axis == 0:
        base_a, base_b = f"{piece['piece_id']}.left", f"{piece['piece_id']}.right"
    elif axis == 2:
        base_a, base_b = f"{piece['piece_id']}.bottom", f"{piece['piece_id']}.top"
    else:
        base_a, base_b = f"{piece['piece_id']}.front", f"{piece['piece_id']}.back"
    path = list(piece.get("cut_path", [])) + [cut_id]
    a = {
        "piece_id": base_a,
        "source_block_id": piece["source_block_id"],
        "source_block_size": list(source_size),
        "bounds": bounds_a,
        "size": [b[1] - b[0] for b in bounds_a],
        "cut_path": path,
        "created_by_cut": cut_id,
        "side": "-",
        "original_faces": original_faces(source_size, bounds_a),
    }
    b = {
        "piece_id": base_b,
        "source_block_id": piece["source_block_id"],
        "source_block_size": list(source_size),
        "bounds": bounds_b,
        "size": [b[1] - b[0] for b in bounds_b],
        "cut_path": path,
        "created_by_cut": cut_id,
        "side": "+",
        "original_faces": original_faces(source_size, bounds_b),
    }
    return a, b


def hole_coords(model: Mapping[str, Any]) -> set[Coord]:
    if "hole_set" in model:
        return set(model["hole_set"])
    return {tuple(h) for h in model.get("holes", [])}  # type: ignore[arg-type]


def holes_in_region(model: Mapping[str, Any], piece: Mapping[str, Any]) -> List[Coord]:
    bounds = piece["bounds"]
    result = []
    for coord in hole_coords(model):
        if all(int(bounds[a][0]) <= coord[a] < int(bounds[a][1]) for a in range(3)):
            result.append(coord)
    return sorted(result)


def shape_dimensions(requirement: Mapping[str, Any]) -> Tuple[int, int, int]:
    shape = requirement.get("shape", requirement.get(
        "dimensions", requirement.get("size", requirement.get("形状"))))
    if shape is None:
        # A strip defaults to two cells when only length is supplied.
        shape = (int(requirement.get("length", 2)), 1, 1)
    dims = normalize_size(shape)
    return dims


def shape_matches(piece: Mapping[str, Any], requirement: Mapping[str, Any],
                  allow_rotation: Optional[bool] = None) -> bool:
    wanted = shape_dimensions(requirement)
    actual = piece_size(piece)
    if allow_rotation is None:
        allow_rotation = bool(requirement.get("allow_rotation", True))
    if actual == wanted:
        return True
    if not allow_rotation:
        return False
    return sorted(actual) == sorted(wanted)


def display_face_ok(model: Mapping[str, Any], piece: Mapping[str, Any],
                    requirement: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    required = requirement.get(
        "display_face",
        requirement.get("wax_face", requirement.get(
            "colored_face", requirement.get(
                "face", requirement.get("保留展面", requirement.get("展面"))))),
    )
    wax_required = (requirement.get("require_wax", False)
                    or requirement.get("colored_wax_required", False)
                    or requirement.get("needs_display_face", False))
    if required is None and not wax_required:
        return True, []
    faces = set(piece.get("original_faces", []))
    wax_faces = model.get("wax_faces", {})
    color = requirement.get("wax_color", requirement.get(
        "color", requirement.get("display_wax_color")))
    reasons: List[str] = []

    candidates: List[str]
    if required is None or str(required).lower() in ("any_outer", "outer", "wax", "展面", "任意外端"):
        candidates = sorted(faces)
    else:
        canonical = normalize_face(required)
        candidates = [canonical] if canonical in faces else []
        if canonical not in faces:
            reasons.append(f"piece does not retain required face {canonical}")

    for face in candidates:
        wax = wax_faces.get(face)
        if not wax or not wax.get("ok", False):
            reasons.append(f"wax on {face} is not qualified")
            continue
        if color is not None and wax.get("color") != color:
            reasons.append(f"wax on {face} has color {wax.get('color')!r}, wanted {color!r}")
            continue
        return True, []
    if not reasons:
        reasons.append("piece retains no original wax face")
    return False, reasons


def evaluate_fit(model: Mapping[str, Any], piece: Mapping[str, Any],
                 requirement: Mapping[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    shape_ok = shape_matches(piece, requirement)
    if not shape_ok:
        reasons.append(
            f"shape {tuple(piece_size(piece))} does not match {shape_dimensions(requirement)}"
        )
    holes = holes_in_region(model, piece)
    max_holes = int(requirement.get(
        "max_holes",
        requirement.get("allowed_holes",
                        requirement.get("hole_limit",
                                        requirement.get("允许空眼数", 0)))))
    holes_ok = len(holes) <= max_holes
    if requirement.get("exact_hole_count") or requirement.get("hole_count_exact"):
        holes_ok = len(holes) == max_holes
    if not holes_ok:
        reasons.append(f"{len(holes)} holes exceed allowed {max_holes}: {holes}")
    wax_ok, wax_reasons = display_face_ok(model, piece, requirement)
    reasons.extend(wax_reasons)
    return {
        "ok": shape_ok and holes_ok and wax_ok,
        "shape_ok": shape_ok,
        "holes": [list(c) for c in holes],
        "hole_count": len(holes),
        "max_holes": max_holes,
        "wax_ok": wax_ok,
        "reasons": reasons,
    }


def universal_fit(piece: Mapping[str, Any], requirement: Mapping[str, Any],
                  models: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_model = {str(m["model_id"]): evaluate_fit(m, piece, requirement) for m in models}
    # An empty model set is a QC contradiction, not vacuous permission to promise.
    safe = bool(models) and all(v["ok"] for v in by_model.values())
    worst_holes = max((v["hole_count"] for v in by_model.values()), default=0)
    return {
        "safe": safe,
        "piece_id": piece["piece_id"],
        "requirement_key": requirement.get("key"),
        "worst_hole_count": worst_holes,
        "by_model": by_model,
        "failure_reasons": [] if safe else [
            f"{mid}: {v['reasons']}" for mid, v in by_model.items() if not v["ok"]
        ],
    }
