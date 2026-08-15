"""用量记账存储（SQLite，单文件）

一条记录 = (service, key_id, limit_key)：

- `service`：服务标识，与调用方 api_secrets.json 的字段名一致，如 `byte_search_api_keys`
- `key_id`：key 的 md5（调用方本地算好再传，服务端全程不接触真实 key）
- `key_mask`：`RfhR***NkSj`，只为了看着方便
- `limit_key`：统计窗口，`period='day'` → `YYYY-MM-DD`，`period='month'` → `YYYY-MM`，
  `period='day+11H'`（每天 11:00 至次日 11:00）→ `YYYY-MM-DD+11H`（取窗口起始日）；
  窗口变了自然是一条新记录，不需要清零逻辑
- `quta`：该窗口已用次数；`limit` 为上限（0 表示只统计不拦截）

对外只一个写入语义：`acquire()` = 「请求一次调用许可」，在同一个 `BEGIN IMMEDIATE`
事务里完成「判定余额 + 扣减」，拿不到许可（`granted=false`）就是额度用完了；
不存在「先查再记」的窗口期，多机器并发也不会超发。WAL 模式下允许多客户端并发读。
"""

import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import List, Optional

# 基础统计周期；另支持带起算时刻的 `day+nH`（见 normalize_period）
PERIODS = ('day', 'month')
# `day+nH`：每天 n 点切窗口。调用方有些额度不是 0 点重置（火山按量端点是每天 11:00），
# 按自然日记账会与真实配额错位。大小写不敏感，归一后存取。
_DAY_OFFSET_RE = re.compile(r'^day\+(\d{1,2})H$', re.IGNORECASE)
_PERIOD_HINT = "只能是 'day' / 'month' / 'day+nH'（n 为 0-23，如 day+11H）"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    service    TEXT    NOT NULL,
    key_id     TEXT    NOT NULL,
    limit_key  TEXT    NOT NULL,
    period     TEXT    NOT NULL DEFAULT 'day',
    key_mask   TEXT    NOT NULL DEFAULT '',
    max_calls  INTEGER NOT NULL DEFAULT 0,
    quta       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (service, key_id, limit_key)
);
CREATE INDEX IF NOT EXISTS idx_usage_window ON usage (service, limit_key);
"""


def key_id_of(api_key: str) -> str:
    """完整 key → md5 十六进制（服务端只认这个标识）。

    只作为记账标识用，不用于鉴权；接口侧只接受这个 md5，不接受真实 key。
    """
    return hashlib.md5(str(api_key or '').encode('utf-8')).hexdigest()


def mask_key(api_key: str) -> str:
    """key 掩码（前 4 + 后 4），只用于展示（本地工具/自测用，接口不收真实 key）。"""
    key = str(api_key or '').strip()
    if not key:
        return '***'
    if len(key) <= 8:
        return f"{key[:2]}***"
    return f"{key[:4]}***{key[-4:]}"


def normalize_period(period: str) -> str:
    """校验并归一统计周期，非法值抛 ValueError。

    合法形态：`day` / `month` / `day+nH`（n 为 0-23）。`day+09H` 与 `day+9H` 归一到
    同一个值，否则两种写法算出两个窗口标识，同一份额度被记成两条账。

    **与调用方 ai-skills 的 `wx_gzh_article_writer/scripts/tools/basic/api_usage.py`
    是同一套语法，改这里必须同步改那边**（两端各自算窗口，算不一样就对不上账）。
    """
    raw = str(period or '').strip()
    if raw in PERIODS:
        return raw
    match = _DAY_OFFSET_RE.match(raw)
    if match:
        hour = int(match.group(1))
        if hour > 23:
            raise ValueError(f"period 的起算时刻必须在 0-23，当前: {period}")
        return f'day+{hour}H'
    raise ValueError(f"period {_PERIOD_HINT}，当前: {period}")


def day_offset_of(period: str) -> Optional[int]:
    """`day+nH` 返回起算小时 n；`day` / `month` 返回 None。"""
    match = _DAY_OFFSET_RE.match(str(period or '').strip())
    return int(match.group(1)) if match else None


def window_label(period: str, now: Optional[datetime] = None) -> str:
    """当前统计窗口：day → YYYY-MM-DD，month → YYYY-MM。

    `day+nH` → `YYYY-MM-DD+nH`，取窗口**起始日**：当前时刻还没到 n 点就算前一天的
    窗口。后缀故意保留：与自然日 label 不同名，调用方改过 period 后旧记录不会被
    当成本窗口。
    """
    normalized = normalize_period(period)
    moment = now or datetime.now()
    hour = day_offset_of(normalized)
    if hour is not None:
        start = moment if moment.hour >= hour else moment - timedelta(days=1)
        return f"{start.strftime('%Y-%m-%d')}+{hour}H"
    return moment.strftime('%Y-%m-%d' if normalized == 'day' else '%Y-%m')


class UsageStore:
    """用量记账表，线程内各持一个连接（ThreadingHTTPServer 每请求一线程）。"""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def query(self, service: str, key_id: str, period: str) -> dict:
        """查该 key 当前窗口用量；无记录返回 quta=0。"""
        label = window_label(period)
        row = self._connect().execute(
            'SELECT * FROM usage WHERE service=? AND key_id=? AND limit_key=?',
            (service, key_id, label),
        ).fetchone()
        if row is None:
            return {'service': service, 'keyId': key_id, 'keyMask': '',
                    'limitKey': label, 'limit': 0, 'quta': 0}
        return _to_item(row)

    def acquire(self, service: str, key_id: str, period: str, max_calls: int = 0,
                delta: int = 1, key_mask: str = '', exhausted: bool = False) -> dict:
        """申请 delta 次调用许可：判定与扣减在同一事务里完成。

        - 额度够：`quta += delta`，`granted=True`
        - 额度不够：**不扣减**，`granted=False`（调用方据此直接换 key）
        - `max_calls=0`（只统计不拦截）：永远批准，只累加计数
        - `exhausted=True`：调用方从第三方接口得知该 key 已满/失效，把 `quta` 拉到
          `max_calls` 并返回 `granted=False`（官方口径比本地计数准）
        """
        return self._write(service, key_id, period, max_calls, key_mask,
                           delta=int(delta), exhausted=bool(exhausted))

    def _write(self, service: str, key_id: str, period: str, max_calls: int,
               key_mask: str, delta: int, exhausted: bool) -> dict:
        label = window_label(period)
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                'SELECT quta, key_mask, max_calls FROM usage'
                ' WHERE service=? AND key_id=? AND limit_key=?',
                (service, key_id, label),
            ).fetchone()
            quta = int(row['quta']) if row else 0
            mask = key_mask or (row['key_mask'] if row else '') or ''
            limit = int(max_calls or (row['max_calls'] if row else 0) or 0)

            if exhausted:
                # limit 为 0 时表达不了「满额」，保持原计数
                quta = max(quta, limit)
                granted = False
            elif limit and quta + delta > limit:
                granted = False
            else:
                quta += delta
                granted = True

            conn.execute(
                'INSERT INTO usage (service, key_id, limit_key, period, key_mask,'
                ' max_calls, quta, updated_at) VALUES (?,?,?,?,?,?,?,?)'
                ' ON CONFLICT(service, key_id, limit_key) DO UPDATE SET'
                ' quta=excluded.quta, max_calls=excluded.max_calls,'
                ' key_mask=excluded.key_mask, period=excluded.period,'
                ' updated_at=excluded.updated_at',
                (service, key_id, label, period, mask, limit, quta, now),
            )
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        return {'granted': granted, 'service': service, 'keyId': key_id, 'keyMask': mask,
                'limitKey': label, 'limit': limit, 'quta': quta,
                'remaining': max(limit - quta, 0) if limit else None}

    def snapshot(self, service: str, period: str) -> List[dict]:
        """某服务当前窗口内所有 key 的用量（按 quta 倒序）。"""
        label = window_label(period)
        rows = self._connect().execute(
            'SELECT * FROM usage WHERE service=? AND limit_key=? ORDER BY quta DESC',
            (service, label),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def dump(self, limit_rows: int = 500) -> List[dict]:
        """全部服务的最近记账（含历史窗口），给排查/看板用。"""
        rows = self._connect().execute(
            'SELECT * FROM usage ORDER BY limit_key DESC, service ASC, quta DESC LIMIT ?',
            (int(limit_rows),),
        ).fetchall()
        return [_to_item(row) for row in rows]


def _to_item(row: sqlite3.Row) -> dict:
    return {
        'service': row['service'],
        'keyId': row['key_id'],
        'keyMask': row['key_mask'] or '',
        'limitKey': row['limit_key'],
        'period': row['period'],
        'limit': int(row['max_calls'] or 0),
        'quta': int(row['quta'] or 0),
        'updatedAt': row['updated_at'],
    }
