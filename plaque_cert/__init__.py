"""巡演协会销量认证模型：销售核单、合格包册、两馆刻牌。

模块划分：
- reconcile：销售核单与合格包册（哈希链、可复查）
- alloc：门槛/联号约束下的最多可发牌回溯求解
- halls：两间刻牌室的联网签印台 / 离线具名模具两种作业模式
"""

from . import alloc, halls, reconcile  # noqa: F401
