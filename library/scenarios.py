"""请柬业务场景搭建助手：从 materials/invitation/input.json 装载，或编程式造场景。

全部人物为虚构；与现有借书系统的衔接点：
- 词册走 catalog 登记（含修订/归并），词义走 kinship 按副本号登记；
- 借书取件回执上的 copy_id 即“实际取走的村志版本”，配词只认这个号。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .system import LibrarySystem

INVITATION_MATERIAL = (
    Path(__file__).resolve().parent.parent
    / "materials" / "invitation" / "input.json"
)


def load_material(system: LibrarySystem, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """完整装载请柬材料：目录修订、副本词条、两份候选谱、口述、印位、字盘、字模、柬。"""
    data = data or json.loads(INVITATION_MATERIAL.read_text(encoding="utf-8"))
    copies = data["copies"]
    for cat in data["catalogs"]:
        system.catalog.submit_revision(
            revision=cat["revision"],
            bibs=list(data["books"]),
            aliases=cat.get("aliases", []),
            copies=copies,
            op_id=f"seed-kcat-{cat['revision']}",
        )
    head = data["catalogs"][-1]["revision"]
    system.catalog.activate_revision(head)
    for ct in data["copy_terms"]:
        system.kinship.register_copy_terms(
            ct["copy_id"], ct["entries"], op_id=f"seed-terms-{ct['copy_id']}"
        )
    for tree in data["trees"]:
        system.kinship.register_tree(
            tree["tree_id"], tree["persons"],
            op_id=f"seed-tree-{tree['tree_id']}",
            side=tree.get("side"), attrs=tree.get("attrs", {}),
        )
    for reg in data["registrations"]:
        system.kinship.register_registration(
            reg["reg_id"], reg["speaker"], reg["referent"], reg["village"],
            reg["copy_id"], reg["paths"], op_id=f"seed-reg-{reg['reg_id']}",
        )
    for pos in data.get("positions", []):
        system.invitation.register_position(pos["position_id"], pos["keyed"])
    for tray in data.get("trays", []):
        system.invitation.add_tray(tray)
    for term in data.get("molds", []):
        system.invitation.set_mold(term, True, op_id=f"seed-mold-{term}")
    for card in data.get("cards", []):
        system.invitation.register_card(
            card["card_id"], card["recipient"], card["signers"],
            card["copy_id"], card["deadline_shift"],
            op_id=f"seed-card-{card['card_id']}",
        )
    return data


def new_system_with_catalog(
    copies: list[dict[str, str]],
    bibs: list[str] | None = None,
    revision: str = "k1",
) -> LibrarySystem:
    """编程式造一个只含词册目录的内存系统（场景 B/D 用）。"""
    system = LibrarySystem(":memory:")
    bibs = bibs or sorted({c["book"] for c in copies})
    system.catalog.submit_revision(
        revision, bibs, [], copies, op_id=f"cat-{revision}"
    )
    system.catalog.activate_revision(revision)
    return system
