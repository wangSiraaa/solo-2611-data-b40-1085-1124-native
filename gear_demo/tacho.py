"""测速盘换盘：定位工装（停一拍）/ 双盘联测两种维护安排。

背景（与题面对应）：

* 演示台四刻一圈；**真实齿位**（轴的犬牙、已锁住的套）永远在真实位置上，
  换盘只改变“表盘读数”对照关系，不动真实位置。
* 新盘可按两种标记方向装上；表盘零位相对真齿可能错 **0 或 2 刻**。
* 测速盘必须在演示中途换一次。

读数模型：真实相位 ``φ``（整数刻，mod T），盘以标记方向 ``s∈{+1,-1}`` 和
零位误差 ``z∈{0,2}`` 装上时，表盘读数::

    display = (s·φ + z) mod T

``s`` 与 ``z`` 都是离散歧义源。没有交接证据时，即便旧盘与新盘都在表盘上读到
0（``display_old=display_new=0``），也可能对应 ``φ=0``（z=0）或 ``φ=2``（z=2）——
两种真实齿位彼此相差半圈，犬牙不可能都在同一刻啮合，因此**不足以放行**。

两种维护安排：

1. ``pause_fixture``：演示停一拍，用定位工装把新盘找回真齿。换盘占**一拍**，
   停台期间所有轴不前进；工装给出“真齿↔表盘”直接证据（z 被设为 0，s 已知）。
   代价：停一拍会把后续拍号整体后移，可能使某个换挡窗错过。
2. ``dual_measure``：不停台，把**唯一的双盘联测位置**留给这次换盘：在同拍同时
   读取旧盘（已标定）与新盘，用旧盘反解真实 φ，从而确定新盘的 z（在给定 s 下）。
   该联测位当拍不能再承接任何其他校准。

本模块给出一份 :class:`TachoSwap`，对所选安排：

* 依据真实读数（来自演示轨迹）在交接前后核算“可信读数窗口”；
* 解释在什么情况下会错过演示（换盘/校准拍落到换挡窗之外、或停台使窗后移越界）；
* 在补上实际校准证据后才 ``release``；无证据时拒绝放行。
"""

from dataclasses import dataclass

from .kinematics import GaugeError

PAUSE_FIXTURE = "pause_fixture"
DUAL_MEASURE = "dual_measure"


@dataclass(frozen=True)
class Disc:
    name: str
    orientation: int  # +1 / -1 标记方向
    zero_shift: int  # 相对真齿的零位误差（0 或 2，可更一般）

    def __post_init__(self):
        if self.orientation not in (1, -1):
            raise GaugeError("标记方向只能是 +1 或 -1")

    def display(self, phase, ticks):
        return (self.orientation * phase + self.zero_shift) % ticks

    def phase_from(self, display, ticks, true_phase):
        """由旧盘读数反解真实相位需要知道其标定；这里返回其一致校验。"""
        return self.display(true_phase, ticks) == display % ticks


@dataclass
class CalibrationEvidence:
    """一次换盘的交接证据。

    ``kind``: ``pause_fixture`` 或 ``dual_measure``；``beat`` 为取证所在拍；
    ``true_phase`` 为工装/旧盘反解出的真实相位；``new_display`` 为新盘读数；
    ``orientation`` 为新盘实际标记方向；``zero_shift`` 为据此确定的新盘零位误差。
    ``old_display`` 仅在双盘联测时给出（与 ``true_phase`` 对照即可反解）。
    """

    kind: str
    beat: int
    true_phase: int
    new_display: int
    orientation: int
    zero_shift: int
    old_display: int = None
    meets_true_teeth: bool = False

    def as_dict(self):
        return {
            "kind": self.kind,
            "beat": self.beat,
            "true_phase": self.true_phase,
            "new_display": self.new_display,
            "old_display": self.old_display,
            "orientation": self.orientation,
            "zero_shift": self.zero_shift,
            "meets_true_teeth": self.meets_true_teeth,
        }


@dataclass
class SwapReport:
    method: str
    swap_beat: int
    pause_beats: list
    calibration: CalibrationEvidence
    trusted_beats_before: list
    trusted_beats_after: list
    usable_window: tuple
    misses: list
    released: bool
    reason: str
    seats_preserved: bool

    def as_dict(self):
        return {
            "method": self.method,
            "swap_beat": self.swap_beat,
            "pause_beats": self.pause_beats,
            "calibration": None if self.calibration is None else self.calibration.as_dict(),
            "trusted_beats_before": self.trusted_beats_before,
            "trusted_beats_after": self.trusted_beats_after,
            "usable_window": list(self.usable_window),
            "misses": self.misses,
            "released": self.released,
            "reason": self.reason,
            "seats_preserved": self.seats_preserved,
        }


class TachoSwap:
    """选定一种维护安排，核算交接窗口并给出放行/拒绝结论。"""

    def __init__(self, ticks_per_rev=4):
        self.ticks = ticks_per_rev

    # --------------------------------------------------- 取证（两种安排）
    def make_evidence_pause_fixture(self, beat, true_phase, orientation):
        """停台工装：把新盘找回真齿，故零位误差归 0，读数直接对应真实相位。"""
        z = 0
        disp = (orientation * true_phase + z) % self.ticks
        return CalibrationEvidence(
            kind=PAUSE_FIXTURE,
            beat=beat,
            true_phase=true_phase % self.ticks,
            new_display=disp,
            orientation=orientation,
            zero_shift=z,
            meets_true_teeth=True,
        )

    def make_evidence_dual_measure(self, beat, true_phase, old_disc, old_display,
                                   new_display, orientation):
        """双盘联测：旧盘（已标定）反解真实相位，据此确定新盘零位误差。

        校验旧盘读数确实与其标定一致，否则该读数不能作为证据。
        """
        if not old_disc.phase_from(old_display, self.ticks, true_phase):
            raise GaugeError("旧盘读数与其标定不一致，无法作为联测证据")
        z = (new_display - orientation * true_phase) % self.ticks
        return CalibrationEvidence(
            kind=DUAL_MEASURE,
            beat=beat,
            true_phase=true_phase % self.ticks,
            new_display=new_display % self.ticks,
            old_display=old_display % self.ticks,
            orientation=orientation,
            zero_shift=z,
            meets_true_teeth=False,
        )

    # --------------------------------------------------- 主流程
    def plan(self, method, swap_beat, trace, shift_windows, calibration=None,
             engaged_seats_before=None, engaged_seats_after=None,
             other_calibration_beats=()):
        """核算一次换盘安排。

        ``trace``: 演示逐拍轨迹（来自 :func:`gear_demo.bench.simulate`），每拍含
        ``beat``、``phases``（各轴真实相位）。``shift_windows``: 换挡窗列表的
        列表（每项为需要可信读数的拍号集合）。``calibration``: 若已有证据则传入。
        ``other_calibration_beats``: 本次演示中还需同拍承接的其它校准拍。

        时间轴模型（演示按拍号 0..N-1 排定）：

        * 停台工装：换盘那一拍整拍停台——无走齿、无锁合、无读数；该拍因此
          不可用于换挡。校准完成后（``evidence_beat`` 起）新盘可信。停台只
          占这一拍，其它拍照常运行。
        * 双盘联测：不停台；在换盘拍同拍读取新旧标记，故该拍及之后新盘即可信，
          但该拍不能再承接**其它校准**。
        """
        if method not in (PAUSE_FIXTURE, DUAL_MEASURE):
            raise GaugeError("未知维护安排")
        beats = sorted(t["beat"] for t in trace)
        total = beats[-1] + 1 if beats else 0
        other_calibration_beats = set(other_calibration_beats)

        pause_beats = [swap_beat] if method == PAUSE_FIXTURE else []
        evidence_beat = calibration.beat if calibration is not None else None

        # 各拍是否可“凭可信读数换挡”
        trusted_before = [b for b in beats if b < swap_beat]
        trusted_after = []
        if calibration is not None:
            if method == PAUSE_FIXTURE:
                # 停台拍整拍不可用；其后（证据拍及以后）新盘可信
                trusted_after = [
                    b for b in beats
                    if b > swap_beat and b >= evidence_beat
                ]
            else:
                # 双盘联测：换盘拍同拍即完成校准，该拍及之后可信
                trusted_after = [b for b in beats if b >= swap_beat]
        trusted = set(trusted_before) | set(trusted_after)
        # 停台拍无论如何不可用于换挡
        unavailable = set(pause_beats) if method == PAUSE_FIXTURE else set()
        usable_beats = trusted - unavailable

        # 任何换挡窗若没有可用可信拍 → 会错过演示
        misses = []
        for window in shift_windows:
            if not any(b in usable_beats for b in window):
                misses.append(
                    {
                        "window": list(window),
                        "reason": self._window_miss_reason(
                            method, window, swap_beat, evidence_beat, trusted,
                            unavailable,
                        ),
                    }
                )
        # 双盘联测占用的联测拍若还需承接其它校准 → 冲突
        dual_conflict = False
        if method == DUAL_MEASURE and swap_beat in other_calibration_beats:
            dual_conflict = True
            misses.append(
                {
                    "calibration_beat": swap_beat,
                    "reason": "双盘联测位当拍不能再承接其它校准",
                }
            )

        usable = (
            (min(usable_beats), max(usable_beats)) if usable_beats else (None, None)
        )

        released, reason = self._decide(
            method, calibration, usable_beats, shift_windows, total, dual_conflict
        )
        # 既有滑座保持真实锁合：交接前后真实锁合集合必须一致（真实位置不变）
        seats_preserved = True
        if engaged_seats_before is not None and engaged_seats_after is not None:
            seats_preserved = dict(engaged_seats_before) == dict(engaged_seats_after)

        return SwapReport(
            method=method,
            swap_beat=swap_beat,
            pause_beats=pause_beats,
            calibration=calibration,
            trusted_beats_before=trusted_before,
            trusted_beats_after=sorted(trusted_after),
            usable_window=usable,
            misses=misses,
            released=released,
            reason=reason,
            seats_preserved=seats_preserved,
        )

    @staticmethod
    def _window_miss_reason(method, window, swap_beat, evidence_beat, trusted,
                            unavailable):
        if method == PAUSE_FIXTURE and all(b in unavailable for b in window):
            return "唯一可用拍恰为停台换盘拍，整拍无走齿、无锁合，错过演示"
        if evidence_beat is not None:
            untrusted = [b for b in window if b not in trusted]
            if untrusted:
                return (
                    f"窗内拍 {untrusted} 落在换盘后、校准完成（拍 {evidence_beat}）之前，"
                    "读数不可信，错过演示"
                )
        return "该换挡窗内没有可信读数"

    def _decide(self, method, calibration, usable_beats, shift_windows, total,
                dual_conflict=False):
        if calibration is None:
            return (
                False,
                "没有交接证据：新盘零位误差在 0 与 2 刻之间歧义，两个显示同为 0 "
                "仍不足以放行",
            )
        # 证据拍必须落在演示范围内
        if total and calibration.beat >= total:
            return (
                False,
                f"交接证据拍 {calibration.beat} 落在演示范围之外，无法据此放行",
            )
        if method == PAUSE_FIXTURE and not calibration.meets_true_teeth:
            return (False, "定位工装安排要求证据来自真齿定位")
        if method == DUAL_MEASURE:
            if calibration.old_display is None:
                return (False, "双盘联测证据必须同时给出旧盘读数")
            # 旧盘读数必须与反解的真实相位自洽（再次核对）
            rebuilt = (calibration.orientation * calibration.true_phase
                       + calibration.zero_shift) % self.ticks
            if rebuilt != calibration.new_display % self.ticks:
                return (False, "双盘联测证据自相矛盾")
            if dual_conflict:
                return (False, "双盘联测位当拍还要承接其它校准，违反独占约定")
        # 每个换挡窗都要有可用可信拍
        for window in shift_windows:
            if not any(b in usable_beats for b in window):
                return (False, "有换挡窗落在不可信读数区间，不能放行")
        return (True, "交接证据充分，且所有换挡窗均有可信读数")

    # --------------------------------------------------- 何时错过演示（解说）
    def explain_miss(self, report, trace):
        """基于报告给出“何时会错过演示”的可核查说明。"""
        notes = []
        if report.misses:
            notes.extend(m["reason"] for m in report.misses)
        if report.calibration is None:
            notes.append(
                "缺交接证据导致换盘后整段不可信，若此间有换挡窗就会错过"
            )
        if report.method == PAUSE_FIXTURE:
            notes.append(
                f"停台占一拍（拍 {report.swap_beat}）：该拍无走齿、无锁合、无读数，"
                "故换挡窗若只覆盖这一拍就会错过；此外校准完成前新盘读数不可信，"
                "落在其前的换挡窗也会错过"
            )
        else:
            notes.append(
                f"双盘联测位（拍 {report.swap_beat}）当拍被本次换盘独占，"
                "不能再承接同拍其他校准；若该拍恰是别的校准唯一可用的拍则冲突"
            )
        return notes
