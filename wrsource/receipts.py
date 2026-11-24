"""Tasting receipts and the preference desk.

A *receipt* (小票) is the JSON body of a weighted-source row.  Its schema is::

    {"buyer": "<group id of one fictional buyer>",
     "visible": ["tea-a", "tea-b"],
     "picked": "tea-a"}

meaning: at that moment exactly ``visible`` were in stock and on the buyer's
shelf, and the buyer finally took ``picked``.  Every buyer owns one fixed but
unknown strict total order over tea varieties and always takes the most
preferred variety currently visible.  One receipt therefore contributes the
constraints

    picked > v        for every v in visible with v != picked

(``>`` means "strictly preferred to").  Only receipts that the sampler
*actually selected* may testify: the desk is built from dataset records, never
from the whole source.

The desk answers three questions about a pair of varieties:

* ``must_precede(x, y)``  -- every compatible strict order puts x before y;
* ``can_precede(x, y)``   -- at least one compatible order puts x before y;
* ``find_cycle``          -- a ring of constraints that no strict order can
  satisfy, together with the receipts proving each edge.

Constraints are kept per buyer (each buyer has their own order) and are also
merged into one desk-wide graph: the desk's pair questions default to the
merged view, while the cabinet scopes compatibility to a single buyer.  Each
individual receipt is always satisfiable; merging several receipts can ring.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import permutations
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MERGED_SCOPE = "*"


class ReceiptError(ValueError):
    """Raised when a receipt body is malformed."""


class PreferenceCycleError(RuntimeError):
    """Raised when constraints contain a directed cycle."""

    def __init__(self, cycle: Sequence[str], evidence: Sequence[dict]):
        self.cycle = list(cycle)
        self.evidence = list(evidence)
        super().__init__(f"preference cycle: {' -> '.join(self.cycle)}")


# ---------------------------------------------------------------------------
# Receipt bodies
# ---------------------------------------------------------------------------

def make_receipt_body(buyer: str, visible: Sequence[str], picked: str) -> str:
    """Canonical receipt body text (stored as a source row body)."""
    body = {
        "buyer": buyer,
        "visible": list(visible),
        "picked": picked,
    }
    # Validate before serialising.
    parse_receipt(body)
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def parse_receipt(body) -> dict:
    """Parse and validate a receipt body (dict or JSON text)."""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8")
    if isinstance(body, str):
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ReceiptError(f"receipt body is not valid JSON: {exc}") from exc
    else:
        obj = body
    if not isinstance(obj, dict):
        raise ReceiptError("receipt body must be a JSON object")
    buyer = obj.get("buyer")
    visible = obj.get("visible")
    picked = obj.get("picked")
    if not isinstance(buyer, str) or not buyer:
        raise ReceiptError("receipt 'buyer' must be a non-empty string")
    if not isinstance(visible, list) or not visible:
        raise ReceiptError("receipt 'visible' must be a non-empty list")
    if not all(isinstance(v, str) and v for v in visible):
        raise ReceiptError("every visible variety must be a non-empty string")
    if len(set(visible)) != len(visible):
        raise ReceiptError(f"duplicate visible varieties in receipt of {buyer}")
    if not isinstance(picked, str) or not picked:
        raise ReceiptError("receipt 'picked' must be a non-empty string")
    if picked not in visible:
        raise ReceiptError(
            f"receipt of {buyer}: picked {picked!r} was not visible {visible!r}")
    return {"buyer": buyer, "visible": list(visible), "picked": picked}


@dataclass(frozen=True)
class Ticket:
    """One sampled receipt that is allowed to testify."""

    row_id: str
    buyer: str
    visible: Tuple[str, ...]
    picked: str
    source_version: int
    body_sha256: str

    def as_dict(self) -> dict:
        return {
            "row_id": self.row_id,
            "buyer": self.buyer,
            "visible": list(self.visible),
            "picked": self.picked,
            "source_version": self.source_version,
            "body_sha256": self.body_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping) -> "Ticket":
        """Build a ticket from one ``samples.jsonl`` record."""
        parsed = parse_receipt(record["body"])
        return cls(
            row_id=record["row_id"],
            buyer=parsed["buyer"],
            visible=tuple(parsed["visible"]),
            picked=parsed["picked"],
            source_version=int(record["source_version"]),
            body_sha256=record["body_sha256"],
        )


def tickets_from_records(records: Iterable[Mapping]) -> List[Ticket]:
    """Parse every sampled record; unsampled source rows never reach here."""
    return [Ticket.from_record(r) for r in records]


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _topo_sort(nodes: Sequence[str], edges: Sequence[Tuple[str, str]]
               ) -> List[str]:
    """Deterministic Kahn sort; raises PreferenceCycleError on a cycle."""
    node_set = set(nodes)
    adj: Dict[str, set] = {n: set() for n in node_set}
    indeg: Dict[str, int] = {n: 0 for n in node_set}
    for x, y in edges:
        if x not in node_set or y not in node_set or x == y:
            continue
        if y not in adj[x]:
            adj[x].add(y)
            indeg[y] += 1
    ready = sorted(n for n in node_set if indeg[n] == 0)
    out: List[str] = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    if len(out) != len(node_set):
        remaining = sorted(node_set - set(out))
        raise PreferenceCycleError(remaining, [])
    return out


def _find_vertex_cycle(nodes: Sequence[str],
                       edges: Sequence[Tuple[str, str]]) -> Optional[List[str]]:
    """Return a vertex list describing one directed cycle, or None."""
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for x, y in edges:
        if x in adj and y in adj:
            adj[x].append(y)
    color: Dict[str, int] = {n: 0 for n in nodes}
    stack: List[str] = []

    def dfs(u: str) -> Optional[List[str]]:
        color[u] = 1
        stack.append(u)
        for v in adj[u]:
            if color[v] == 0:
                found = dfs(v)
                if found is not None:
                    return found
            elif color[v] == 1:
                idx = stack.index(v)
                return stack[idx:] + [v]
        stack.pop()
        color[u] = 2
        return None

    for n in sorted(nodes):
        if color[n] == 0:
            found = dfs(n)
            if found is not None:
                return found
    return None


def _reach(nodes: Sequence[str], edges: Sequence[Tuple[str, str]],
           start: str) -> set:
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for x, y in edges:
        if x in adj and y in adj:
            adj[x].append(y)
    seen = set()
    stack = [start] if start in adj else []
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


# ---------------------------------------------------------------------------
# Preference desk
# ---------------------------------------------------------------------------

class PreferenceDesk:
    """Constraint graph over the receipts actually selected by the sampler."""

    def __init__(self, tickets: Sequence[Ticket],
                 varieties: Optional[Sequence[str]] = None):
        self.tickets: List[Ticket] = list(tickets)
        self._edges: Dict[str, set] = {}
        # (scope, x, y) -> ticket row_ids proving the edge
        self._provenance: Dict[Tuple[str, str, str], List[str]] = {}
        found_varieties = set()
        for t in self.tickets:
            found_varieties.update(t.visible)
        if varieties is None:
            varieties = sorted(found_varieties)
        self.varieties: Tuple[str, ...] = tuple(varieties)
        for t in self.tickets:
            for v in t.visible:
                if v == t.picked:
                    continue
                self._add_edge(t.buyer, t.picked, v, t.row_id)
                self._add_edge(MERGED_SCOPE, t.picked, v, t.row_id)

    def _add_edge(self, scope: str, x: str, y: str, row_id: str) -> None:
        self._edges.setdefault(scope, set()).add((x, y))
        key = (scope, x, y)
        prov = self._provenance.setdefault(key, [])
        if row_id not in prov:
            prov.append(row_id)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_records(cls, records: Iterable[Mapping],
                     varieties: Optional[Sequence[str]] = None
                     ) -> "PreferenceDesk":
        return cls(tickets_from_records(records), varieties)

    @property
    def buyers(self) -> List[str]:
        return sorted({t.buyer for t in self.tickets})

    def tickets_of(self, buyer: str) -> List[Ticket]:
        return [t for t in self.tickets if t.buyer == buyer]

    def edges(self, buyer: Optional[str] = None
              ) -> List[Tuple[str, str, List[str]]]:
        """Constraint edges with provenance ticket row ids."""
        scope = MERGED_SCOPE if buyer is None else buyer
        out = []
        for x, y in sorted(self._edges.get(scope, set())):
            out.append((x, y, list(self._provenance[(scope, x, y)])))
        return out

    def _scope_edges(self, buyer: Optional[str]) -> List[Tuple[str, str]]:
        scope = MERGED_SCOPE if buyer is None else buyer
        return list(self._edges.get(scope, set()))

    # -- pair questions --------------------------------------------------
    def must_precede(self, x: str, y: str, buyer: Optional[str] = None
                     ) -> bool:
        """True iff every compatible strict order ranks x before y."""
        if x == y:
            raise ValueError("compare two distinct varieties")
        if x not in self.varieties or y not in self.varieties:
            return False
        return y in _reach(self.varieties, self._scope_edges(buyer), x)

    def can_precede(self, x: str, y: str, buyer: Optional[str] = None
                    ) -> bool:
        """True iff some compatible strict order ranks x before y."""
        if x == y:
            raise ValueError("compare two distinct varieties")
        if x not in self.varieties or y not in self.varieties:
            return False
        return not self.must_precede(y, x, buyer)

    def relation(self, x: str, y: str, buyer: Optional[str] = None) -> str:
        """One of ``x_first`` / ``y_first`` / ``both_possible``."""
        x_first = self.must_precede(x, y, buyer)
        y_first = self.must_precede(y, x, buyer)
        if x_first and y_first:
            cyc = self.find_cycle(buyer)
            raise PreferenceCycleError(cyc or [x, y],
                                       self.cycle_evidence(buyer))
        if x_first:
            return "x_first"
        if y_first:
            return "y_first"
        return "both_possible"

    # -- cycles ----------------------------------------------------------
    def find_cycle(self, buyer: Optional[str] = None
                   ) -> Optional[List[str]]:
        """A directed cycle of varieties, or None when acyclic."""
        return _find_vertex_cycle(self.varieties, self._scope_edges(buyer))

    def cycle_evidence(self, buyer: Optional[str] = None) -> List[dict]:
        """The ring expanded into edges plus the receipts proving each."""
        cycle = self.find_cycle(buyer)
        if cycle is None:
            return []
        scope = MERGED_SCOPE if buyer is None else buyer
        evidence = []
        for x, y in zip(cycle, cycle[1:]):
            evidence.append({
                "preferred": x,
                "over": y,
                "ticket_row_ids": list(self._provenance.get(
                    (scope, x, y), [])),
            })
        return evidence

    def per_buyer_cycles(self) -> Dict[str, List[str]]:
        """Cycles inside one buyer's own evidence (hard contradictions)."""
        out: Dict[str, List[str]] = {}
        for b in self.buyers:
            cyc = self.find_cycle(b)
            if cyc is not None:
                out[b] = cyc
        return out

    # -- compatibility ---------------------------------------------------
    def _buyer_edges(self, buyer: str) -> List[Tuple[str, str]]:
        return self._scope_edges(buyer)

    def satisfiable(self, buyer: str,
                    extra_edges: Sequence[Tuple[str, str]] = ()) -> bool:
        """True iff the buyer's evidence plus extra edges has a strict order."""
        edges = self._buyer_edges(buyer) + list(extra_edges)
        try:
            _topo_sort(self.varieties, edges)
        except PreferenceCycleError:
            return False
        return True

    def witness_order(self, buyer: str,
                      extra_edges: Sequence[Tuple[str, str]] = (),
                      varieties: Optional[Sequence[str]] = None
                      ) -> Tuple[str, ...]:
        """A concrete strict total order compatible with the buyer's evidence.

        Raises :class:`PreferenceCycleError` when no such order exists.
        """
        nodes = tuple(varieties) if varieties is not None else self.varieties
        edges = self._buyer_edges(buyer) + list(extra_edges)
        return tuple(_topo_sort(nodes, edges))

    def compatible_orders(self, buyer: str,
                          varieties: Optional[Sequence[str]] = None
                          ) -> List[Tuple[str, ...]]:
        """All strict total orders of ``varieties`` fitting the buyer."""
        nodes = tuple(varieties) if varieties is not None else self.varieties
        edges = self._buyer_edges(buyer)
        edge_set = {(x, y) for x, y in edges
                    if x in nodes and y in nodes}
        out = []
        for order in permutations(nodes):
            rank = {v: i for i, v in enumerate(order)}
            if all(rank[x] < rank[y] for x, y in edge_set):
                out.append(order)
        return out

    def possible_picks(self, buyer: str, options: Sequence[str]
                       ) -> List[str]:
        """Varieties in ``options`` that can be this buyer's top choice.

        A variety v is pickable iff some compatible order ranks v above every
        other currently available option.
        """
        opts = [v for v in dict.fromkeys(options) if v in self.varieties]
        if not opts:
            return []
        picks = []
        for v in opts:
            extra = [(v, u) for u in opts if u != v]
            if self.satisfiable(buyer, extra):
                picks.append(v)
        return picks
