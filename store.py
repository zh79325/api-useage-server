"""用量记账存储（SQLite，单文件）

一条记录 = (service, key_id, limit_key)：

- `service`：服务标识，与调用方 api_secrets.json 的字段名一致，如 `byte_search_api_keys`
- `key_id`：key 的 md5（调用方本地算好再传，服务端全程不接触真实 key）
- `key_mask`：`RfhR***NkSj`，只为了看着方便
- `limit_key`：统计窗口，`period='day'` → `YYYY-MM-DD`，`period='month'` → `YYYY-MM`；
  窗口变了自然是一条新记录，不需要清零逻辑
- `quta`：该窗口已用次数；`limit` 为上限（0 表示只统计不拦截）

写操作用 `BEGIN IMMEDIATE` 串行化，WAL 模式下允许多客户端并发读。
"""

import hashlib
import os
import sqlite3
import threading
from datetime import datetime
from typing import List, Optional

PERIODS = ('day', 'month')

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


def window_label(period: str, now: Optional[datetime] = None) -> str:
    """当前统计窗口：day → YYYY-MM-DD，month → YYYY-MM。"""
    if period not in PERIODS:
        raise ValueError(f"period 只能是 {PERIODS}，当前: {period}")
    moment = now or datetime.now()
    return moment.strftime('%Y-%m-%d' if period == 'day' else '%Y-%m')


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

    def record(self, service: str, key_id: str, period: str,
               max_calls: int = 0, delta: int = 1, key_mask: str = '') -> dict:
        """记 delta 次调用（默认 +1），返回写后的当前窗口用量。"""
        return self._write(service, key_id, period, max_calls, key_mask,
                           lambda quta: quta + int(delta))

    def exhaust(self, service: str, key_id: str, period: str,
                max_calls: int = 0, key_mask: str = '') -> dict:
        """把该 key 当前窗口直接标成满额（quta = max_calls）。

        max_calls 为 0（只统计不拦截）时无法表达「满额」，保持原计数不动。
        """
        if not max_calls:
            return self.query(service, key_id, period)
        return self._write(service, key_id, period, max_calls, key_mask,
                           lambda quta: max(quta, int(max_calls)))

    def _write(self, service: str, key_id: str, period: str, max_calls: int,
               key_mask: str, mutate) -> dict:
        label = window_label(period)
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                'SELECT quta, key_mask FROM usage WHERE service=? AND key_id=? AND limit_key=?',
                (service, key_id, label),
            ).fetchone()
            old_quta = int(row['quta']) if row else 0
            mask = key_mask or (row['key_mask'] if row else '') or ''
            quta = max(0, int(mutate(old_quta)))
            conn.execute(
                'INSERT INTO usage (service, key_id, limit_key, period, key_mask,'
                ' max_calls, quta, updated_at) VALUES (?,?,?,?,?,?,?,?)'
                ' ON CONFLICT(service, key_id, limit_key) DO UPDATE SET'
                ' quta=excluded.quta, max_calls=excluded.max_calls,'
                ' key_mask=excluded.key_mask, period=excluded.period,'
                ' updated_at=excluded.updated_at',
                (service, key_id, label, period, mask, int(max_calls or 0), quta, now),
            )
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        return {'service': service, 'keyId': key_id, 'keyMask': mask,
                'limitKey': label, 'limit': int(max_calls or 0), 'quta': quta}

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
