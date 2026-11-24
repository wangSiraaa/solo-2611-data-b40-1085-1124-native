"""教具藏品称量：静止平面秤、随托车移动的物盒与读数窗合法性。

合同（与连续动作库一致，全部精确有理数、无采样）：

- 平面秤是**静止轴对齐矩形**；物盒装在具名矩形托车上，走分段线性轨迹。
- 物盒在时刻 t 被**完整托住**，当且仅当两个方向同时满足
  ``|dx(t)| <= 秤半宽 - 盒半宽`` 与 ``|dy(t)| <= 秤半高 - 盒半高``。
  完整托住允许盒边与秤边贴齐（取 ``<=``）；盒比秤大则该方向极限为负，
  永远不可能完整托住。
- 物盒与秤“有正面积相交”沿用 :class:`~cart_audit.service.ProfileOverlapService`
  的严格合同（``<``，碰边/碰角不算）。
- 读数窗是半开区间 ``[t0, t1)``。窗内每盒必须**全程完整托住**（贡献全重）
  或**全程无正面积相交**（贡献零）；任一盒在窗内出现半搭、进出、边缘混杂，
  整窗作废。只采窗两端看不出中途半搭，因此本服务按共同细分逐段精确求解，
  与核查库一样不允许凭端点采样下结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

from .intervals import TimeInterval, intersect_intervals, merge_union
from .models import MotionSegment, Profile, Trajectory
from .rational import fraction
from .service import ProfileOverlapService

__all__ = ["Scale", "ScaleWindow", "ScaleAuditService", "ScaleInputError"]


class ScaleInputError(ValueError):
    """秤、读数窗或称量输入非法。"""


@dataclass(frozen=True)
class Scale:
    """静止平面秤：固定中心与轴对齐矩形半宽/半高（正有理数）。"""

    scale_id: str
    x: Fraction
    y: Fraction
    half_width: Fraction
    half_height: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.scale_id, str) or not self.scale_id:
            raise ScaleInputError("scale_id 必须是非空字符串")
        x = fraction(self.x, field="scale.x")
        y = fraction(self.y, field="scale.y")
        w = fraction(self.half_width, field="scale.half_width")
        h = fraction(self.half_height, field="scale.half_height")
        if w <= 0 or h <= 0:
            raise ScaleInputError("秤的半宽/半高必须为正有理数")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "half_width", w)
        object.__setattr__(self, "half_height", h)

    @classmethod
    def from_data(cls, data: dict) -> "Scale":
        try:
            return cls(
                scale_id=str(data["scale_id"]),
                x=fraction(data["x"], field="scale.x"),
                y=fraction(data["y"], field="scale.y"),
                half_width=fraction(data["half_width"], field="scale.half_width"),
                half_height=fraction(data["half_height"], field="scale.half_height"),
            )
        except KeyError as exc:
            raise ScaleInputError(f"平面秤缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {
            "scale_id": self.scale_id,
            "x": str(self.x),
            "y": str(self.y),
            "half_width": str(self.half_width),
            "half_height": str(self.half_height),
        }

    def stationary_segment(self, t0: Fraction, t1: Fraction) -> MotionSegment:
        """在 [t0, t1) 上表示秤的静止动作段，供轮廓服务复用。"""
        return MotionSegment(0, t0, t1, self.x, self.y, self.x, self.y)

    def profile(self) -> Profile:
        return Profile(self.scale_id, self.half_width, self.half_height)


@dataclass(frozen=True)
class ScaleWindow:
    """一个读数窗：半开 [t0, t1)，读数为闭整数区间 [lo, hi]。"""

    window_id: str
    t0: Fraction
    t1: Fraction
    reading_low: int
    reading_high: int
    calibration: Optional[bool] = None

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id:
            raise ScaleInputError("window_id 必须是非空字符串")
        t0 = fraction(self.t0, field="window.t0")
        t1 = fraction(self.t1, field="window.t1")
        if t1 <= t0:
            raise ScaleInputError(f"读数窗 {self.window_id!r} 必须满足 t1 > t0")
        lo, hi = self.reading_low, self.reading_high
        if not isinstance(lo, int) or isinstance(lo, bool) or not isinstance(hi, int) or isinstance(hi, bool):
            raise ScaleInputError("读数区间端点必须是整数")
        if hi < lo:
            raise ScaleInputError(f"读数窗 {self.window_id!r} 读数区间上下界颠倒")
        object.__setattr__(self, "t0", t0)
        object.__setattr__(self, "t1", t1)

    @classmethod
    def from_data(cls, data: dict) -> "ScaleWindow":
        try:
            return cls(
                window_id=str(data["window_id"]),
                t0=fraction(data["t0"], field="window.t0"),
                t1=fraction(data["t1"], field="window.t1"),
                reading_low=_strict_int(data["reading_low"], "reading_low"),
                reading_high=_strict_int(data["reading_high"], "reading_high"),
                calibration=data.get("calibration"),
            )
        except KeyError as exc:
            raise ScaleInputError(f"读数窗缺少字段 {exc.args[0]!r}") from exc

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "t0": str(self.t0),
            "t1": str(self.t1),
            "reading_low": self.reading_low,
            "reading_high": self.reading_high,
            "calibration": self.calibration,
        }


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ScaleInputError(f"{field} 不接受布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    if isinstance(value, str):
        parsed = Fraction(value)
        if parsed.denominator == 1:
            return parsed.numerator
    raise ScaleInputError(f"{field} 必须是整数，拒绝浮点或非整数值: {value!r}")


class ScaleAuditService:
    """无状态称量几何审核器：逐窗判定合法/作废并保存逐 cell 证据。"""

    def __init__(self, overlap_service: Optional[ProfileOverlapService] = None) -> None:
        self.overlap = overlap_service or ProfileOverlapService()

    # ---- 单方向“完整在内”非严格不等式 |d0 + slope*tau| <= limit --------
    @staticmethod
    def _axis_inside(
        d0: Fraction,
        slope: Fraction,
        limit: Fraction,
        lo: Fraction,
        hi: Fraction,
    ) -> Optional[TimeInterval]:
        """求解 lo <= t < hi 上的 ``|d0 + slope*(t-lo)| <= limit``，根点闭合。"""
        if limit < 0:
            # 盒比秤还大（半宽差为负）：该方向不可能完整托住。
            return None
        if slope == 0:
            if abs(d0) <= limit:
                return TimeInterval(lo, hi, True, False)
            return None
        tau_low = (-limit - d0) / slope
        tau_high = (limit - d0) / slope
        if tau_low > tau_high:
            tau_low, tau_high = tau_high, tau_low
        root_low = lo + tau_low
        root_high = lo + tau_high
        low = max(root_low, lo)
        high = min(root_high, hi)
        if high < low:
            return None
        if high == low:
            # 孤立贴边点（low < hi 保证它落在半开 cell 内）：零长度区间，
            # 在 merge_union 中按空处理，不影响“全程托住”的判定。
            return TimeInterval(low, low, True, True)
        # <= 的根点闭合；左端点若落在域起点也闭合。右端随半开 cell 统一开。
        return TimeInterval(low, high, True, False)

    def _inside_interval(
        self,
        scale: Scale,
        box_profile: Profile,
        box_segment: MotionSegment,
        u: Fraction,
        v: Fraction,
    ) -> Optional[TimeInterval]:
        """一个 cell 上盒矩形完整落在秤矩形内（允许贴边）的时间区间。"""
        lo = max(box_segment.t0, u)
        hi = min(box_segment.t1, v)
        if lo >= hi:
            return None
        bx, by = box_segment.position_at(lo)
        vx = (box_segment.x1 - box_segment.x0) / box_segment.duration
        vy = (box_segment.y1 - box_segment.y0) / box_segment.duration
        x_limit = scale.half_width - box_profile.half_width
        y_limit = scale.half_height - box_profile.half_height
        x_iv = self._axis_inside(bx - scale.x, vx, x_limit, lo, hi)
        y_iv = self._axis_inside(by - scale.y, vy, y_limit, lo, hi)
        iv = intersect_intervals(x_iv, y_iv)
        if iv is None:
            return None
        return intersect_intervals(iv, TimeInterval(u, v, True, False))

    def _overlap_interval(
        self,
        scale: Scale,
        box_profile: Profile,
        box_segment: MotionSegment,
        u: Fraction,
        v: Fraction,
    ) -> Optional[TimeInterval]:
        """一个 cell 上盒与秤正面积相交的时间区间（严格合同，碰边不算）。"""
        lo = max(box_segment.t0, u)
        hi = min(box_segment.t1, v)
        if lo >= hi:
            return None
        scale_seg = scale.stationary_segment(lo, hi)
        iv = self.overlap.segment_overlap(box_profile, box_segment, scale.profile(), scale_seg)
        if iv is None:
            return None
        return intersect_intervals(iv, TimeInterval(u, v, True, False))

    @staticmethod
    def _subdivision(window: ScaleWindow, trajectories: Sequence[Trajectory]) -> List[Fraction]:
        grid = {window.t0, window.t1}
        for traj in trajectories:
            h0, h1 = traj.horizon
            # 窗必须整体落在每辆托车轨迹的核查域内，部分越界同样拒绝（无外推）。
            if window.t0 < h0 or window.t1 > h1:
                raise ScaleInputError(
                    f"读数窗 {window.window_id!r} [{window.t0}, {window.t1}) 越出 "
                    f"{traj.cart_id!r} 的核查域 [{h0}, {h1})"
                )
            for knot in traj.knots:
                if window.t0 <= knot.t <= window.t1:
                    grid.add(knot.t)
        return sorted(grid)

    def classify_window(
        self,
        scale: Scale,
        window: ScaleWindow,
        boxes: Sequence[Tuple[str, Profile, Trajectory]],
    ) -> dict:
        """判定一个读数窗合法还是作废，并给出每盒状态与逐 cell 证据。

        返回字典字段：

        - ``status``：``"legal"`` 或 ``"void"``；
        - ``contained``：窗内全程完整托住、贡献全重的盒；
        - ``zero``：窗内始终无正面积相交、贡献零的盒；
        - ``bad``：作废证据，含每盒问题 cell（``inside/outside/mixed``）与
          合并后的托住区间、正面积相交区间。
        """
        trajectories = [item[2] for item in boxes]
        grid = self._subdivision(window, trajectories)
        cell_evidence: Dict[str, List[dict]] = {box_id: [] for box_id, _, _ in boxes}
        inside_pieces: Dict[str, List[TimeInterval]] = {box_id: [] for box_id, _, _ in boxes}
        overlap_pieces: Dict[str, List[TimeInterval]] = {box_id: [] for box_id, _, _ in boxes}
        segments_cache: Dict[str, Tuple[List[MotionSegment], List[Fraction]]] = {}
        for box_id, _profile, traj in boxes:
            segs = traj.segments()
            segments_cache[box_id] = (segs, [s.t0 for s in segs])

        for u, v in zip(grid, grid[1:]):
            for box_id, box_profile, traj in boxes:
                segs, starts = segments_cache[box_id]
                idx = _segment_index(starts, u)
                seg = segs[idx]
                inside_iv = self._inside_interval(scale, box_profile, seg, u, v)
                overlap_iv = self._overlap_interval(scale, box_profile, seg, u, v)
                if inside_iv is not None:
                    inside_pieces[box_id].append(inside_iv)
                if overlap_iv is not None:
                    overlap_pieces[box_id].append(overlap_iv)
                # cell 状态只取决于“完整托住/正面积相交”是否覆盖整个 cell：
                # 孤立贴边点（零长度 inside、无 overlap）不算 inside，也不算 mixed。
                full_inside = (
                    inside_iv is not None
                    and inside_iv.left == u
                    and inside_iv.right == v
                )
                if full_inside:
                    cell_status = "inside"
                elif overlap_iv is None:
                    cell_status = "outside"
                else:
                    # 有正面积相交但未完整托住：半搭、进出或贴边混杂。
                    cell_status = "mixed"
                cell_evidence[box_id].append(
                    {
                        "cell": [str(u), str(v)],
                        "status": cell_status,
                        "inside": None if inside_iv is None else inside_iv.to_dict(),
                        "overlap": None if overlap_iv is None else overlap_iv.to_dict(),
                    }
                )

        contained: List[str] = []
        zero: List[str] = []
        bad: List[dict] = []
        for box_id, _profile, _traj in boxes:
            inside_union = merge_union(inside_pieces[box_id])
            overlap_union = merge_union(overlap_pieces[box_id])
            fully_inside = (
                len(inside_union) == 1
                and inside_union[0].left == window.t0
                and inside_union[0].right == window.t1
                and inside_union[0].left_inclusive
            )
            never_touches = len(overlap_union) == 0
            if fully_inside:
                contained.append(box_id)
            elif never_touches:
                # 全程连正面积相交都没有（含只碰边/角）：贡献零，与是否出现过
                # 孤立贴边点无关——孤立点对“完整托住时长”无贡献，也无正面积。
                zero.append(box_id)
            else:
                statuses = [c["status"] for c in cell_evidence[box_id]]
                if "mixed" in statuses:
                    reason = "半搭/进出：窗内存在有正面积相交但未完整托住的时段"
                elif "inside" in statuses:
                    reason = "进出：仅部分时段完整托住，其余时段在秤外"
                else:
                    reason = "边缘混杂：窗内与秤的相交关系不满足全程全托或全程零相交"
                bad.append(
                    {
                        "box_id": box_id,
                        "reason": reason,
                        "cells": cell_evidence[box_id],
                        "inside_union": [iv.to_dict() for iv in inside_union],
                        "overlap_union": [iv.to_dict() for iv in overlap_union],
                    }
                )

        return {
            "window_id": window.window_id,
            "scale_id": scale.scale_id,
            "range": [str(window.t0), str(window.t1)],
            "status": "legal" if not bad else "void",
            "contained": contained,
            "zero": zero,
            "bad": bad,
            "reading": [window.reading_low, window.reading_high],
            "calibration": window.calibration,
        }

    def audit_windows(
        self,
        scale: Scale,
        windows: Sequence[ScaleWindow],
        boxes: Sequence[Tuple[str, Profile, Trajectory]],
    ) -> List[dict]:
        """逐窗审核；窗 id 重复拒绝，避免证据张冠李戴。"""
        seen = set()
        results = []
        for window in windows:
            if window.window_id in seen:
                raise ScaleInputError(f"读数窗 id 重复: {window.window_id!r}")
            seen.add(window.window_id)
            results.append(self.classify_window(scale, window, boxes))
        return results


def _segment_index(starts: Sequence[Fraction], t: Fraction) -> int:
    from bisect import bisect_right

    idx = bisect_right(starts, t) - 1
    if idx < 0:
        raise ScaleInputError(f"时刻 {t} 找不到对应动作段（轨迹缺段）")
    return idx
