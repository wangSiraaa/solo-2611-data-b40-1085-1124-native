"""业务错误类型。所有错误都带稳定 code，便于 JSON 操作计划按码断言。"""


class LedgerError(Exception):
    """本系统业务错误基类。"""

    code = "ledger_error"

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        # details 为可 JSON 化的结构化明细（待核清单、冲突字段等）
        self.details = details if details is not None else {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(LedgerError):
    """整笔拒绝类错误：未知账户、非法金额、借贷不平、文档与分录不一致等。"""

    code = "validation_error"


class ConflictError(LedgerError):
    """稳定身份冲突：同 id 重投但内容异文。"""

    code = "conflict"


class PendingError(LedgerError):
    """发布底稿前缺镜像/异文/收付不一致/声明不全，返回明确待核明细。"""

    code = "pending"


class SnapshotConflictError(LedgerError):
    """同 input_id 以不同切面（heads 或声明版本）重发。"""

    code = "snapshot_conflict"
