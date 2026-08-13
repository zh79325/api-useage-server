"""API 用量统计 HTTP 服务（标准库 http.server，无第三方依赖）

启动：
    ./init.sh          # 首次部署：自检 python3、建 data/logs 目录、跑自测
    ./run.sh start     # 启动/停止/重启/看日志：start|stop|restart|status|logs
    python3 server.py --host 0.0.0.0 --port 2697 --db /var/lib/api_usage.db
无配置文件，参数只认命令行与环境变量：
    API_USAGE_HOST / API_USAGE_PORT / API_USAGE_DB / API_USAGE_TOKEN

接口（除 /health 外都要带 `Authorization: Bearer <token>`，未配 token 时不校验）：
    GET  /health                  存活探测
    POST /api/usage/query         查某 key 当前窗口用量
    POST /api/usage/record        记一次调用（quta + delta）
    POST /api/usage/exhaust       标记该 key 当前窗口满额
    POST /api/usage/snapshot      某服务当前窗口全部 key 用量
    GET  /api/usage/all?rows=500  全部记账（含历史窗口）

请求体统一为 JSON。key **只传 `keyId`（完整 key 的 md5）**，真实 key 不进请求也不落库；
要人看得出是哪个 key 就额外传一个 `keyMask`（如 `RfhR***NkSj`）。
"""

import argparse
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from store import PERIODS, UsageStore

DEFAULT_PORT = 2697
MAX_BODY_BYTES = 64 * 1024
_MD5_PATTERN = re.compile(r'^[0-9a-f]{32}$')


class UsageHandler(BaseHTTPRequestHandler):
    server_version = 'api-usage-server/1.0'
    protocol_version = 'HTTP/1.1'

    store: UsageStore = None  # 由 create_server 注入
    token: str = ''

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path in ('/health', '/'):
            self._send_json(HTTPStatus.OK, {'ok': True, 'db': self.store.db_path})
            return
        if path == '/api/usage/all':
            if not self._check_token():
                return
            query = parse_qs(urlparse(self.path).query)
            rows = self._as_int(query.get('rows', ['500'])[0], 500)
            self._send_json(HTTPStatus.OK, {'items': self.store.dump(min(max(rows, 1), 5000))})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {'error': f"未知接口: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/') or '/'
        handlers = {
            '/api/usage/query': self._handle_query,
            '/api/usage/record': self._handle_record,
            '/api/usage/exhaust': self._handle_exhaust,
            '/api/usage/snapshot': self._handle_snapshot,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {'error': f"未知接口: {path}"})
            return
        if not self._check_token():
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            self._send_json(HTTPStatus.OK, handler(payload))
        except ValueError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {'error': str(e)})
        except Exception as e:  # 存储异常也要给出明确响应，客户端好回退本地
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {'error': f"{type(e).__name__}: {e}"})

    # ---- 业务处理 ----

    def _handle_query(self, payload: dict) -> dict:
        service, key_id, period, _, _ = _parse_key_args(payload)
        return self.store.query(service, key_id, period)

    def _handle_record(self, payload: dict) -> dict:
        service, key_id, period, max_calls, key_mask = _parse_key_args(payload)
        delta = _parse_int(payload.get('delta'), 1, 'delta')
        if not 1 <= delta <= 1000:
            raise ValueError(f"delta 必须在 1~1000 之间，当前: {delta}")
        return self.store.record(service, key_id, period, max_calls=max_calls,
                                 delta=delta, key_mask=key_mask)

    def _handle_exhaust(self, payload: dict) -> dict:
        service, key_id, period, max_calls, key_mask = _parse_key_args(payload)
        return self.store.exhaust(service, key_id, period, max_calls=max_calls,
                                  key_mask=key_mask)

    def _handle_snapshot(self, payload: dict) -> dict:
        service = _parse_str(payload.get('service'), 'service')
        period = _parse_period(payload.get('period'))
        return {'items': self.store.snapshot(service, period)}

    # ---- 通用 ----

    def _check_token(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get('Authorization') or ''
        provided = header[7:].strip() if header.lower().startswith('bearer ') else ''
        if provided == self.token:
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'token 无效或缺失'})
        return False

    def _read_json(self):
        length = self._as_int(self.headers.get('Content-Length'), 0)
        if length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {'error': '请求体过大'})
            return None
        raw = self.rfile.read(length) if length > 0 else b''
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {'error': f"请求体不是合法 JSON: {e}"})
            return None
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {'error': '请求体必须是 JSON 对象'})
            return None
        return data

    def _send_json(self, status, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(int(status))
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _as_int(raw, default: int) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")


def _parse_str(raw, field: str) -> str:
    value = str(raw or '').strip()
    if not value:
        raise ValueError(f"缺少 {field}")
    return value


def _parse_period(raw) -> str:
    period = str(raw or 'day').strip()
    if period not in PERIODS:
        raise ValueError(f"period 只能是 {PERIODS}，当前: {period}")
    return period


def _parse_int(raw, default: int, field: str) -> int:
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是整数，当前: {raw!r}")
    if value < 0:
        raise ValueError(f"{field} 不能为负: {value}")
    return value


def _parse_key_args(payload: dict):
    """取出 (service, key_id, period, max_calls, key_mask)。

    `keyId` = 完整 key 的 md5，必填；**不接受真实 key**（传了 `apiKey` 直接报错，
    避开一不小心把 key 写进服务端日志）。
    """
    if payload.get('apiKey'):
        raise ValueError('接口不接受真实 key，请改传 keyId（完整 key 的 md5）')
    service = _parse_str(payload.get('service'), 'service')
    period = _parse_period(payload.get('period'))
    max_calls = _parse_int(payload.get('maxCalls'), 0, 'maxCalls')
    key_mask = str(payload.get('keyMask') or '').strip()
    key_id = _parse_str(payload.get('keyId'), 'keyId（完整 key 的 md5）').lower()
    if not _MD5_PATTERN.match(key_id):
        raise ValueError(f"keyId 必须是 32 位 md5 十六进制，当前: {key_id[:16]}...")
    return service, key_id, period, max_calls, key_mask


def create_server(host: str, port: int, db_path: str, token: str = '') -> ThreadingHTTPServer:
    """建好 HTTP 服务（未启动），store/token 挂在 handler 类上共享。"""
    store = UsageStore(db_path)
    handler = type('BoundUsageHandler', (UsageHandler,), {'store': store, 'token': token})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(description='API 用量统计服务')
    parser.add_argument('--host', default=os.environ.get('API_USAGE_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('API_USAGE_PORT', DEFAULT_PORT)))
    parser.add_argument('--db', default=os.environ.get(
        'API_USAGE_DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'api_usage.db')))
    parser.add_argument('--token', default=os.environ.get('API_USAGE_TOKEN', ''),
                        help='访问 token，留空则不校验')
    args = parser.parse_args(argv)

    httpd = create_server(args.host, args.port, args.db, args.token)
    print(f"[INFO] API 用量服务已启动: http://{args.host}:{args.port}  db={os.path.abspath(args.db)}"
          f"  auth={'on' if args.token else 'off'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[INFO] 收到中断信号，退出')
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
