"""门槛与联号约束下的最多可发牌求解。

约束：
- 一个有效码最多盖给一张牌；
- 每牌有门槛（需码数）与可接受发行名（再版按各自归并许可决定能否计入）；
- 联号组内的码要么全盖同一张牌、要么全不盖，不可拆；
- 场次规模上限：十二包五牌。
同数任选：取“牌数最多”的方案，平局时按稳定字典序给出一个真实盖码单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .reconcile import BookletEntry, MAX_PLAQUES


@dataclass(frozen=True)
class PlaqueSpec:
    plaque_id: str
    threshold: int
    accepted_releases: frozenset[str]  # 含允许归并进来的再版发行 id


@dataclass
class Allocation:
    plaque_id: str
    codes: list[str]


def _code_eligible(entry: BookletEntry, spec: PlaqueSpec) -> bool:
    if entry.status != "eligible":
        return False
    return bool(set(entry.eligible_releases) & spec.accepted_releases)


def max_plaques(
    entries: list[BookletEntry],
    specs: list[PlaqueSpec],
    linkage: dict[str, tuple[str, ...]] | None = None,
    max_plaques: int = MAX_PLAQUES,
) -> list[Allocation]:
    """回溯求最多可发牌，返回每张牌的真实盖码单（含同数任选的确定选择）。

    linkage: 组号 -> 该组全部码；组内码不可拆，必须同盖一张牌或全不盖。
    """
    linkage = linkage or {}
    code_to_group: dict[str, str] = {}
    for gid, codes in linkage.items():
        for c in codes:
            code_to_group[c] = gid

    eligible_entries = [e for e in entries if e.status == "eligible"]
    specs = sorted(specs, key=lambda s: s.plaque_id)
    if len(specs) > max_plaques:
        specs = specs[:max_plaques]

    best: list[Allocation] = []

    def can_assign_group(group_codes: tuple[str, ...], spec: PlaqueSpec) -> bool:
        for c in group_codes:
            ent = next((e for e in eligible_entries if e.code == c), None)
            if ent is None or not _code_eligible(ent, spec):
                return False
        return True

    def dfs(spec_idx: int, used: set[str], chosen: list[Allocation]) -> None:
        nonlocal best
        if len(chosen) + (len(specs) - spec_idx) <= len(best):
            return  # 上界剪枝：不可能超过当前最优
        if spec_idx == len(specs):
            if len(chosen) > len(best):
                best = [Allocation(a.plaque_id, list(a.codes)) for a in chosen]
            return

        spec = specs[spec_idx]
        # 候选原子：联号组（整组）或独立码
        atoms: list[tuple[str, ...]] = []
        seen_groups: set[str] = set()
        singles: list[str] = []
        for ent in eligible_entries:
            if ent.code in used or not _code_eligible(ent, spec):
                continue
            gid = code_to_group.get(ent.code)
            if gid is not None:
                if gid in seen_groups:
                    continue
                group = tuple(sorted(linkage[gid]))
                if can_assign_group(group, spec):
                    atoms.append(group)
                seen_groups.add(gid)
            else:
                singles.append(ent.code)
                atoms.append((ent.code,))

        # 先尝试给这张牌凑满门槛（原子按字典序，保证同数任选稳定）
        atoms.sort(key=lambda a: (len(a), a))
        pick: list[str] = []

        def fill(atom_idx: int) -> bool:
            if len(pick) >= spec.threshold:
                return True
            if atom_idx == len(atoms):
                return False
            for j in range(atom_idx, len(atoms)):
                atom = atoms[j]
                if any(c in used or c in pick for c in atom):
                    continue
                pick.extend(atom)
                if fill(j + 1):
                    return True
                for _ in atom:
                    pick.pop()
            return False

        if fill(0):
            new_used = used | set(pick)
            chosen.append(Allocation(spec.plaque_id, sorted(pick)))
            dfs(spec_idx + 1, new_used, chosen)
            chosen.pop()
        # 也尝试不发这张牌（可能让后面的牌凑出更多总数）
        dfs(spec_idx + 1, used, chosen)

    dfs(0, set(), [])
    return best
