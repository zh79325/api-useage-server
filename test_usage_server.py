"""服务端自测：起真服务打真接口（python3 test_usage_server.py）。"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import create_server  # noqa: E402
from store import (UsageStore, day_offset_of, key_id_of, mask_key,  # noqa: E402
                   normalize_period, window_label)


class ServerTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = create_server('127.0.0.1', 0, os.path.join(cls.tmp.name, 'usage.db'))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def acquire(self, service, key, period='day', max_calls=0, **extra):
        payload = {'service': service, 'keyId': key_id_of(key), 'keyMask': mask_key(key),
                   'period': period, 'maxCalls': max_calls}
        payload.update(extra)
        return self.post('/api/usage/acquire', payload)[1]

    def test_health_needs_no_auth(self):
        status, body = self.get('/health')
        self.assertEqual(200, status)
        self.assertTrue(body['ok'])

    def test_acquire_grants_until_limit(self):
        """额度内批准并扣减，到顶后拒发且不再累加。"""
        args = ('byte_search_api_keys', 'AAAABBBBCCCCDDDD', 'month', 3)
        for expected in (1, 2, 3):
            body = self.acquire(*args)
            self.assertTrue(body['granted'], body)
            self.assertEqual(expected, body['quta'])
            self.assertEqual(3 - expected, body['remaining'])
        self.assertEqual(window_label('month'), body['limitKey'])
        self.assertEqual('AAAA***DDDD', body['keyMask'])

        denied = self.acquire(*args)
        self.assertFalse(denied['granted'], denied)
        self.assertEqual(3, denied['quta'])          # 拒发不扣减
        self.assertEqual(0, denied['remaining'])

    def test_delta_larger_than_remaining_is_denied(self):
        """delta 超过剩余额度时整批拒发，不留半截。"""
        args = ('svc_delta', 'delta-key', 'day', 10)
        self.assertTrue(self.acquire(*args, delta=8)['granted'])
        denied = self.acquire(*args, delta=5)
        self.assertFalse(denied['granted'])
        self.assertEqual(8, denied['quta'])
        self.assertTrue(self.acquire(*args, delta=2)['granted'])

    def test_no_limit_always_granted(self):
        """maxCalls=0 只统计不拦截：永远批准。"""
        for expected in (1, 2, 3):
            body = self.acquire('svc_free', 'free-key')
            self.assertTrue(body['granted'])
            self.assertEqual(expected, body['quta'])
            self.assertIsNone(body['remaining'])

    def test_token_scale_delta_and_limit(self):
        """token 场景：百万级 delta / 千万级 maxCalls 正常记账。"""
        args = ('token_svc', 'token-key', 'month', 5_000_000)
        body = self.acquire(*args, delta=1861)
        self.assertTrue(body['granted'], body)
        self.assertEqual(1861, body['quta'])

        body = self.acquire(*args, delta=3_000_000)
        self.assertTrue(body['granted'], body)
        self.assertEqual(3_001_861, body['quta'])
        self.assertEqual(5_000_000 - 3_001_861, body['remaining'])

        denied = self.acquire(*args, delta=2_000_000)
        self.assertFalse(denied['granted'], denied)
        self.assertEqual(3_001_861, denied['quta'])

    def test_delta_over_hard_cap_is_rejected(self):
        """超过硬上限的 delta 仍然 400，防止误传天文数字打爆额度。"""
        payload = {'service': 's', 'keyId': key_id_of('k'), 'period': 'day',
                   'delta': 100_000_001}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post('/api/usage/acquire', payload)
        self.assertEqual(400, ctx.exception.code)

    def test_exhausted_marks_full(self):
        """第三方实报额度用尽：拉满并拒发，后续 acquire 也拒发。"""
        args = ('baidu_search_api_keys', 'dead-key', 'day', 500)
        self.acquire(*args)
        body = self.acquire(*args, exhausted=True)
        self.assertFalse(body['granted'])
        self.assertEqual(500, body['quta'])
        self.assertFalse(self.acquire(*args)['granted'])

    def test_exhausted_without_limit_keeps_count(self):
        args = ('svc_free2', 'free-key-2', 'day', 0)
        self.acquire(*args)
        body = self.acquire(*args, exhausted=True)
        self.assertFalse(body['granted'])
        self.assertEqual(1, body['quta'])

    def test_limit_remembered_from_previous_acquire(self):
        """后续请求没带 maxCalls 也按已记录的上限判定。"""
        self.acquire('svc_sticky', 'sticky-key', 'day', 2)
        self.acquire('svc_sticky', 'sticky-key', 'day', 2)
        body = self.acquire('svc_sticky', 'sticky-key', 'day', 0)
        self.assertFalse(body['granted'], body)
        self.assertEqual(2, body['limit'])

    def test_real_key_rejected(self):
        """接口不收真实 key：传 apiKey 或非 md5 的 keyId 一律 400。"""
        for payload in ({'service': 's', 'apiKey': 'REAL_KEY', 'period': 'day'},
                        {'service': 's', 'keyId': 'REAL_KEY', 'period': 'day'},
                        {'service': 's', 'keyId': 'a' * 31, 'period': 'day'},
                        {'service': 's', 'keyId': 'z' * 32, 'period': 'day'}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post('/api/usage/acquire', payload)
            self.assertEqual(400, ctx.exception.code)

    def test_snapshot_and_all(self):
        service = 'snapshot_service'
        for key in ('key-one', 'key-two'):
            self.acquire(service, key, 'day', 10)
        _, body = self.post('/api/usage/snapshot', {'service': service, 'period': 'day'})
        self.assertEqual({key_id_of('key-one'), key_id_of('key-two')},
                         {item['keyId'] for item in body['items']})

        _, dumped = self.get('/api/usage/all?rows=100')
        self.assertTrue(any(item['service'] == service for item in dumped['items']))

    def test_bad_request(self):
        for payload in ({'keyId': key_id_of('k')},
                        {'service': 's', 'keyId': key_id_of('k'), 'period': 'week'},
                        {'service': 's', 'keyId': key_id_of('k'), 'period': 'day+24H'},
                        {'service': 's', 'keyId': key_id_of('k'), 'period': 'day+H'},
                        {'service': 's', 'period': 'day'},
                        {'service': 's', 'keyId': key_id_of('k'), 'period': 'day', 'delta': 0}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post('/api/usage/acquire', payload)
            self.assertEqual(400, ctx.exception.code)

    def test_day_offset_period_over_http(self):
        """`day+11H` 走完整 HTTP 链路：落库 limit_key 带后缀，snapshot 能查回。"""
        key_id = key_id_of('http-offset')
        _, body = self.post('/api/usage/acquire', {
            'service': 'svc-http', 'keyId': key_id, 'period': 'day+11h',
            'maxCalls': 1800000, 'delta': 700,
        })
        self.assertTrue(body['granted'])
        self.assertEqual(700, body['quta'])
        self.assertTrue(body['limitKey'].endswith('+11H'), body['limitKey'])

        _, snap = self.post('/api/usage/snapshot',
                            {'service': 'svc-http', 'period': 'day+11H'})
        self.assertEqual([key_id], [item['keyId'] for item in snap['items']])
        self.assertEqual('day+11H', snap['items'][0]['period'], '归一后的 period 落库')

    def test_removed_endpoints(self):
        """record / exhaust / query 已合并进 acquire，路径不再存在。"""
        for path in ('/api/usage/record', '/api/usage/exhaust', '/api/usage/query'):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post(path, {'service': 's', 'keyId': key_id_of('k'), 'period': 'day'})
            self.assertEqual(404, ctx.exception.code)


class StoreTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UsageStore(os.path.join(self.tmp.name, 'sub', 'usage.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_window_isolation(self):
        """跨窗口的记录互不干扰（旧窗口不影响当前窗口计数）。"""
        key_id = key_id_of('k1')
        self.store.acquire('svc', key_id, 'day', max_calls=5)
        with self.store._connect() as conn:  # 手工塞一条历史窗口记录
            conn.execute('INSERT INTO usage (service, key_id, limit_key, period, key_mask,'
                         ' max_calls, quta, updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('svc', key_id, '1999-01-01', 'day', '', 5, 5, 'x'))
        self.assertEqual(1, self.store.query('svc', key_id, 'day')['quta'])

    def test_concurrent_acquire_never_oversells(self):
        """多线程抢 100 个额度：批准恰好 100 次，计数不超发。"""
        key_id = key_id_of('hot-key')
        stores = [UsageStore(self.store.db_path) for _ in range(4)]
        granted = []
        lock = threading.Lock()

        def worker(store):
            hits = sum(1 for _ in range(40)
                       if store.acquire('svc', key_id, 'day', max_calls=100)['granted'])
            with lock:
                granted.append(hits)

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(100, sum(granted))
        self.assertEqual(100, self.store.query('svc', key_id, 'day')['quta'])

    def test_mask_key(self):
        self.assertEqual('AAAA***DDDD', mask_key('AAAABBBBCCCCDDDD'))
        self.assertEqual('ab***', mask_key('abcd'))
        self.assertEqual('***', mask_key(''))

    def test_key_id_is_md5(self):
        self.assertEqual(hashlib.md5(b'some-key').hexdigest(), key_id_of('some-key'))
        self.assertEqual(32, len(key_id_of('some-key')))

    def test_window_label_invalid_period(self):
        for bad in ('week', 'day+24H', 'day+H', 'day+11', ''):
            with self.assertRaises(ValueError):
                window_label(bad)

    def test_normalize_period_day_offset(self):
        """`day+nH` 归一：大小写不敏感、前导零去掉（否则同一份额度记两条账）。

        与调用方 ai-skills 的 tools/basic/api_usage.normalize_period 必须完全一致。
        """
        self.assertEqual('day', normalize_period('day'))
        self.assertEqual('month', normalize_period('month'))
        self.assertEqual('day+11H', normalize_period('day+11H'))
        self.assertEqual('day+11H', normalize_period('day+11h'))
        self.assertEqual('day+9H', normalize_period('day+09H'))
        self.assertEqual('day+0H', normalize_period('day+0H'))
        self.assertEqual('day+23H', normalize_period('day+23H'))
        self.assertEqual(11, day_offset_of('day+11H'))
        self.assertIsNone(day_offset_of('day'))
        self.assertIsNone(day_offset_of('month'))

    def test_window_label_day_offset_boundary(self):
        """窗口标识取起始日，11 点分界；期望值与客户端测试里硬编码的一致。"""
        cases = [
            (datetime(2026, 8, 16, 10, 59), '2026-08-15+11H'),
            (datetime(2026, 8, 16, 11, 0), '2026-08-16+11H'),
            (datetime(2026, 8, 16, 23, 59), '2026-08-16+11H'),
            (datetime(2026, 8, 16, 0, 0), '2026-08-15+11H'),
        ]
        for now, expect in cases:
            self.assertEqual(expect, window_label('day+11H', now=now), now)
        # 与自然日 label 不同名：调用方切 period 后旧记录不会被当成本窗口
        self.assertEqual('2026-08-16', window_label('day', now=datetime(2026, 8, 16, 12, 0)))

    def test_day_offset_window_isolation(self):
        """偏移窗口下旧窗口记录不影响当前窗口，且与自然日记录各记一条。"""
        key_id = key_id_of('k-offset')
        self.store.acquire('svc', key_id, 'day+11H', max_calls=1800000, delta=700)
        current = window_label('day+11H')
        self.assertEqual(700, self.store.query('svc', key_id, 'day+11H')['quta'])
        self.assertEqual(current, self.store.query('svc', key_id, 'day+11H')['limitKey'])
        self.assertTrue(current.endswith('+11H'), current)

        # 上一个偏移窗口打满，不影响当前窗口
        with self.store._connect() as conn:
            conn.execute('INSERT INTO usage (service, key_id, limit_key, period, key_mask,'
                         ' max_calls, quta, updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('svc', key_id, '1999-01-01+11H', 'day+11H', '',
                          1800000, 1800000, 'x'))
        self.assertEqual(700, self.store.query('svc', key_id, 'day+11H')['quta'])

        # 自然日与 day+11H 是两条独立的账（label 不同名）
        self.store.acquire('svc', key_id, 'day', max_calls=1800000, delta=5)
        self.assertEqual(5, self.store.query('svc', key_id, 'day')['quta'])
        self.assertEqual(700, self.store.query('svc', key_id, 'day+11H')['quta'])

    def test_day_offset_snapshot(self):
        """snapshot 按偏移窗口取当前窗口的条目，period 原样带回。"""
        key_id = key_id_of('k-snap')
        self.store.acquire('svc-snap', key_id, 'day+11H', max_calls=100, delta=3)
        items = self.store.snapshot('svc-snap', 'day+11H')
        self.assertEqual(1, len(items), items)
        self.assertEqual('day+11H', items[0]['period'])
        self.assertEqual(window_label('day+11H'), items[0]['limitKey'])
        self.assertEqual([], self.store.snapshot('svc-snap', 'day'), '自然日窗口下查不到')


if __name__ == '__main__':
    unittest.main(verbosity=2)
