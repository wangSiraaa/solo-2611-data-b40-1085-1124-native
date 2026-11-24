"""留样交付：备赛分交具名留样 或 保管台临开炉发实样（任选一套可运行办法）。

题面允许两条办法，本模块都实现，并统一折算成 :class:`~baking_judge.scheduling.Sample`
的到场条件，供 ``plan_retaste`` 使用：

办法 A —— 备赛分交（pre-distributed）
    展方在备赛时把**具名**留样分给各场。分交后受**单场保鲜期**限制：
    若某场分交发生于 ``s`` 班、保鲜期为 ``life`` 场，则该留样只能在
    ``session <= s + life`` 的机位使用。

办法 B —— 中心临发（central dispatch）
    全部留样存于保管台，临开炉才发实样。中心到炉有一班**运送时间** ``transit``：
    若在某时刻 ``t0`` 发出，则很早在 ``t0 + transit`` 之后才可能到场，
    到场时刻还要落在试样自身的试吃时窗内。

两种办法都尊重：份数、隔离标签、最后可开班次、试吃时窗。交付方式只影响
“该试样能否在某 slot 到场”，不影响复尝位覆盖算法本身。
"""

from .scheduling import Sample

PRE_DISTRIBUTED = "pre_distributed"
CENTRAL_DISPATCH = "central_dispatch"


def pre_distributed_samples(supplies, session, shelf_life):
    """办法 A：在 ``session`` 班把具名留样分交，保鲜期 ``shelf_life`` 场。

    ``supplies``：iterable，每项 dict，至少含
    ``supplier``/``sample_id``/``label``/``window``/``last_session``/``count``。

    返回的 Sample 已把“保鲜期”折进 ``last_session``：实际可开班次取
    ``min(last_session, session + shelf_life)``。
    """
    out = []
    for item in supplies:
        last = item.get("last_session")
        cap = session + shelf_life
        effective_last = cap if last is None else min(last, cap)
        out.append(Sample(
            item["sample_id"], item["supplier"], item["label"],
            tuple(item["window"]), effective_last, item.get("count", 1),
        ))
    return out


def central_dispatch_samples(supplies, dispatch_time, transit):
    """办法 B：于 ``dispatch_time`` 从中心发出，一班运输 ``transit`` 才到炉。

    到场不早于 ``dispatch_time + transit``；把试样窗口的起点抬到该时刻。
    若某试样原窗口完全早于到场时刻，则它在此办法下无法到场（不产可用 Sample）。
    """
    arrive = dispatch_time + transit
    out = []
    for item in supplies:
        start, end = item["window"]
        new_start = max(start, arrive)
        if new_start > end:
            # 到场时已过试吃时窗：此办法下不可用，跳过
            continue
        out.append(Sample(
            item["sample_id"], item["supplier"], item["label"],
            (new_start, end), item["last_session"], item.get("count", 1),
        ))
    return out


def deliver(mode, supplies, *, session=None, shelf_life=None,
            dispatch_time=None, transit=None):
    if mode == PRE_DISTRIBUTED:
        if session is None or shelf_life is None:
            raise ValueError("办法 A 需要 session 与 shelf_life")
        return pre_distributed_samples(supplies, session, shelf_life)
    if mode == CENTRAL_DISPATCH:
        if dispatch_time is None or transit is None:
            raise ValueError("办法 B 需要 dispatch_time 与 transit")
        return central_dispatch_samples(supplies, dispatch_time, transit)
    raise ValueError(f"未知交付办法 {mode!r}")
