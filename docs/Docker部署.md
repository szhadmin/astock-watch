# Docker 部署

两种用法：**本机/内网**用 `docker-compose.yml`；**公网服务器**用 `docker-compose.prod.yml`（自带 nginx 密码认证）。

## 一、准备配置（必须）

```bash
cp config.example.json config.json     # Windows: copy config.example.json config.json
```

编辑 `config.json`：

| 字段 | 说明 |
|---|---|
| `ai.provider` / `base_url` / `model` / `api_key` | 模型网关（必填） |
| `notify.email.*` | 邮件通知（可选，QQ/163 用授权码） |
| `ai.schedule.enabled` | 是否开启自动调度 |

> `config.json` 含密钥，已在 `.gitignore` 与 `.dockerignore` 中排除，**不会进镜像**。

## 二、本机 / 内网部署

```bash
docker compose up -d --build
docker compose logs -f          # 看日志
```
访问 <http://127.0.0.1:8770>（默认只绑本机）。

## 三、公网服务器部署（带密码认证）

应用容器**不发布端口**，只在一个 docker 内网里；对外只暴露 nginx，nginx 上有 HTTP Basic 认证：

```bash
# 1. 生成访问密码（用户名 admin）
printf 'admin:%s\n' "$(openssl passwd -apr1 '你的密码')" > docker/.htpasswd
chmod 644 docker/.htpasswd        # 必须 644：容器里的 nginx 用户要能读

# 2. 构建并启动
docker compose -f docker-compose.prod.yml up -d --build

# 3. 验证
curl -u admin:你的密码 http://127.0.0.1:8770/api/overview
```

浏览器打开 `http://<服务器IP>:8770`，会弹出原生登录框。

> **端口权限**：除了服务器本机防火墙（`ufw`），云厂商的**安全组**也要放行 8770。

## 三之二、用域名 + HTTPS（推荐，服务器上已有 Caddy）

如果服务器上已经有 Caddy（或 nginx）在管 80/443，最省事的做法是：**应用只绑 127.0.0.1，由反向代理对外**。

`docker-compose.prod.yml` 里把端口改成只绑本机：

```yaml
    ports:
      - "127.0.0.1:8770:8770"
```

在 `Caddyfile` 里加一段（Caddy 会自动申请 Let's Encrypt 证书，前提是该子域名的 A 记录指向本机）：

```caddyfile
astock.example.com {
    reverse_proxy 127.0.0.1:8770 {
        flush_interval -1
        transport http {
            read_timeout 300s
            write_timeout 300s
        }
    }
    encode gzip
}
```

然后 `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`，
几十秒后 `https://astock.example.com` 就能用（证书在 `/var/lib/caddy/.local/share/caddy/certificates/`）。

> 有泛域名解析（`*.example.com` → 本机）时，新子域名无需再单独加 A 记录。



## 四、数据持久化（重要）

| 宿主机 | 容器内 | 内容 |
|---|---|---|
| `./config.json` | `/app/config.json`（只读） | 模型密钥、通知配置 |
| `./data` | `/app/data` | 账户/成交、AI 手册与日记、规则库、回放样本、日报归档 |
| `./backups` | `/app/backups` | 自动备份 zip（保留 30 份） |

`data/` 是全部资产所在，**必须挂载**，否则容器重建即丢。迁移到新机器时，把 `data/` 整目录拷过去即可无缝继续。

## 五、启用财报/股东/龙虎榜（可选）

默认镜像零第三方依赖。需要 akshare 时：

```bash
docker compose -f docker-compose.prod.yml build --build-arg WITH_AKSHARE=1
docker compose -f docker-compose.prod.yml up -d
```
镜像会大约 +600MB，构建更慢；不需要就不加。

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| nginx 报 500、日志 `open() ".htpasswd" failed (13: Permission denied)` | `chmod 644 docker/.htpasswd` |
| 页面能开但接口 404 | 镜像太旧：`docker compose ... up -d --build` 重建 |
| 首页空白、容器 unhealthy | `docker compose logs app` 看是不是 config.json 缺失或密钥为空 |
| 外网打不开、本机 curl 正常 | 云厂商安全组没放行端口 |
| 东财行情拉不到 | 服务器在境外时常被限频；国内机房正常 |
| 出现两份成交 | 本机服务和容器**同时在跑**，两个调度器共用同一份数据。只保留一个 |

## 七、安全提醒

- 控制台**能花你的模型费用**、能看你的持仓，务必开启密码认证（prod 文件已内置）；
- 现在是 HTTP 明文，公网建议用域名 + Caddy/Nginx 申请证书后加一层 HTTPS；
- 密码泄露就重设：重新生成 `docker/.htpasswd` 后 `docker compose ... restart web`；
- 容器以 root 运行，若在意可用 `user:` 降权并保证 `data/` 目录可写。
