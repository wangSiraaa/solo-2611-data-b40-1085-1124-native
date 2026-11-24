"""方言亲称词册、候选家系与口述语料（全部人物为虚构）。

与既有目录/台账的边界
----------------------
- 词册本身仍由 catalog 登记为书目，村志版本是 revision_copies 中的副本；
  书目可以经别名边归并（多册方言词册并入同一规范书目），但**词义只按实际
  取件回执上的副本号 copy_id 读取**——归并不揉掉“实际取走的村志版本”。
- 本模块不保存任何顺序、印位或柬状态，只负责：词义词条、家系、语料、证言，
  以及“在某岗位可见证据下哪些家系仍成立”的认知计算。

路径语言（词条 pattern 与口述 paths 同一套）
-------------------------------------------
每条路径是若干 step 的有序列表，step 为：
  ("up", "father" | "mother")  向上一代（父/母各至多一人，谱外长辈留空）
  ("sib",)                     同代同胞（由同一对父母与已登记长幼顺序支持）
  ("down",)                    向下一代
例：父的姐 = [("up","father"), ("sib",)]，母的姐 = [("up","mother"), ("sib",)]。

可能世界
--------
候选家系是**有限的具名关系备选**。任何写入的家系都先做无环校验，保证
“选边时不能令某人成自己的祖先”；父、母各至多一人由表结构与写入校验共同保证。
访谈员可追加证言：实名证言直接指认实际家系（仅持钥岗位可见时不进礼房证据），
非实名约束按世界属性/路径条件筛掉不成立的家系，并能给出两份有分歧的家系。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .errors import Rejection

# 受保护身份的两种取值；家属要求隐去父系或母系身份时用这两个 side
PROTECTED_SIDES = ("patrilineal", "matrilineal")
SIDE_CN = {"patrilineal": "父系", "matrilineal": "母系"}


class KinshipService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ================================================================ 词册

    def register_copy_terms(
        self, copy_id: str, entries: list[dict[str, Any]], op_id: str
    ) -> dict[str, Any]:
        """为某一副本（实际取走的村志版本）登记词条。

        entries: [{"term": "姑母", "patterns": [[...], ...]}, ...]
        副本号必须已存在于某个目录修订中；同一副本重复登记整单拒绝，
        改词义只能换副本（新版本）或显式撤换——词册与目录一样保留原始身份。
        """
        payload = {"copy_id": copy_id, "entries": _norm_entries(entries)}
        fp = db.fingerprint("register_copy_terms", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_copy_terms", fp)
            if prior is not None:
                conn.commit()
                return prior
            self._require_known_copy(copy_id)
            if conn.execute(
                "SELECT 1 FROM kin_copy_terms WHERE copy_id=?", (copy_id,)
            ).fetchone():
                raise Rejection("copy_terms_exists", copy=copy_id)
            _validate_entries(entries)
            conn.execute(
                "INSERT INTO kin_copy_terms(copy_id, entries, created_at) "
                "VALUES(?, ?, ?)",
                (copy_id, json.dumps(payload["entries"], ensure_ascii=False), db.now_iso()),
            )
            result = {"status": "ok", "copy_id": copy_id, "entries": payload["entries"]}
            db.record_operation(conn, op_id, "register_copy_terms", fp)
            db.add_receipt(conn, "register_copy_terms", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_copy_terms", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_copy_terms(self, copy_id: str) -> dict[str, Any]:
        """按真实取件回执的副本号读取亲称词义（公开查询）。"""
        row = self.conn.execute(
            "SELECT entries FROM kin_copy_terms WHERE copy_id=?", (copy_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_copy_terms", copy=copy_id)
        entries = json.loads(row["entries"])
        return {"copy_id": copy_id, "entries": entries, "terms": [e["term"] for e in entries]}

    # ================================================================ 家系

    def register_tree(
        self,
        tree_id: str,
        persons: list[dict[str, Any]],
        op_id: str,
        side: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一份候选家系（有限具名备选）。

        persons: [{"id": p, "father": f|None, "mother": m|None,
                   "seniority": int|None, "off_pedigree": bool}, ...]
        - 父、母各至多一人；边终点必须在本家系内；整图无环（含自环）；
        - 谱外长辈（off_pedigree）父母留空，不参与备选关系；
        - side 标注该世界的受保护身份，attrs 保存该世界可观测属性
          （如联名先后 order、字盘 tray）。
        """
        norm = _norm_persons(persons)
        payload = {
            "tree_id": tree_id,
            "side": side,
            "attrs": attrs or {},
            "persons": norm,
        }
        fp = db.fingerprint("register_tree", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_tree", fp)
            if prior is not None:
                conn.commit()
                return prior
            if side is not None and side not in PROTECTED_SIDES:
                raise Rejection("bad_protected_side", side=side)
            if conn.execute(
                "SELECT 1 FROM kin_trees WHERE tree_id=?", (tree_id,)
            ).fetchone():
                raise Rejection("tree_exists", tree=tree_id)
            _validate_tree(tree_id, norm)
            conn.execute(
                "INSERT INTO kin_trees(tree_id, side, attrs, created_at) "
                "VALUES(?, ?, ?, ?)",
                (tree_id, side, json.dumps(attrs or {}, ensure_ascii=False), db.now_iso()),
            )
            conn.executemany(
                "INSERT INTO kin_persons(tree_id, person_id, father_id, mother_id, "
                "seniority, off_pedigree) VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        tree_id,
                        p["id"],
                        p["father"],
                        p["mother"],
                        p["seniority"],
                        1 if p["off_pedigree"] else 0,
                    )
                    for p in norm
                ],
            )
            result = {"status": "ok", "tree_id": tree_id, "side": side,
                      "attrs": attrs or {}, "persons": norm}
            db.record_operation(conn, op_id, "register_tree", fp)
            db.add_receipt(conn, "register_tree", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_tree", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_tree(self, tree_id: str) -> dict[str, Any]:
        conn = self.conn
        trow = conn.execute(
            "SELECT tree_id, side, attrs FROM kin_trees WHERE tree_id=?", (tree_id,)
        ).fetchone()
        if trow is None:
            raise Rejection("unknown_tree", tree=tree_id)
        return {
            "tree_id": tree_id,
            "side": trow["side"],
            "attrs": json.loads(trow["attrs"]),
            "persons": self._load_persons(tree_id),
        }

    def list_trees(self) -> list[str]:
        return [
            r["tree_id"]
            for r in self.conn.execute("SELECT tree_id FROM kin_trees ORDER BY tree_id")
        ]

    def _load_persons(self, tree_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT person_id, father_id, mother_id, seniority, off_pedigree "
            "FROM kin_persons WHERE tree_id=? ORDER BY person_id",
            (tree_id,),
        ).fetchall()
        return [
            {
                "id": r["person_id"],
                "father": r["father_id"],
                "mother": r["mother_id"],
                "seniority": r["seniority"],
                "off_pedigree": bool(r["off_pedigree"]),
            }
            for r in rows
        ]

    # ================================================================ 语料

    def register_registration(
        self,
        reg_id: str,
        speaker: str,
        referent: str,
        village: str,
        copy_id: str,
        paths: list[list[tuple[str, ...]]],
        op_id: str,
    ) -> dict[str, Any]:
        """登记一条口述：说话人、所指、村别，以及该册容许的一组关系路径。

        例：referent 是说话人“父的姐或母的姐”，两条候选路径同时登记。
        路径条件在筛家系时解释：至少一条路径在家系中可走通，该家系才不被排除。
        """
        norm_paths = [_norm_path(p) for p in paths]
        payload = {
            "reg_id": reg_id,
            "speaker": speaker,
            "referent": referent,
            "village": village,
            "copy_id": copy_id,
            "paths": norm_paths,
        }
        fp = db.fingerprint("register_registration", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_registration", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM kin_registrations WHERE reg_id=?", (reg_id,)
            ).fetchone():
                raise Rejection("registration_exists", reg=reg_id)
            self._require_known_copy(copy_id)
            for p in norm_paths:
                _validate_path(p)
            conn.execute(
                "INSERT INTO kin_registrations(reg_id, speaker, referent, village, "
                "copy_id, paths, op_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reg_id, speaker, referent, village, copy_id,
                    json.dumps(norm_paths, ensure_ascii=False), op_id, db.now_iso(),
                ),
            )
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "register_registration", fp)
            db.add_receipt(conn, "register_registration", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_registration", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def list_registrations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT reg_id, speaker, referent, village, copy_id, paths "
            "FROM kin_registrations ORDER BY reg_id"
        ).fetchall()
        return [
            {
                "reg_id": r["reg_id"],
                "speaker": r["speaker"],
                "referent": r["referent"],
                "village": r["village"],
                "copy_id": r["copy_id"],
                "paths": json.loads(r["paths"]),
            }
            for r in rows
        ]

    # ================================================================ 证言

    def add_testimony(
        self,
        testimony_id: str,
        holder: str,
        op_id: str,
        named: bool = False,
        asserted_tree: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """访谈员追加证言。

        - 实名证言（named=True, asserted_tree=T）：在有实名证言时查明实际家系；
          holder='interviewer' 时礼房看不到，不进入礼房的可能世界；
          holder='li_fang' 时礼房可能世界立即只剩 T。
        - 非实名约束（constraints，如 {"order": "A_first"}）：筛掉属性不符的家系。
        接口同时返回按此证言仍可成立的家系，及两份有分歧的具体家系（若存在）。
        """
        if holder not in ("interviewer", "li_fang"):
            raise ValueError("holder must be 'interviewer' or 'li_fang'")
        constraints = constraints or {}
        if named and not asserted_tree:
            raise Rejection("named_testimony_requires_tree")
        payload = {
            "testimony_id": testimony_id,
            "holder": holder,
            "named": named,
            "asserted_tree": asserted_tree,
            "constraints": constraints,
        }
        fp = db.fingerprint("add_testimony", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "add_testimony", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM kin_testimony WHERE testimony_id=?", (testimony_id,)
            ).fetchone():
                raise Rejection("testimony_exists", testimony=testimony_id)
            if asserted_tree is not None and not conn.execute(
                "SELECT 1 FROM kin_trees WHERE tree_id=?", (asserted_tree,)
            ).fetchone():
                raise Rejection("unknown_tree", tree=asserted_tree)
            conn.execute(
                "INSERT INTO kin_testimony(testimony_id, holder, named, asserted_tree, "
                "constraints, op_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    testimony_id, holder, 1 if named else 0, asserted_tree,
                    json.dumps(constraints, ensure_ascii=False), op_id, db.now_iso(),
                ),
            )
            db.record_operation(conn, op_id, "add_testimony", fp)
            tenable = self.tenable_trees(holder)
            divergent = self.divergent_trees(holder)
            result = {
                "status": "ok",
                **payload,
                "tenable_trees": [t["tree_id"] for t in tenable],
                "divergent_pair": [d["tree_id"] for d in divergent[:2]]
                if divergent else [],
            }
            db.add_receipt(conn, "add_testimony", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("add_testimony", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def list_testimony(self, holder: str) -> list[dict[str, Any]]:
        """某岗位只读到自己保管的证言（礼房读不到访谈员持有的实名原谱证言）。"""
        rows = self.conn.execute(
            "SELECT testimony_id, holder, named, asserted_tree, constraints "
            "FROM kin_testimony WHERE holder=? ORDER BY testimony_id",
            (holder,),
        ).fetchall()
        return [
            {
                "testimony_id": r["testimony_id"],
                "holder": r["holder"],
                "named": bool(r["named"]),
                "asserted_tree": r["asserted_tree"],
                "constraints": json.loads(r["constraints"]),
            }
            for r in rows
        ]

    # ================================================================ 认知引擎

    def tenable_trees(
        self,
        holder: str,
        extra_constraints: dict[str, Any] | None = None,
        registration_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """在 holder 岗位可见证据下仍不能排除的家系（可能世界）。

        证据来源：该 holder 保管的证言 + 可选的口述登记（路径可走通）+
        调用方追加的世界属性约束（如礼房已收到的交接字段）。
        约束键 'side' 对应家系的受保护身份列，其余键匹配 attrs。
        实名证言直接把可能世界收缩为所指家系；谱外长辈不参与备选关系。
        """
        extra_constraints = extra_constraints or {}
        side = extra_constraints.get("side")
        attr_only = {k: v for k, v in extra_constraints.items() if k != "side"}
        trees = [self.get_tree(tid) for tid in self.list_trees()]
        if side is not None:
            trees = [x for x in trees if x["side"] == side]
        # 1. 证言
        for t in self.list_testimony(holder):
            if t["named"] and t["asserted_tree"]:
                trees = [x for x in trees if x["tree_id"] == t["asserted_tree"]]
                break
            trees = [x for x in trees if _attrs_match(x["attrs"], t["constraints"])]
        # 2. 调用方追加的累积约束（礼房交接资料 / 单项查询条件）
        if attr_only:
            trees = [x for x in trees if _attrs_match(x["attrs"], attr_only)]
        # 3. 口述登记：每条登记的候选路径至少一条在该家系可走通
        if registration_ids:
            regs = {r["reg_id"]: r for r in self.list_registrations()}
            for rid in registration_ids:
                reg = regs.get(rid)
                if reg is None:
                    raise Rejection("unknown_registration", reg=rid)
                trees = [x for x in trees if self._registration_supports(reg, x)]
        return trees

    def divergent_trees(self, holder: str) -> list[dict[str, Any]]:
        """给出仍可成立但彼此有分歧（side 或属性不同）的具体家系。"""
        tenable = self.tenable_trees(holder)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, a in enumerate(tenable):
            for b in tenable[i + 1:]:
                if a["side"] != b["side"] or a["attrs"] != b["attrs"]:
                    for t in (a, b):
                        if t["tree_id"] not in seen:
                            seen.add(t["tree_id"])
                            out.append(t)
        return out

    def term_valid_in_tree(
        self, copy_id: str, term: str, speaker: str, referent: str, tree_id: str
    ) -> bool:
        """该词在该家系中对 speaker->referent 是否语义正确。

        取副本词条里 term 的全部 pattern，任一可由 speaker 走到 referent 即正确；
        谱外长辈不在谱内，任何谱内路径都无法命中（不参与备选关系）。
        """
        entries = json.loads(
            self.conn.execute(
                "SELECT entries FROM kin_copy_terms WHERE copy_id=?", (copy_id,)
            ).fetchone()["entries"]
        )
        patterns: list[list[list]] = []
        for e in entries:
            if e["term"] == term:
                patterns = e["patterns"]
                break
        if not patterns:
            return False
        tree = self.get_tree(tree_id)
        idx = _person_index(tree)
        for path in patterns:
            if _walk_path(idx, speaker, referent, path):
                return True
        return False

    def viable_terms(
        self, copy_id: str, speaker: str, referent: str, holder: str
    ) -> dict[str, list[str]]:
        """每个仍成立家系 -> 对这对人物语义正确的词条用词。"""
        out: dict[str, list[str]] = {}
        terms = self.get_copy_terms(copy_id)["terms"]
        for tree in self.tenable_trees(holder):
            good = [
                term for term in terms
                if self.term_valid_in_tree(copy_id, term, speaker, referent, tree["tree_id"])
            ]
            out[tree["tree_id"]] = good
        return out

    def _registration_supports(self, reg: dict[str, Any], tree: dict[str, Any]) -> bool:
        idx = _person_index(tree)
        return any(_walk_path(idx, reg["speaker"], reg["referent"], p) for p in reg["paths"])

    # ================================================================ 内部

    def _require_known_copy(self, copy_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM revision_copies WHERE copy_id=? LIMIT 1", (copy_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_copy", copy=copy_id)

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict[str, Any]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, kind, fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(conn, kind, "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result


# ---------------------------------------------------------------------------
# 路径语言与家系校验
# ---------------------------------------------------------------------------

def _norm_path(path: list[Any]) -> list[list]:
    """把 tuple step 规范成 JSON 友好的 list 形式。"""
    return [list(step) for step in path]


def _validate_path(path: list[list]) -> None:
    for step in path:
        if not step or step[0] not in ("up", "sib", "down"):
            raise Rejection("bad_path_step", step=step)
        if step[0] == "up" and (len(step) != 2 or step[1] not in ("father", "mother")):
            raise Rejection("bad_path_step", step=step)
        if step[0] == "sib":
            if len(step) > 2 or (len(step) == 2 and step[1] not in ("elder", "younger")):
                raise Rejection("bad_path_step", step=step)
        if step[0] == "down" and len(step) != 1:
            raise Rejection("bad_path_step", step=step)


def _norm_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"term": e["term"], "patterns": [_norm_path(p) for p in e["patterns"]]}
        for e in entries
    ]


def _validate_entries(entries: list[dict[str, Any]]) -> None:
    if not entries:
        raise Rejection("empty_entries")
    terms = [e["term"] for e in entries]
    if len(set(terms)) != len(terms):
        raise Rejection("duplicate_term")
    for e in entries:
        if not e.get("term") or not e.get("patterns"):
            raise Rejection("bad_entry", entry=e)
        for p in e["patterns"]:
            _validate_path(_norm_path(p))


def _norm_persons(persons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in persons:
        out.append(
            {
                "id": p["id"],
                "father": p.get("father"),
                "mother": p.get("mother"),
                "seniority": p.get("seniority"),
                "off_pedigree": bool(p.get("off_pedigree", False)),
            }
        )
    return out


def _validate_tree(tree_id: str, persons: list[dict[str, Any]]) -> None:
    if not persons:
        raise Rejection("empty_tree", tree=tree_id)
    ids = [p["id"] for p in persons]
    if len(set(ids)) != len(ids):
        raise Rejection("duplicate_person", tree=tree_id)
    id_set = set(ids)
    # 父、母各至多一人由单行单字段天然保证；这里校验终点与谱外规则
    for p in persons:
        if p["off_pedigree"]:
            if p["father"] is not None or p["mother"] is not None:
                raise Rejection("off_pedigree_has_parent", tree=tree_id, person=p["id"])
            continue
        for parent in (p["father"], p["mother"]):
            if parent is not None and parent not in id_set:
                raise Rejection(
                    "parent_missing", tree=tree_id, person=p["id"], parent=parent
                )
            if parent == p["id"]:
                raise Rejection("kinship_cycle", tree=tree_id, at=p["id"])
    _assert_kinship_acyclic(persons, tree_id)


def _assert_kinship_acyclic(persons: list[dict[str, Any]], tree_id: str) -> None:
    """三色 DFS 沿父/母边查环：有环（含自环）整批拒绝，不允许成为自己的祖先。"""
    idx = {p["id"]: p for p in persons}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {pid: WHITE for pid in idx}

    def edges(pid: str) -> list[str]:
        p = idx[pid]
        return [x for x in (p["father"], p["mother"]) if x is not None]

    for start in idx:
        if color[start] != WHITE:
            continue
        stack = [(start, 0)]
        while stack:
            node, i = stack[-1]
            color[node] = GRAY
            nxt_edges = edges(node)
            if i == len(nxt_edges):
                color[node] = BLACK
                stack.pop()
                continue
            child = nxt_edges[i]
            stack[-1] = (node, i + 1)
            if color.get(child, BLACK) == GRAY:
                raise Rejection("kinship_cycle", tree=tree_id, at=child)
            if color.get(child, BLACK) == WHITE:
                stack.append((child, 0))


def _attrs_match(attrs: dict[str, Any], constraints: dict[str, Any]) -> bool:
    return all(attrs.get(k) == v for k, v in constraints.items())


def _person_index(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in tree["persons"]}


def _walk_path(
    idx: dict[str, dict[str, Any]],
    speaker: str,
    referent: str,
    path: list[list],
) -> bool:
    """从 speaker 按路径行走，终点集合含 referent 即命中。

    up 指定父/母（缺失即该路径不通）；sib 在同父母同胞中按已登记长幼顺序展开；
    down 展开全部子女。每一步都是集合展开，一条路径可对应多个人。
    """
    if speaker not in idx or referent not in idx:
        return False
    if idx[speaker]["off_pedigree"] or idx[referent]["off_pedigree"]:
        return False  # 谱外长辈不参与备选关系
    current = {speaker}
    for step in path:
        nxt: set[str] = set()
        kind = step[0]
        for pid in current:
            p = idx.get(pid)
            if p is None or p["off_pedigree"]:
                continue
            if kind == "up":
                parent = p["father"] if step[1] == "father" else p["mother"]
                if parent is not None and parent in idx:
                    nxt.add(parent)
            elif kind == "sib":
                # 长幼顺序由 seniority 支持：elder 只取序号更小的同胞，
                # younger 取序号更大；无标记则取全部同胞。任一比较方缺
                # seniority 时该带标记路径不成立（不能凭空认定长幼）。
                rank = step[1] if len(step) > 1 else None
                for other_id, other in idx.items():
                    if other_id == pid or other["off_pedigree"]:
                        continue
                    if not (
                        other["father"] is not None
                        and other["father"] == p["father"]
                        and other["mother"] is not None
                        and other["mother"] == p["mother"]
                    ):
                        continue
                    if rank is None:
                        nxt.add(other_id)
                    elif p["seniority"] is None or other["seniority"] is None:
                        continue
                    elif rank == "elder" and other["seniority"] < p["seniority"]:
                        nxt.add(other_id)
                    elif rank == "younger" and other["seniority"] > p["seniority"]:
                        nxt.add(other_id)
            elif kind == "down":
                for other_id, other in idx.items():
                    if other["off_pedigree"]:
                        continue
                    if other["father"] == pid or other["mother"] == pid:
                        nxt.add(other_id)
        current = nxt
        if not current:
            return False
    return referent in current
