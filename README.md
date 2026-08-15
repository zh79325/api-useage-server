# api-useage-server

轻量级 API 用量控制服务：给每个第三方 API key 按统计窗口（天/月）**发调用许可**，多台机器 /
多个进程共用同一批 key 时不会把额度合起来打穿。

写入接口只有一个：`acquire`（判定余额 + 扣减在同一事务里）。**拿不到许可就是额度用完了**；
响应带回 `quta`/`limit`/`remaining`，调用方直接覆写本地镜像，远程挂了本地也拦得住。

**技术栈：纯 Python 标准库 —— HTTP 层 `http.server.ThreadingHTTPServer`（不用 Flask/FastAPI，
不需 uvicorn/gunicorn），存储 `sqlite3` 单文件（WAL）。无第三方依赖、无配置文件、无鉴权，
Python >= 3.8，拷到任何服务器上 `./init.sh && ./run.sh start` 就能跑。**

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
| `limit` | 配置上限（0 = 只统计不拦截），上限 1e12 |
| `quta` | 该窗口已用量（可以是调用次数，也可以是 token 数）|

## 接口

请求体统一 JSON，**无鉴权**（只在内网/信任网络里跑）。
**key 只传 `keyId` = `md5(完整key)` 的 32 位小写十六进制；传 `apiKey` 或非 md5 的 `keyId` 直接 400。**

| 方法 | 路径 | 入参 | 返回 |
| --- | --- | --- | --- |
| POST | `/api/usage/acquire` | `service` `keyId` `period` `maxCalls` `keyMask` `delta`(默认1) `exhausted`(可选) | `{"granted", "quta", "limit", "remaining", "limitKey", "keyId", "keyMask"}` |
| POST | `/api/usage/snapshot` | `service` `period` | `{"items": [{...}]}`，当前窗口全部 key |
| GET | `/api/usage/all?rows=500` | - | `{"items": [{...}]}`，含历史窗口 |
| GET | `/health` | - | `{"ok": true, "db": "..."}` |

acquire 语义：

- 额度够 → `granted=true` 且 `quta += delta`
- 额度不够 → `granted=false`，**不扣减**（`delta` 超过剩余时整批拒发，不留半截）
- `delta` 取值 `1~100000000`（1 亿），`maxCalls` 上限 `1e12`：按次记账传 1，按 token 记账直接传本次消耗的 token 数
- `maxCalls=0`（只统计不拦截）→ 永远批准，`remaining` 为 `null`
- `exhausted=true` → 第三方实报额度用尽/key 失效时用，`quta` 拉到 `limit` 并返回 `granted=false`
- 后续请求没带 `maxCalls` 也按已记录的上限判定
- 参数错 → 400，路径错 → 404，存储异常 → 500，返回体统一 `{"error": "..."}`

示例：

```bash
KEY_ID=$(python3 -c "import hashlib;print(hashlib.md5(b'完整key').hexdigest())")

curl -s -X POST http://127.0.0.1:2697/api/usage/acquire \
  -H 'Content-Type: application/json' \
  -d "{\"service\":\"byte_search_api_keys\",\"keyId\":\"$KEY_ID\",\"period\":\"month\",\"maxCalls\":500}"
# {"granted": true, "quta": 1, "limit": 500, "remaining": 499, ...}

curl -s http://127.0.0.1:2697/api/usage/all
```

## 客户端接入（ai-skills）

**服务器地址只在密钥文件里维护**：`wx_gzh_article_writer/config/api_secrets.json` 加一段，
即刻生效（远程优先，超时/报错自动回退本地 `api_usage.json`）：

```json
{
  "usage_server": {
    "base_url": "http://<服务器IP>:2697",
    "timeout_seconds": 2,
    "retry_interval_seconds": 60,
    "enabled": true
  }
}
```

- 换服务器只改 `base_url`；不读环境变量、没有 CLI 入参，就这一处配置
- `enabled: false` 或 `base_url` 留空 = 不启用远程，完全走本地文件
- 客户端只上报 `md5(key)` 与掩码，真实 key 不出本机
- **本地镜像已满就不再请求远程**；远程失败后 `retry_interval_seconds` 内不再重试，期间按本地镜像判定
- 客户端实现：`scripts/tools/basic/usage_client.py`，许可+镜像+回退在 `scripts/tools/basic/api_usage.py`

## 自测

```bash
python3 test_usage_server.py     # 起真服务打真接口 + 并发记账不丢数
```
