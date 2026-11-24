"""三色冰砖模拟的领域错误类型。"""


class IcebrickError(Exception):
    """所有可预期业务错误的基类。"""


class InfeasibleModelError(IcebrickError):
    """约束模型无完整可行读法；携带矛盾凭据。"""

    def __init__(self, witness):
        super().__init__(witness.describe())
        self.witness = witness


class TooManyReadingsError(IcebrickError):
    """可行读法超过题面承诺的六种上限。"""


class CupOrderError(IcebrickError):
    """杯单/轮班/窗口配置非法。"""


class NoGuaranteedPolicyError(IcebrickError):
    """不存在在所有可行读法下都准点交齐的策略；携带必定失约的观察分支凭据。"""

    def __init__(self, message, failure_branch=None):
        super().__init__(message)
        self.failure_branch = failure_branch
