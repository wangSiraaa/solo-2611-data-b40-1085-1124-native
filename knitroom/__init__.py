"""针织班的版师（pattern）与缝台（sewing）两个独立服务。

模块划分：
- schema  ：针织域 SQLite 表（图解版本、缝边记录、实针记录、线轴与占用）
- engine  ：纯函数的针织机：平织/二并一/一针增一针、具名线圈谱系、边弧与标记
- pattern ：版师服务，从借还台账的真实取件回执出发，按取件当时的册版出缝边记录
- sewing  ：缝台服务，读缝边记录与已落针位置，给余下能走的针序、吃势与交袖判定
- system  ：KnitRoom 门面，复用 library 的连接，可持久化重开

两个服务与 library/catalog、library/ledger 物理分表：版师只读台账的回执，
缝台只读版师的记录与自己的实针表，互不写入对方的数据。
"""

from library.errors import IdempotencyConflict, LibraryError, Rejection

from .system import KnitRoom

__all__ = ["KnitRoom", "LibraryError", "Rejection", "IdempotencyConflict"]
