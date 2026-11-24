"""Read the *actual* JSONL produced by the reproducible weighted sampler."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..dataset import load_dataset
from .qc import quality_check


def _parse_body(record: Mapping[str, Any]) -> Any:
    body = record.get("body")
    if isinstance(body, (dict, list)):
        return body
    if body is None:
        return {}
    text = str(body)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Old prose source bodies remain readable; they simply are not structured
        # cheese records.  Audit workflows that correct prose can still inspect
        # it through SourceService rather than treating it as a QC datum.
        return {"record_type": "prose", "text": text}


def parse_probe_prose(text: str, record: Optional[Mapping[str, Any]] = None
                      ) -> Optional[Dict[str, Any]]:
    """Best-effort parser for human prose source bodies.

    Structured JSON bodies are preferred.  This exists so correcting an old
    textual source body such as ``左探针...首孔在第4格`` remains auditable.
    """
    record = record or {}
    depth = None
    patterns = [
        r"首孔(?:深度)?(?:在)?第?\s*(\d+)\s*格",
        r"first[_\s-]?hole[_\s-]?depth\s*[=:：]\s*(\d+)",
        r"first[_\s-]?hole\s*[=:：]\s*(\d+)",
        r"深度\s*[=:：]?\s*(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            depth = int(m.group(1))
            break
    solid = bool(re.search(r"全程实心|实心|solid", text, flags=re.IGNORECASE))
    if depth is None and not solid:
        return None

    block = None
    for pat in [r"block(?:_id)?\s*[=:：]\s*([A-Za-z0-9_.\-]+)",
                r"具名块\s*[=:：]?\s*([A-Za-z0-9_.\-]+)",
                r"块\s*[=:：]?\s*([A-Za-z0-9_.\-]+)"]:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            block = m.group(1)
            break
    if block is None:
        return None

    face = "left"
    for token, canonical in [
        ("左面", "left"), ("左侧", "left"), ("左边", "left"), ("left", "left"),
        ("右面", "right"), ("右侧", "right"), ("右边", "right"), ("right", "right"),
        ("前面", "front"), ("正面", "front"), ("front", "front"),
        ("后面", "back"), ("背面", "back"), ("back", "back"),
        ("上面", "top"), ("顶面", "top"), ("top", "top"),
        ("下面", "bottom"), ("底面", "bottom"), ("bottom", "bottom"),
    ]:
        if token.lower() in text.lower():
            face = canonical
            break
    rig = None
    m = re.search(r"(?:rig[_\s-]?id|托架|同架)\s*[=:：]?\s*([A-Za-z0-9_.\-]+)",
                  text, flags=re.IGNORECASE)
    if m:
        rig = m.group(1)
    return {
        "record_type": "probe",
        "probe_id": record.get("row_id") or f"prose-{block}-{depth or 'solid'}",
        "block_id": block,
        "entry_face": face,
        "first_hole_depth": depth,
        "solid": solid and depth is None,
        "rig_id": rig,
        "source_row_id": record.get("row_id"),
        "source_version": record.get("source_version"),
        "prose": text,
    }


def case_from_sample(records: Sequence[Mapping[str, Any]],
                     base_case: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Aggregate the whole sampled JSONL into one normalized cheese case."""
    case: Dict[str, Any] = {
        "blocks": [], "probes": [], "rigs": [], "boxes": [], "fixtures": [],
    }
    if base_case:
        case.update(dict(base_case))
        case.setdefault("blocks", [])
        case.setdefault("probes", [])

    block_index: Dict[str, Dict[str, Any]] = {
        b.get("block_id", b.get("id")): b for b in case["blocks"]
    }

    def ensure_block(bid: str) -> Dict[str, Any]:
        if bid not in block_index:
            b = {"block_id": bid, "models": []}
            case["blocks"].append(b)
            block_index[bid] = b
        return block_index[bid]

    row_ids = []
    for rec in records:
        row_ids.append(rec.get("row_id"))
        body = _parse_body(rec)
        if not isinstance(body, Mapping):
            continue
        if body.get("record_type") == "prose":
            probe = parse_probe_prose(str(body.get("text", "")), rec)
            if probe:
                case["probes"].append(probe)
            continue
        rtype = body.get("record_type") or body.get("type") or ""
        rtype = str(rtype).lower()
        payload = dict(body)
        payload.setdefault("source_row_id", rec.get("row_id"))
        payload.setdefault("source_version", rec.get("source_version"))

        if rtype in ("cheese_case", "case", "order"):
            for key in ("blocks", "probes", "rigs", "boxes", "fixtures",
                        "station_beats", "cut_budget", "mode", "name"):
                if key in payload:
                    if key in ("blocks", "probes", "rigs", "boxes", "fixtures") and key in case:
                        if isinstance(payload[key], list):
                            case.setdefault(key, []).extend(payload[key])
                    else:
                        case[key] = payload[key]
            continue
        if rtype in ("block", "cheese_block"):
            bid = payload.get("block_id") or payload.get("id")
            b = ensure_block(str(bid))
            b.update({k: v for k, v in payload.items() if k != "models"})
            b.setdefault("models", [])
            b["models"].extend(payload.get("models", payload.get("candidate_models", [])))
            continue
        if rtype in ("model", "candidate_model"):
            bid = str(payload.get("block_id") or payload.get("block"))
            b = ensure_block(bid)
            b.setdefault("models", []).append(payload)
            continue
        if rtype in ("probe", "probe_record", "core_probe"):
            case["probes"].append(payload)
            continue
        if rtype in ("rig", "cradle"):
            case["rigs"].append(payload)
            continue
        if rtype in ("box", "display_box"):
            case["boxes"].append(payload)
            continue
        if rtype in ("fixture", "plane"):
            case["fixtures"].append(payload)
            continue

        # Tolerate a body that directly looks like a probe/block.
        if "entry_face" in payload or "first_hole_depth" in payload or "solid" in payload:
            case["probes"].append(payload)
        elif "candidate_models" in payload or "models" in payload and "size" in payload:
            case["blocks"].append(payload)
        elif "strips" in payload:
            case["boxes"].append(payload)

    case["sample_row_ids"] = row_ids
    return case


def quality_check_sample(records: Sequence[Mapping[str, Any]],
                         base_case: Optional[Mapping[str, Any]] = None,
                         source_version: Optional[int] = None) -> Dict[str, Any]:
    case = case_from_sample(records, base_case)
    report = quality_check(case)
    report["source_version"] = source_version
    report["sample_row_ids"] = [r.get("row_id") for r in records]
    report["sample_record_count"] = len(records)
    return report


def quality_check_dataset(dataset_dir: str,
                          source_db: Optional[str] = None,
                          verify: bool = False,
                          base_case: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    manifest, records = load_dataset(dataset_dir)
    if verify and source_db:
        from ..source import SourceService
        from ..dataset import verify_dataset
        svc = SourceService(source_db)
        try:
            verification = verify_dataset(svc, dataset_dir)
        finally:
            close = getattr(svc, "close", None)
            if callable(close):
                close()
    else:
        verification = None
    report = quality_check_sample(
        records, base_case=base_case, source_version=manifest.get("source_version")
    )
    report["dataset_dir"] = os.path.abspath(dataset_dir)
    report["manifest"] = {
        "source_version": manifest.get("source_version"),
        "seed_hex": manifest.get("seed_hex"),
        "quotas": manifest.get("quotas"),
        "records_sha256": manifest.get("records_sha256"),
    }
    if verification is not None:
        report["verification"] = {"ok": verification.get("ok", True)}
    return report
