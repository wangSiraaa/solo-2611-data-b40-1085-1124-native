"""针织域 schema：图解册版、缝边记录、缝台实针记录与线轴占用。

设计要点（与 library 同一套约定）：
- 图解册版是不可变快照：同一 chart_id 的不同 edition 各自保存完整图解，
  后续勘误/再版不覆盖旧册，旧缝边记录永远能按当时册版重算。
- 缝边记录一旦建立即锁定 receipt（取件回执）、册版与整块边谱快照（JSON），
  之后目录更正、新册版登记都不影响它。
- 线圈谱系、标记、边弧随快照保存，同时 loops/markers/arcs 三表可单独查询
  （“版师的边圈谱和缝台的实针记录都要能单独查”）。
- 缝台实针、线轴占用自成一组表，版师表不出现任何针/线占用概念。
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 图解册版（不可变全量快照）。chart_id 是同名作品标识，edition 是勘误本/再版号
CREATE TABLE IF NOT EXISTS knit_editions (
    chart_id   TEXT NOT NULL,
    edition    TEXT NOT NULL,
    book_id    TEXT NOT NULL,          -- 同名作品原书目（保留原身份）
    chart_json TEXT NOT NULL,          -- engine.ChartSpec 的完整快照
    created_at TEXT NOT NULL,
    PRIMARY KEY (chart_id, edition)
);

-- 每个图解当前生效的册版：勘误/再版登记后显式切换；旧缝边记录不受影响
CREATE TABLE IF NOT EXISTS knit_chart_head (
    chart_id TEXT PRIMARY KEY,
    edition  TEXT NOT NULL
);

-- 本册已画记号：挂在某次真实取件的具名线圈上，属于“这一册/这一件”的记号
CREATE TABLE IF NOT EXISTS knit_copy_marks (
    mark_id    TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    anchor     TEXT NOT NULL,          -- 锚定的具名线圈
    label      TEXT NOT NULL,
    orientation TEXT NOT NULL CHECK (orientation IN ('up', 'down')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kcm_receipt ON knit_copy_marks(receipt_id);

-- 缝边记录：版师按取件当时的册版出的带具名线圈与边弧的缝边
CREATE TABLE IF NOT EXISTS seam_records (
    seam_id        TEXT PRIMARY KEY,
    receipt_id     TEXT NOT NULL,      -- 借还台账里的取件回执/保留号
    revision       TEXT NOT NULL,      -- 取件当时锁定的目录册版
    chart_id       TEXT NOT NULL,
    edition        TEXT NOT NULL,      -- 取件当时该图解的生效册版
    piece_kind     TEXT NOT NULL CHECK (piece_kind IN ('sleeve', 'body')),
    copy_id        TEXT NOT NULL,      -- 实际取到的副本（如 C3）
    ticket_id      TEXT NOT NULL,      -- 兑现的票号（如 TC）
    reader_id      TEXT NOT NULL,      -- 真实读者（如 RC）
    snapshot_json  TEXT NOT NULL,      -- engine 边谱完整快照
    created_at     TEXT NOT NULL,
    FOREIGN KEY (chart_id, edition) REFERENCES knit_editions(chart_id, edition)
);

-- 边谱里的具名线圈（可单独查；parents 为二并一的两个父圈，children 为加针的两个子圈）
CREATE TABLE IF NOT EXISTS seam_loops (
    seam_id   TEXT NOT NULL,
    loop_id   TEXT NOT NULL,
    row_no    INTEGER NOT NULL,
    position  INTEGER NOT NULL,
    alive     INTEGER NOT NULL,
    parents   TEXT NOT NULL,           -- JSON 数组，平织为单元素，二并一为两元素
    children  TEXT NOT NULL,           -- JSON 数组，加针为两元素
    PRIMARY KEY (seam_id, loop_id),
    FOREIGN KEY (seam_id) REFERENCES seam_records(seam_id)
);

-- 缝线标记（定位标，不可翻面；随锚圈被收针而消失）
CREATE TABLE IF NOT EXISTS seam_markers (
    seam_id     TEXT NOT NULL,
    marker_id   TEXT NOT NULL,
    anchor_loop TEXT NOT NULL,
    orientation TEXT NOT NULL CHECK (orientation IN ('up', 'down')),
    alive       INTEGER NOT NULL,
    PRIMARY KEY (seam_id, marker_id),
    FOREIGN KEY (seam_id) REFERENCES seam_records(seam_id)
);

-- 边弧：两标记之间的一段，归属到当前边圈
CREATE TABLE IF NOT EXISTS seam_arcs (
    seam_id    TEXT NOT NULL,
    arc_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,       -- 从腋下起点向肩点的顺序
    owner_loop TEXT NOT NULL,
    region     TEXT NOT NULL,          -- 所在缝线标记区间
    PRIMARY KEY (seam_id, arc_id),
    FOREIGN KEY (seam_id) REFERENCES seam_records(seam_id)
);

-- 线轴：临缝专用线与定针线分开记账
CREATE TABLE IF NOT EXISTS thread_spools (
    spool_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('baste', 'permanent', 'decorative')),
    capacity   INTEGER NOT NULL,       -- 可缝的针数（装饰线为可占用的袖数）
    created_at TEXT NOT NULL
);

-- 线占用：定针/临缝各记一行；临缝在本次交货前不可再借给另一袖
CREATE TABLE IF NOT EXISTS thread_allocations (
    alloc_id   TEXT PRIMARY KEY,
    spool_id   TEXT NOT NULL,
    seam_id    TEXT,
    sleeve_id  TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('baste', 'permanent', 'decorative')),
    units      INTEGER NOT NULL,
    released   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (spool_id) REFERENCES thread_spools(spool_id)
);
CREATE INDEX IF NOT EXISTS idx_ta_open ON thread_allocations(spool_id) WHERE released = 0;
-- 共用的一段装饰标线只够缝一只：同一装饰线轴同时至多一只未释放占用
CREATE UNIQUE INDEX IF NOT EXISTS idx_decor_one_sleeve
    ON thread_allocations(spool_id)
    WHERE kind = 'decorative' AND released = 0;

-- 缝台实针记录：每针的针序、缝掉的袖弧/身弧与吃势方式
CREATE TABLE IF NOT EXISTS sewing_needles (
    needle_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    seam_group TEXT NOT NULL,          -- 同一对袖/同一次接单的分组
    sleeve_id  TEXT NOT NULL,
    seam_id_s  TEXT NOT NULL,
    seam_id_b  TEXT NOT NULL,
    region     TEXT NOT NULL,          -- 该针所在的缝线标记区间（吃势按区计额）
    mode       TEXT NOT NULL CHECK (mode IN ('baste', 'permanent')),
    ease       INTEGER NOT NULL,       -- 0 = 1对1；1 = 2对1吃势
    sleeve_arcs TEXT NOT NULL,         -- JSON 数组（1 或 2 段袖弧）
    body_arcs  TEXT NOT NULL,          -- JSON 数组（恒 1 段身弧）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sn_group_sleeve
    ON sewing_needles(seam_group, sleeve_id, needle_seq);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表：与 library.db.connect 的 IF NOT EXISTS 约定一致，可随重开调用。"""
    conn.executescript(SCHEMA)
