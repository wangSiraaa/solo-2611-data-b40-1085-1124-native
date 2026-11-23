"""集团合并工作底稿系统（合成会计口径，仅 P/S 两实体、单币种、单期间）。

模块划分：
- accounts：固定账户表与有符号余额约定（借减贷）
- errors：业务错误类型
- jsoncodec：分数/JSON 序列化
- ledger：独立实体账簿（只追加、可重开、原子入账、局部校验）
- consolidation：集团合并服务（切面冻结、错时待核、逐发票抵销）
- verifier：独立核验（不调用合并服务自己的汇总函数）
"""

from .errors import (
    LedgerError,
    ValidationError,
    ConflictError,
    PendingError,
    SnapshotConflictError,
)
from .ledger import EntityLedger
from .consolidation import ConsolidationService
from .verifier import verify_worksheet

__all__ = [
    "LedgerError",
    "ValidationError",
    "ConflictError",
    "PendingError",
    "SnapshotConflictError",
    "EntityLedger",
    "ConsolidationService",
    "verify_worksheet",
]
