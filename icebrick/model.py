"""备料台：层厚整数约束、量条/同模厚差、冻结快照与读法枚举。

一条砖只登记 *从左至右的几层口味* 与 *整砖总格数*；各层厚度是至少 1 的整数，
层厚之和必须等于总格数。量条有两种：

* :class:`SumSlip`  —— 同一砖一段相邻层（按层号，含两端）的厚度和；
* :class:`DiffSlip` —— 两块 *同版模具* 砖各自指定层的厚度差
  （``thick(a, ia) - thick(b, ib) = value``）。

模型在 :meth:`IceModel.freeze` 时做完全部引用核对并算出内容哈希，产出不可变的
:class:`FrozenModel`；量条及其引用的格号都随这一份冻结快照固定，后续任何规划只
面对快照。可行读法（给每层一个具体厚度的完整整数赋值）在题面规模内不超过六种，
枚举超过六种会显式抛错而不是静默截断。
"""

from dataclasses import dataclass

from budget_solver.util import content_hash, utc_now_iso
from icebrick.errors import IcebrickError, InfeasibleModelError, TooManyReadingsError

# 题面承诺：完整可行读法不超过六种。枚举到第七种即说明输入超出约定规模。
MAX_READINGS = 6


# --------------------------------------------------------------------------- 数据
@dataclass(frozen=True)
class Layer:
    """一层口味；厚度在读法里赋值，模型阶段未知。"""

    index: int          # 从 0 起的层号
    color: str


@dataclass(frozen=True)
class Brick:
    """一条冰砖：层口味序列 + 整砖总格数，可选同版模具签。"""

    brick_id: str
    layers: tuple[Layer, ...]
    total_length: int
    mold_id: str | None = None

    @property
    def layer_count(self):
        return len(self.layers)

    def color_of(self, layer_index):
        return self.layers[layer_index].color


@dataclass(frozen=True)
class SumSlip:
    """量条：brick 的 [layer_from, layer_to] 相邻层厚度和等于 value。"""

    slip_id: str
    brick_id: str
    layer_from: int
    layer_to: int
    value: int

    def variables(self):
        return [(self.brick_id, i) for i in range(self.layer_from, self.layer_to + 1)]


@dataclass(frozen=True)
class DiffSlip:
    """量条：同模两砖指定层厚差 thick(a,ia)-thick(b,ib)=value。"""

    slip_id: str
    brick_a: str
    layer_a: int
    brick_b: str
    layer_b: int
    value: int

    def variables(self):
        return [(self.brick_a, self.layer_a), (self.brick_b, self.layer_b)]


@dataclass(frozen=True)
class Reading:
    """一份完整可行读法：(砖 id, 层号) -> 厚度的不可变整数赋值。"""

    thicknesses: tuple[tuple[tuple[str, int], int], ...]
    bricks: tuple[Brick, ...]

    def thick(self, brick_id, layer_index):
        return dict(self.thicknesses)[(brick_id, layer_index)]

    def brick_thicknesses(self, brick_id):
        return tuple(v for (b, i), v in self.thicknesses if b == brick_id)

    def cell_colors(self, brick_id):
        """把层厚展开成逐格颜色（左至右）。"""
        brick = next(b for b in self.bricks if b.brick_id == brick_id)
        out = []
        for layer in brick.layers:
            out.extend([layer.color] * self.thick(brick_id, layer.index))
        return tuple(out)

    def color_at(self, brick_id, position):
        """position 为从 0 起的整砖格号。"""
        return self.cell_colors(brick_id)[position]

    def as_dict(self):
        return {
            brick.brick_id: list(self.brick_thicknesses(brick.brick_id))
            for brick in self.bricks
        }

    def __str__(self):
        parts = []
        for brick in self.bricks:
            th = self.brick_thicknesses(brick.brick_id)
            seq = "".join(
                f"{layer.color}{t}" for layer, t in zip(brick.layers, th)
            )
            parts.append(f"{brick.brick_id}=[{seq}]")
        return "读法(" + " ".join(parts) + ")"


@dataclass(frozen=True)
class ContradictionWitness:
    """矛盾凭据：最小化后仍不可满足的量条子集 + 传播到的空界/违例说明。"""

    slip_ids: tuple[str, ...]
    reason: str
    bounds: tuple[tuple[str, int, int, int], ...]  # (变量名, 下界, 上界, 固定值或-1)
    violated: tuple[str, ...]

    def describe(self):
        lines = [f"矛盾凭据：仅用量条 {list(self.slip_ids)} 已不可满足。", f"  依据：{self.reason}"]
        for name, lo, hi, fixed in self.bounds:
            tag = f"（固定 {fixed}）" if fixed >= 0 else ""
            lines.append(f"  界 {name}: [{lo}, {hi}]{tag}")
        for v in self.violated:
            lines.append(f"  违例量条：{v}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 建模
class IceModel:
    """可增量构建的冰砖约束模型；:meth:`freeze` 后得到不可变快照。"""

    def __init__(self):
        self._bricks: dict[str, Brick] = {}
        self._sum_slips: list[SumSlip] = []
        self._diff_slips: list[DiffSlip] = []
        self._slip_ids: set[str] = set()

    def add_brick(self, brick_id, colors, total_length, mold_id=None):
        if brick_id in self._bricks:
            raise IcebrickError(f"砖 {brick_id} 重复登记")
        if not isinstance(colors, (list, tuple)) or not colors:
            raise IcebrickError(f"砖 {brick_id} 至少要有一层口味")
        if any(not isinstance(c, str) or not c for c in colors):
            raise IcebrickError(f"砖 {brick_id} 口味必须是非空字符串")
        if not isinstance(total_length, int) or total_length < len(colors):
            raise IcebrickError(
                f"砖 {brick_id} 总格数 {total_length} 小于层数 {len(colors)}"
            )
        if mold_id is not None and not isinstance(mold_id, str):
            raise IcebrickError("mold_id 必须是字符串或 None")
        layers = tuple(Layer(index=i, color=c) for i, c in enumerate(colors))
        self._bricks[brick_id] = Brick(brick_id, layers, total_length, mold_id)
        return self

    def add_sum_slip(self, slip_id, brick_id, layer_from, layer_to, value):
        self._require_slip_id(slip_id)
        brick = self._bricks.get(brick_id)
        if brick is None:
            raise IcebrickError(f"量条 {slip_id} 引用不存在的砖 {brick_id}")
        if not (0 <= layer_from <= layer_to < brick.layer_count):
            raise IcebrickError(
                f"量条 {slip_id} 层号 [{layer_from},{layer_to}] 越界（砖 {brick_id} "
                f"共 {brick.layer_count} 层）"
            )
        if not isinstance(value, int) or value < (layer_to - layer_from + 1):
            raise IcebrickError(f"量条 {slip_id} 的和 {value} 小于每层至少 1")
        self._sum_slips.append(
            SumSlip(slip_id, brick_id, layer_from, layer_to, value)
        )
        return self

    def add_diff_slip(self, slip_id, brick_a, layer_a, brick_b, layer_b, value):
        self._require_slip_id(slip_id)
        ba, bb = self._bricks.get(brick_a), self._bricks.get(brick_b)
        if ba is None or bb is None:
            raise IcebrickError(f"量条 {slip_id} 引用了不存在的砖")
        if ba.mold_id is None or ba.mold_id != bb.mold_id:
            raise IcebrickError(
                f"量条 {slip_id} 要求 {brick_a} 与 {brick_b} 同版模具"
            )
        if not (0 <= layer_a < ba.layer_count and 0 <= layer_b < bb.layer_count):
            raise IcebrickError(f"量条 {slip_id} 层号越界")
        if not isinstance(value, int):
            raise IcebrickError(f"量条 {slip_id} 厚差必须是整数")
        self._diff_slips.append(
            DiffSlip(slip_id, brick_a, layer_a, brick_b, layer_b, value)
        )
        return self

    def _require_slip_id(self, slip_id):
        if not isinstance(slip_id, str) or not slip_id:
            raise IcebrickError("量条 id 必须是非空字符串")
        if slip_id in self._slip_ids:
            raise IcebrickError(f"量条 id {slip_id} 重复")
        self._slip_ids.add(slip_id)

    def freeze(self, label=None):
        bricks = tuple(sorted(self._bricks.values(), key=lambda b: b.brick_id))
        sum_slips = tuple(self._sum_slips)
        diff_slips = tuple(self._diff_slips)
        payload = {
            "label": label,
            "bricks": [
                {
                    "brick_id": b.brick_id,
                    "colors": [l.color for l in b.layers],
                    "total_length": b.total_length,
                    "mold_id": b.mold_id,
                }
                for b in bricks
            ],
            "sum_slips": [s.__dict__ for s in sum_slips],
            "diff_slips": [s.__dict__ for s in diff_slips],
        }
        return FrozenModel(
            bricks=bricks,
            sum_slips=sum_slips,
            diff_slips=diff_slips,
            label=label,
            content_hash=content_hash(payload),
            frozen_at=utc_now_iso(),
        )


# --------------------------------------------------------------------------- 快照
@dataclass(frozen=True)
class FrozenModel:
    """不可变冻结快照：规划侧唯一可见的事实来源。"""

    bricks: tuple[Brick, ...]
    sum_slips: tuple[SumSlip, ...]
    diff_slips: tuple[DiffSlip, ...]
    label: str | None
    content_hash: str
    frozen_at: str

    def brick(self, brick_id):
        for b in self.bricks:
            if b.brick_id == brick_id:
                return b
        raise IcebrickError(f"快照内无砖 {brick_id}")

    # ------------------------------------------------------------- 约束传播内核
    def _initial_domains(self):
        domains = {}
        for b in self.bricks:
            n = b.layer_count
            for layer in b.layers:
                # 单层最多占到“其余 n-1 层各留 1 格”
                domains[(b.brick_id, layer.index)] = [1, b.total_length - (n - 1)]
        return domains

    def _all_sum_constraints(self):
        """砖自身的“层厚和=总格数”也作为一条全幅和约束参与传播。"""
        constraints = list(self.sum_slips)
        for b in self.bricks:
            constraints.append(
                SumSlip(
                    slip_id=f"__total__{b.brick_id}",
                    brick_id=b.brick_id,
                    layer_from=0,
                    layer_to=b.layer_count - 1,
                    value=b.total_length,
                )
            )
        return tuple(constraints)

    def _propagate(self, domains, assignment):
        """区间传播到不动点。返回收缩后的 domains，或 None 表示已证矛盾。"""
        domains = {k: list(v) for k, v in domains.items()}
        for var, val in assignment.items():
            domains[var] = [val, val]
        sum_constraints = self._all_sum_constraints()
        changed = True
        while changed:
            changed = False
            # 和约束：min/max 可行性 + 单变量收紧
            for slip in sum_constraints:
                vars_ = slip.variables()
                lo_sum = hi_sum = 0
                free = []
                for var in vars_:
                    lo, hi = domains[var]
                    if lo > hi:
                        return None
                    if lo == hi:
                        lo_sum += lo
                        hi_sum += hi
                    else:
                        free.append(var)
                        lo_sum += lo
                        hi_sum += hi
                if lo_sum > slip.value or hi_sum < slip.value:
                    return None
                if len(free) == 1:
                    var = free[0]
                    fixed = slip.value - (lo_sum - domains[var][0])
                    lo, hi = domains[var]
                    new_lo, new_hi = max(lo, fixed), min(hi, fixed)
                    if new_lo > new_hi:
                        return None
                    if (new_lo, new_hi) != (lo, hi):
                        domains[var] = [new_lo, new_hi]
                        changed = True
                elif not free and lo_sum != slip.value:
                    return None
            # 厚差约束：x - y = v，双向收界
            for slip in self.diff_slips:
                x = (slip.brick_a, slip.layer_a)
                y = (slip.brick_b, slip.layer_b)
                xlo, xhi = domains[x]
                ylo, yhi = domains[y]
                nx = (max(xlo, ylo + slip.value), min(xhi, yhi + slip.value))
                ny = (max(ylo, xlo - slip.value), min(yhi, xhi - slip.value))
                if nx[0] > nx[1] or ny[0] > ny[1]:
                    return None
                if list(nx) != domains[x]:
                    domains[x] = list(nx)
                    changed = True
                if list(ny) != domains[y]:
                    domains[y] = list(ny)
                    changed = True
        return domains

    def _is_feasible(self, assignment=()):
        domains = self._propagate(self._initial_domains(), dict(assignment))
        if domains is None:
            return False
        return all(lo <= hi for lo, hi in domains.values())

    # --------------------------------------------------------------- 读法枚举
    def enumerate_readings(self):
        """枚举全部完整可行读法；超过 :data:`MAX_READINGS` 显式报错。"""
        base = self._initial_domains()
        propagated = self._propagate(base, {})
        if propagated is None:
            raise InfeasibleModelError(self.contradiction_witness())
        variables = sorted(propagated.keys())
        readings = []
        assignment = {}

        def search():
            if len(readings) > MAX_READINGS:
                return
            domains = self._propagate(propagated, assignment)
            if domains is None:
                return
            branched = None
            for var in variables:
                lo, hi = domains[var]
                if lo < hi:
                    branched = (var, lo, hi)
                    break
            if branched is None:
                # 全部固定：核对全部和约束（传播已保证，防御性复算）
                values = tuple(
                    sorted(
                        ((var, domains[var][0]) for var in variables),
                        key=lambda kv: (kv[0][0], kv[0][1]),
                    )
                )
                reading = Reading(thicknesses=tuple(values), bricks=self.bricks)
                if self._reading_satisfies_everything(reading):
                    readings.append(reading)
                return
            var, lo, hi = branched
            for val in range(lo, hi + 1):
                assignment[var] = val
                search()
                del assignment[var]
                if len(readings) > MAX_READINGS:
                    raise TooManyReadingsError(
                        f"可行读法超过 {MAX_READINGS} 种，输入超出题面约定规模"
                    )

        search()
        if not readings:
            raise InfeasibleModelError(self.contradiction_witness())
        return tuple(readings)

    def _reading_satisfies_everything(self, reading):
        for slip in self._all_sum_constraints():
            total = sum(reading.thick(*var) for var in slip.variables())
            if total != slip.value:
                return False
        for slip in self.diff_slips:
            if (
                reading.thick(slip.brick_a, slip.layer_a)
                - reading.thick(slip.brick_b, slip.layer_b)
                != slip.value
            ):
                return False
        return True

    # --------------------------------------------------------------- 矛盾凭据
    def contradiction_witness(self):
        """贪心删条求最小（按包含序的局部最小）不可满足量条子集，并附传播说明。"""
        all_slips = [f"sum:{s.slip_id}" for s in self.sum_slips] + [
            f"diff:{s.slip_id}" for s in self.diff_slips
        ]
        if self._is_feasible():
            raise IcebrickError("模型本身可满足，不存在矛盾凭据")
        kept = list(all_slips)
        for slip in list(kept):
            trial = [s for s in kept if s != slip]
            if not self._feasible_with(trial):
                kept = trial  # 删掉后仍矛盾 -> 该条不是必要凭据
        domains, violated = self._propagation_report(kept)
        reason = self._explain_empty_domain(domains, violated)
        bounds = tuple(
            sorted(
                (
                    f"{var[0]}::层{var[1]}",
                    lo,
                    hi,
                    lo if lo == hi else -1,
                )
                for var, (lo, hi) in domains.items()
            )
        )
        return ContradictionWitness(
            slip_ids=tuple(kept),
            reason=reason,
            bounds=bounds,
            violated=tuple(violated),
        )

    def _feasible_with(self, slip_tags):
        sum_ids = {t[4:] for t in slip_tags if t.startswith("sum:")}
        diff_ids = {t[5:] for t in slip_tags if t.startswith("diff:")}
        saved_sum, saved_diff = self.sum_slips, self.diff_slips
        object.__setattr__(
            self,
            "sum_slips",
            tuple(s for s in saved_sum if s.slip_id in sum_ids),
        )
        object.__setattr__(
            self,
            "diff_slips",
            tuple(s for s in saved_diff if s.slip_id in diff_ids),
        )
        try:
            return self._is_feasible()
        finally:
            object.__setattr__(self, "sum_slips", saved_sum)
            object.__setattr__(self, "diff_slips", saved_diff)

    def _propagation_report(self, slip_tags):
        """在指定量条子集上传播，返回最终界与被违反的量条说明。"""
        sum_ids = {t[4:] for t in slip_tags if t.startswith("sum:")}
        diff_ids = {t[5:] for t in slip_tags if t.startswith("diff:")}
        sums = tuple(s for s in self._all_sum_constraints()
                     if s.slip_id.startswith("__total__") or s.slip_id in sum_ids)
        diffs = tuple(s for s in self.diff_slips if s.slip_id in diff_ids)
        domains = {k: list(v) for k, v in self._initial_domains().items()}
        violated = []
        # 手工跑一遍带报告的传播（与 _propagate 同规则）
        changed = True
        while changed:
            changed = False
            for slip in sums:
                vars_ = slip.variables()
                lo_sum = sum(domains[v][0] for v in vars_)
                hi_sum = sum(domains[v][1] for v in vars_)
                if lo_sum > slip.value or hi_sum < slip.value:
                    violated.append(
                        f"{slip.slip_id}: 可达和区间 [{lo_sum},{hi_sum}] 不含 {slip.value}"
                    )
                free = [v for v in vars_ if domains[v][0] != domains[v][1]]
                if len(free) == 1 and lo_sum <= slip.value <= hi_sum:
                    var = free[0]
                    fixed = slip.value - (lo_sum - domains[var][0])
                    lo, hi = domains[var]
                    if max(lo, fixed) <= min(hi, fixed) and (max(lo, fixed), min(hi, fixed)) != (lo, hi):
                        domains[var] = [max(lo, fixed), min(hi, fixed)]
                        changed = True
            for slip in diffs:
                x = (slip.brick_a, slip.layer_a)
                y = (slip.brick_b, slip.layer_b)
                xlo, xhi = domains[x]
                ylo, yhi = domains[y]
                nx = (max(xlo, ylo + slip.value), min(xhi, yhi + slip.value))
                ny = (max(ylo, xlo - slip.value), min(yhi, xhi - slip.value))
                if nx[0] > nx[1] or ny[0] > ny[1]:
                    violated.append(
                        f"{slip.slip_id}: {x}-{y}={slip.value} 推出空界"
                    )
                if list(nx) != domains[x]:
                    domains[x] = list(nx)
                    changed = True
                if list(ny) != domains[y]:
                    domains[y] = list(ny)
                    changed = True
        return domains, sorted(set(violated))

    @staticmethod
    def _explain_empty_domain(domains, violated):
        empty = [f"{v} 界 [{lo},{hi}] 为空" for v, (lo, hi) in domains.items() if lo > hi]
        if empty:
            return "；".join(empty)
        return "；".join(violated) if violated else "整数层厚无可行赋值"

    # --------------------------------------------------------------- 备料查询
    def possible_colors(self, brick_id, position):
        """一条砖在某格位上，跨全部可行读法可能出现的口味集合。"""
        brick = self.brick(brick_id)
        if not (0 <= position < brick.total_length):
            raise IcebrickError(f"格位 {position} 越界（砖 {brick_id} 长 {brick.total_length}）")
        return {reading.color_at(brick_id, position) for reading in self.enumerate_readings()}

    def can_see_together(self, facts):
        """若干 (砖, 格位, 颜色) 事实能否在 *同一份* 读法下同时成立。"""
        for brick_id, position, color in facts:
            self.brick(brick_id)  # 引用核对
        for reading in self.enumerate_readings():
            if all(reading.color_at(b, p) == c for b, p, c in facts):
                return True
        return False

    def readings_supporting(self, facts):
        """与已观察事实相容的读法子集（备料卡/信念更新用）。"""
        return tuple(
            reading
            for reading in self.enumerate_readings()
            if all(reading.color_at(b, p) == c for b, p, c in facts)
        )

    def describe_readings(self):
        lines = [f"快照 {self.content_hash[:13]}（{self.label or '无标签'}）共 {len(self.enumerate_readings())} 种可行读法："]
        for i, reading in enumerate(self.enumerate_readings(), 1):
            lines.append(f"  {i}. {reading}")
        return "\n".join(lines)
