"""可修订加权记录源、可重复加权采样，以及贴纸厂/换卡柜共用程序。

四个可独立运行的模块：

* :mod:`wrsource.source`  -- SQLite 支撑的版本化加权记录源。
* :mod:`wrsource.dataset` -- 可重复加权采样数据集生产器。
* :mod:`wrsource.tape`    -- 连印带推断：回条 -> 可行（带型/起格/方向）。
* :mod:`wrsource.counter` -- 换卡柜：保证单、押换/即换、自适应开包规划。

实现只使用 Python 3.11 标准库。
"""

__all__ = ["source", "sampling", "dataset", "tape", "counter"]
__version__ = "1.1.0"
