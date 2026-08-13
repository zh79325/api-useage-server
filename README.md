# api-useage-server

轻量级 API 用量统计服务：给每个第三方 API key 按统计窗口（天/月）记账，多台机器 / 多个进程
共用同一批 key 时不会把额度合并打穿。

**技术栈：纯 Python 标准库 —— HTTP 层 `http.server.ThreadingHTTPServer`（不用 Flask/FastAPI，
不需 uvicorn/gunicorn），存储 `sqlite3` 单文件（WAL）。无第三方依赖，Python >= 3.8，
拷到任何服务器上 `./init.sh && ./run.sh start` 就能跑。**

## 快速部署

```bash
git clone git@github.com:zh79325/api-useage-server.git && cd api-useage-server
./init.sh                                   # 自检 python3/sqlite3 → 建 data、logs → 跑自测
./run.sh start                              # 0.0.0.0:2697，日志 logs/server.log
API_USAGE_TOKEN=你的token ./run.sh start     # 带鉴权启动（公网必须带）
./run.sh status                             # 看进程 + /health
```

| 脚本 | 用途 |
| --- | --- |
| `./init.sh` | 首次初始化，幂等；`--skip-test` 跳过自测 |
| `./run.sh` | `start` \| `stop` \| `restart` \| `status` \| `logs` \| `foreground`（前台运行，供 systemd/docker 用） |
| `requirements.txt` | 无第三方依赖，仅为统一部署流程保留 |

**没有配置文件**，运行参数只认环境变量 / 命令行参数：

| 参数 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` | `API_USAGE_HOST` | `127.0.0.1`（`run.sh` 里默认 `0.0.0.0`） |
| `--port` | `API_USAGE_PORT` | `2697` |
| `--db` | `API_USAGE_DB` | `./data/api_usage.db` |
| `--token` | `API_USAGE_TOKEN` | 空（不校验） |

开机自启（systemd）：

```ini
[Service]
WorkingDirectory=/opt/api-useage-server
Environment=API_USAGE_TOKEN=你的token
ExecStart=/opt/api-useage-server/run.sh foreground
Restart=always
```

## 数据模型

一条记录 = `(service, key_id, limit_key)`：

| 字段 | 说明 |
| --- | --- |
| `service` | 服务标识，与调用方 `api_secrets.json` 的字段名一致，如 `byte_search_api_keys` |
| `key_id` | **完整 key 的 md5**（调用方本地算）；真实 key 不进请求、不进日志、不落库 |
| `key_mask` | `RfhR***NkSj`，只用于展示（可不传） |
| `limit_key` | 统计窗口：`period=day` → `YYYY-MM-DD`，`period=month` → `YYYY-MM`。换窗口即新记录，无需清零 |
| `limit` | 配置上限（0 = 只统计不拦截） |
| `quta` | 该窗口已用次数 |

## 接口

请求体统一 JSON；除 `/health` 外都要带 `Authorization: Bearer <token>`（服务端未配 token 时不校验）。
**key 只传 `keyId` = `md5(完整key)` 的32 位小写十六进制；传 `apiKey` 或非 md5 的 `keyId` 直接 400。**

| 方法 | 路径 | 入参 | 返回 |
| --- | --- | --- | --- |
| GET | `/health` | - | `{"ok": true, "db": "..."}` |
| POST | `/api/usage/query` | `service` `keyId` `period` | `{"quta", "limit", "limitKey", "keyId", "keyMask"}` |
| POST | `/api/usage/record` | 同上 + `maxCalls` `keyMask` `delta`(默认 1) | 同上（写后用量） |
| POST | `/api/usage/exhaust` | 同上 + `maxCalls` | 同上（`quta` = `maxCalls`） |
| POST | `/api/usage/snapshot` | `service` `period` | `{"items": [{...}]}`，当前窗口全部 key |
| GET | `/api/usage/all?rows=500` | - | `{"items": [{...}]}`，含历史窗口 |

- `period` 只能是 `day` / `month`
- `maxCalls` 为 0 时 `exhaust` 不改计数（无法表达「满额」）
- 参数错 → 400，token 错 → 401，路径错 → 404，存储异常 → 500，返回体统一 `{"error": "..."}`

示例：

```bash
TOKEN=启动时用的token
KEY_ID=$(python3 -c "import hashlib;print(hashlib.md5(b'完整key').hexdigest())")

curl -s -X POST http://127.0.0.1:2697/api/usage/record \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "{\"service\":\"byte_search_api_keys\",\"keyId\":\"$KEY_ID\",\"period\":\"month\",\"maxCalls\":500}"

curl -s http://127.0.0.1:2697/api/usage/all -H "Authorization: Bearer $TOKEN"
```

## 客户端接入（ai-skills）

**服务器地址只在密钥文件里维护**：`wx_gzh_article_writer/config/api_secrets.json` 加一段，
即刻生效（远程优先，超时/报错自动回退本地 `api_usage.json`）：

```json
{
  "usage_server": {
    "base_url": "http://<服务器IP>:2697",
    "token": "与服务端启动时的 API_USAGE_TOKEN 一致，未配则留空",
    "timeout_seconds": 2,
    "retry_interval_seconds": 60,
    "enabled": true
  }
}
```

- 换服务器只改 `base_url`；不读环境变量、没有 CLI 入参，就这一处配置
- `enabled: false` 或 `base_url` 留空 = 不启用远程，完全走本地文件
- 客户端只上报 `md5(key)` 与掩码，真实 key 不出本机
- 远程失败后 `retry_interval_seconds` 内不再重试（避免每次调用都白等一个超时）
- 客户端实现：`scripts/tools/basic/usage_client.py`，回退逻辑在 `scripts/tools/basic/api_usage.py`

## 自测

```bash
python3 test_usage_server.py     # 起真服务打真接口 + 并发记账不丢数
```
