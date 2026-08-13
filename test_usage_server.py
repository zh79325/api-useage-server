"""服务端自测：起真服务打真接口（python3 test_usage_server.py）。"""

import json
import hashlib
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import create_server  # noqa: E402
from store import UsageStore, key_id_of, mask_key, window_label  # noqa: E402

TOKEN = 'test-token'


class ServerTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = create_server('127.0.0.1', 0, os.path.join(cls.tmp.name, 'usage.db'), TOKEN)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def post(self, path, payload, token=TOKEN):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        if token:
            req.add_header('Authorization', f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def get(self, path, token=TOKEN):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header('Authorization', f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def test_health_no_token_needed(self):
        status, body = self.get('/health', token='')
        self.assertEqual(200, status)
        self.assertTrue(body['ok'])

    def test_token_required(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post('/api/usage/query',
                      {'service': 's', 'keyId': key_id_of('k'), 'period': 'day'}, token='')
        self.assertEqual(401, ctx.exception.code)

    def test_record_and_query(self):
        args = {'service': 'byte_search_api_keys', 'keyId': key_id_of('AAAABBBBCCCCDDDD'),
                'keyMask': mask_key('AAAABBBBCCCCDDDD'), 'period': 'month', 'maxCalls': 500}
        _, first = self.post('/api/usage/record', args)
        self.assertEqual(1, first['quta'])
        self.assertEqual(window_label('month'), first['limitKey'])
        self.assertEqual('AAAA***DDDD', first['keyMask'])
        self.assertEqual(32, len(first['keyId']))

        _, second = self.post('/api/usage/record', dict(args, delta=3))
        self.assertEqual(4, second['quta'])

        _, queried = self.post('/api/usage/query', args)
        self.assertEqual(4, queried['quta'])
        self.assertEqual(500, queried['limit'])

    def test_query_unknown_key_is_zero(self):
        _, body = self.post('/api/usage/query',
                            {'service': 'x', 'keyId': key_id_of('never-used'), 'period': 'day'})
        self.assertEqual(0, body['quta'])

    def test_real_key_rejected(self):
        """接口不收真实 key：传 apiKey 或非 md5 的 keyId 一律 400。"""
        for payload in ({'service': 's', 'apiKey': 'REAL_KEY', 'period': 'day'},
                        {'service': 's', 'keyId': 'REAL_KEY', 'period': 'day'},
                        {'service': 's', 'keyId': 'a' * 31, 'period': 'day'},
                        {'service': 's', 'keyId': 'z' * 32, 'period': 'day'}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post('/api/usage/record', payload)
            self.assertEqual(400, ctx.exception.code)

    def test_exhaust_marks_full(self):
        args = {'service': 'baidu_search_api_keys', 'keyId': key_id_of('dead-key-1'),
                'period': 'day', 'maxCalls': 500}
        self.post('/api/usage/record', args)
        _, body = self.post('/api/usage/exhaust', args)
        self.assertEqual(500, body['quta'])
        _, queried = self.post('/api/usage/query', args)
        self.assertEqual(500, queried['quta'])

    def test_exhaust_without_max_calls_keeps_count(self):
        args = {'service': 'no_limit_service', 'keyId': key_id_of('free-key'), 'period': 'day'}
        self.post('/api/usage/record', args)
        _, body = self.post('/api/usage/exhaust', args)
        self.assertEqual(1, body['quta'])

    def test_snapshot_and_all(self):
        service = 'snapshot_service'
        for key in ('key-one', 'key-two'):
            self.post('/api/usage/record',
                      {'service': service, 'keyId': key_id_of(key), 'period': 'day',
                       'maxCalls': 10})
        _, body = self.post('/api/usage/snapshot', {'service': service, 'period': 'day'})
        self.assertEqual(2, len(body['items']))
        self.assertEqual({key_id_of('key-one'), key_id_of('key-two')},
                         {item['keyId'] for item in body['items']})

        _, dumped = self.get('/api/usage/all?rows=100')
        self.assertTrue(any(item['service'] == service for item in dumped['items']))

    def test_bad_request(self):
        for payload in ({'keyId': key_id_of('k')},
                        {'service': 's', 'keyId': key_id_of('k'), 'period': 'week'},
                        {'service': 's', 'period': 'day'}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post('/api/usage/query', payload)
            self.assertEqual(400, ctx.exception.code)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get('/api/nope')
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
        self.store.record('svc', key_id, 'day', max_calls=5)
        with self.store._connect() as conn:  # 手工塞一条历史窗口记录
            conn.execute('INSERT INTO usage (service, key_id, limit_key, period, key_mask,'
                         ' max_calls, quta, updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('svc', key_id, '1999-01-01', 'day', '', 5, 5, 'x'))
        self.assertEqual(1, self.store.query('svc', key_id, 'day')['quta'])

    def test_concurrent_record(self):
        """多线程并发记账不丢计数。"""
        key_id = key_id_of('hot-key')
        stores = [UsageStore(self.store.db_path) for _ in range(4)]

        def worker(store):
            for _ in range(25):
                store.record('svc', key_id, 'day', max_calls=1000)

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(100, self.store.query('svc', key_id, 'day')['quta'])

    def test_mask_key(self):
        self.assertEqual('AAAA***DDDD', mask_key('AAAABBBBCCCCDDDD'))
        self.assertEqual('ab***', mask_key('abcd'))
        self.assertEqual('***', mask_key(''))

    def test_key_id_is_md5(self):
        self.assertEqual(hashlib.md5(b'some-key').hexdigest(), key_id_of('some-key'))
        self.assertEqual(32, len(key_id_of('some-key')))

    def test_window_label_invalid_period(self):
        with self.assertRaises(ValueError):
            window_label('week')


if __name__ == '__main__':
    unittest.main(verbosity=2)
