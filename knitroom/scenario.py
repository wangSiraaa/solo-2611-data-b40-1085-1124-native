"""题面场景的图解册版工厂与借还台账装载。

全部图解满足“最多八行、每行不超过十二针”。命名约定：
- 袖山缝边的两个定位标：AXL 腋下标、SHD 肩标，方向 up（不可翻面）；
- 区间 AXL->SHD 即“腋标到肩标内”，吃势许可按具名区声明；
- cast_on 是起针行的具名线圈，后续行的每个产出圈也都具名，
  dec 的两个父圈、inc 的两个子圈在边圈谱里均可追溯；
- 标记锚圈取“该行织完仍唯一”的具名圈，因此标记一直活到边端收针。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MATERIAL = Path(__file__).parent.parent / "materials" / "765" / "input.json"

# 图解（同名作品）标识
SLEEVE_CHART = "sleeve-cap"
BODY_CHART = "body-armhole"


def sleeve_chart(edition: str, increases: int = 1) -> dict[str, Any]:
    """待办袖图解。

    实袖腋标到肩标内原有 2 段边弧（起针边圈 S02、S03 各持一段）；
    图解在指定边圈上作 increases 次增加：
    - increases=1（原图）：一次增加形成 3 段，一次吃势可对成 2 对 1 再 1 对 1；
    - increases=2（连续两次增加）：形成 4 段，一次吃势不够，缝台必须拒绝。
    S01 是腋标外侧边圈、末针是肩标外侧边圈，不计入“腋标到肩标内”。
    """
    if increases == 1:
        rows = [
            {
                "row_no": 1,
                "ops": [
                    {"action": "knit", "sources": [0], "name": "L1"},
                    {"action": "knit", "sources": [1], "name": "M11"},
                    # 在指定边圈 S03 上增一针：一个父圈分出两个具名子圈
                    {"action": "inc", "sources": [2], "names": ["M12A", "M12B"],
                     "on_edge": True},
                    {"action": "knit", "sources": [3], "name": "R1"},
                ],
            },
        ]
        markers = [
            {"id": "AXL", "anchor": "L1", "orientation": "up"},
            {"id": "SHD", "anchor": "R1", "orientation": "up"},
        ]
    else:
        rows = [
            {
                "row_no": 1,
                "ops": [
                    {"action": "knit", "sources": [0], "name": "L1"},
                    {"action": "knit", "sources": [1], "name": "M11"},
                    {"action": "inc", "sources": [2], "names": ["M12A", "M12B"],
                     "on_edge": True},
                    {"action": "knit", "sources": [3], "name": "R1"},
                ],
            },
            {
                # 连续第二次增加：在上一针增出的边圈 M12A 上再增一针
                "row_no": 2,
                "ops": [
                    {"action": "knit", "sources": [0], "name": "L2"},
                    {"action": "knit", "sources": [1], "name": "M21"},
                    {"action": "inc", "sources": [2], "names": ["M22A", "M22B"],
                     "on_edge": True},
                    {"action": "knit", "sources": [3], "name": "M23"},
                    {"action": "knit", "sources": [4], "name": "R2"},
                ],
            },
        ]
        markers = [
            {"id": "AXL", "anchor": "L2", "orientation": "up"},
            {"id": "SHD", "anchor": "R2", "orientation": "up"},
        ]
    return {
        "chart_id": SLEEVE_CHART,
        "edition": edition,
        "book_id": "WB",
        "cast_on": ["S01", "S02", "S03", "S04"],
        "rows": rows,
        "edge_dec_end": None,
        "markers": markers,
    }


def sleeve_reissue_chart(edition: str, edge_dec_end: str) -> dict[str, Any]:
    """同名再版：图解主体沿用一次增加的原图，只改收针端（本册勘误）。

    新增末行在指定边端做一次二并一收边针；锚在被收父圈上的标记随收针消失，
    制样师短行查询随即报 marker_gone。
    """
    base = sleeve_chart(edition, increases=1)
    # 末行输入是 row1 inc 之后的 5 个产出圈；两端各做一次二并一收边针
    if edge_dec_end == "left":
        ops = [
            {"action": "dec", "sources": [0, 1], "name": "EL", "on_edge": True},
            *[
                {"action": "knit", "sources": [i], "name": f"E{i}"}
                for i in range(2, 5)
            ],
        ]
        markers = [
            {"id": "AXL", "anchor": "L1", "orientation": "up"},   # 左父，将被收
            {"id": "SHD", "anchor": "R1", "orientation": "up"},
        ]
    else:
        ops = [
            *[
                {"action": "knit", "sources": [i], "name": f"E{i}"}
                for i in range(3)
            ],
            {"action": "dec", "sources": [3, 4], "name": "ER", "on_edge": True},
        ]
        markers = [
            {"id": "AXL", "anchor": "L1", "orientation": "up"},
            {"id": "SHD", "anchor": "R1", "orientation": "up"},  # 右父，将被收
        ]
    base["rows"].append({"row_no": len(base["rows"]) + 1, "ops": ops})
    base["edge_dec_end"] = edge_dec_end
    base["markers"] = markers
    return base


def body_chart(edition: str, orientation: str = "up") -> dict[str, Any]:
    """衣身袖窿图解：腋标到肩标内 2 段边弧（C2、C3 各持一段）。

    吃势许可是缝台入参里的具名区声明，不在图解里固化——
    “把吃势许可移到肩标外”由调用方改 ease_regions 即可，衣身记录不变。
    orientation='down' 用于演示定位标不可翻面（方向不配即拒绝合缝）。
    """
    return {
        "chart_id": BODY_CHART,
        "edition": edition,
        "book_id": "WA",
        "cast_on": ["B01", "B02", "B03", "B04"],
        "rows": [
            {
                "row_no": 1,
                "ops": [
                    {"action": "knit", "sources": [0], "name": "C1"},
                    {"action": "knit", "sources": [1], "name": "C2"},
                    {"action": "knit", "sources": [2], "name": "C3"},
                    {"action": "knit", "sources": [3], "name": "C4"},
                ],
            },
        ],
        "edge_dec_end": None,
        "markers": [
            {"id": "AXL", "anchor": "C1", "orientation": orientation},
            {"id": "SHD", "anchor": "C4", "orientation": orientation},
        ],
    }


# ---------------------------------------------------------------- 台账装载

def load_library_material(system: Any) -> dict[str, Any]:
    """把 materials/765 装进图书馆系统（与原 demo 同顺序），返回材料字典。"""
    data = json.loads(MATERIAL.read_text(encoding="utf-8"))
    for cat in data["catalogs"]:
        system.catalog.submit_revision(
            revision=cat["revision"],
            bibs=list(data["books"]),
            aliases=cat["aliases"],
            copies=data["copies"],
            op_id=f"seed-catalog-{cat['revision']}",
        )
    for t in data["tickets"]:
        system.ledger.register_ticket(
            ticket_id=t["id"],
            reader_id=t["reader"],
            original_bib=t["book"],
            arrival_seq=t["seq"],
            allowed_branches=t["branches"],
            allowed_formats=t["formats"],
            op_id=f"seed-ticket-{t['id']}",
        )
    system.catalog.activate_revision("v1")
    held = data["held"][0]
    system.ledger.seed_hold(
        hold_id=held["id"],
        revision=held["catalog"],
        ticket_id=held["ticket"],
        reader_id=held["reader"],
        copy_id=held["copy"],
        branch=held["branch"],
    )
    return data


def fulfill_j5(system: Any) -> dict[str, Any]:
    """重放 J4 拒绝、J5 真实兑现：RC 实际取到 C3，返回 J5 回执。"""
    j4 = system.ledger.pickup("J4", "H3", "RA", "C3")
    assert j4["status"] == "rejected"
    j5 = system.ledger.pickup("J5", "H3", "RC", "C3")
    assert j5["status"] == "ok"
    return j5
