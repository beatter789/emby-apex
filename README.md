# Emby Apex

外挂式的 Emby 管理平台 Emby Apex。不改动 Emby 本身，通过官方 HTTP API 从旁边管理多台服务器：看谁在播放什么、启停用户、设置到期时间并在到期后自动停用。

Docker 运行，数据存 SQLite。一个进程在两个端口监听：管理后台（默认 8000）和用户端账户中心（默认 8080，可反代到公网）；两套路由和登录 Cookie 仍然完全隔离。

## 功能

- **多服务器**：同一后台接入任意数量的 Emby，逐台独立配置地址与 API Key。
- **实时播放**：正在播放的用户、片名/剧集、客户端与设备、播放进度百分比、暂停状态、来源 IP，带封面图。可一键掐断某个会话。
- **用户启停**：直接改 Emby 侧的 `IsDisabled` 策略，停用后该用户立刻无法登录。
- **细粒度用户权限**：管理端按播放、转码、访问、设备、可见性、媒体库、下载和直播等分组编辑 Emby Policy；媒体库支持“全部”或指定列表。
- **管理员创建用户**：可按普通注册模式创建待激活账号，或直接创建已激活账号并选择播放权限、到期时间；两种模式创建后都能立即登录 portal。
- **到期时间**：给用户设到期时间；portal 账户到期后收回播放权限并保留登录以便续期，其他账户按策略停用，随后进入回收流程。到期前若干天会提前提醒。
- **企业微信通知**：支持企业微信自建应用发送文本通知，可配置代理、成员白名单和回调验签参数。
- **客户端限制**：按客户端名称设白名单或黑名单，命中规则的会话在 Emby 播放 Webhook 到达时自动停止。
- **历史统计**：按天维度汇总播放次数与时长，看用户榜、片子榜、客户端榜。
- **操作日志**：所有变更（人工与自动）都留痕。
- **自助注册**：用户自己注册，账号在 Emby 侧建好但播放权限全关，只能看媒体库封面。注册后在宽限期内（默认 24 小时）不用兑换码激活就连号一起删除。
- **注册服务器选择**：管理端开启开放注册后，可在设置页选择启用的默认注册服务器；portal 注册请求按数据库中的最新开关和目标服务器执行。
- **兑换码**：后台批量生成，时长自定义（分/时/天/年）。用户在账户中心兑换即开权限、续期。
- **用户端账户中心**：和管理后台分端口，用户用自己的 Emby 账号登录，查看到期时间与账户状态、兑换兑换码、改用户名和密码。
- **用户端登录开通**：管理端用户详情可开关“开通用户端登录”；已有 Emby 用户首次开通时设置初始密码，管理员账号不允许开通。
- **求片系统**：用户端通过后台配置的 TMDB Key 搜索电影/电视剧、查看详情、填写备注并提交；管理端按服务器和 TMDB 作品合并确认入库或拒绝。
- **iOS 主屏 Web App**：管理端和用户端均提供 favicon、Apple 主屏图标、manifest 和安全区域适配，统一使用 `logoicon.png`。
- **播放写入优化**：播放会话由 Emby Webhook 事件驱动并在内存聚合，只在停止事件或进程正常退出时写入历史；关键业务数据仍立即提交，SQLite 保持 WAL + `synchronous=FULL`。
- **注册/到期通知**：自助注册、管理员创建、老账号自助认领、管理员批准老账号开通 portal 和兑换激活发送 `registration` 通知；到期提醒、停用/收回播放和到期删除发送 `expiry` 通知。通知在业务提交后执行，三渠道按分类独立开关且故障隔离，不会回滚业务。
- **日志凭据防护**：启动时统一将 `httpx`/`httpcore` 请求日志限制为 WARNING，并对所有日志记录中的 URL 查询参数、Authorization、常见 Secret/Token/API Key 变体、用户信息和异常堆栈做脱敏；日志只保留可诊断的主机、路径、状态码、渠道和错误类型，不输出请求头、请求体或凭据原文。

## 快速开始

### 使用 GitHub Container Registry 镜像

公开镜像发布在 GHCR。首次启动前请修改 Compose 中的 `APEX_PASSWORD`，并准备数据与海报目录：

```bash
mkdir -p data image
docker compose -f docker-compose.example.yml up -d
```

也可以直接拉取镜像：

```bash
docker pull ghcr.io/beatter789/emby-apex:latest
```

### 本地构建

仓库中的 `docker-compose.yml` 仅用于本地测试，已加入 `.gitignore`，不会上传到 GitHub。它会从当前目录构建镜像：

```bash
docker compose -f docker-compose.yml up -d --build
```

`docker-compose.example.yml` 才是公开仓库中的 Compose 示例，使用已经发布的 GHCR 镜像。

Compose 服务、镜像和容器标识为 `emby-apex`；项目目录仍保留为 `emby-controller` 以兼容现有部署路径。

启动一个容器，在同一进程内监听两个端口，共用 `./data` 下的同一个库：

- 管理后台 `http://127.0.0.1:8000`，默认账号 `admin` / `admin`。生产环境建议在反向代理或防火墙层限制管理端访问。
- 用户端账户中心 `http://<宿主机>:8080`，给普通用户注册和登录用。

定时任务（未激活清理、到期回收）只在管理进程里跑，portal 不跑，避免同一批清理执行两遍。

Compose 已直接写入启动环境变量，默认后台账号为 `admin` / `admin`；修改账号密码或 Cookie/数据目录等启动项时，直接编辑 `docker-compose.yml`，不需要创建 `.env` 文件。

### 接入一台 Emby

进「服务器」页，填名称、地址（如 `http://192.168.1.10:8096`）、API Key。

API Key 在 Emby 后台的「高级 → API 密钥」里新建。控制器需要它来读取会话和修改用户策略，所以这把 Key 权限不小 —— 它在数据库里是加密存储的（加密密钥首次启动时自动生成，存于数据目录的 `secret.key`），但仍建议只在受信任的内网环境部署。

保存后点「同步用户」把该服务器的用户拉进来。

### 配置 Emby 播放 Webhook

播放监控由 Emby Webhook 推送，不再周期请求 `/Sessions`。在 Emby 的 Webhook/通知设置中新增回调地址：

`http://你的控制器地址:8000/webhook?token=embyapex`

勾选播放开始、播放进度、暂停/恢复和播放停止事件。控制器按 Webhook 中的服务器名称或地址识别目标服务器；仅配置一台服务器时也支持省略服务器元数据。开始、进度和暂停/恢复只更新内存中的当前播放，停止事件才写入播放历史。portal 端口 `8080` 不提供该回调。

## 配置项

**运营类配置一律在后台「设置」页改，改完立即生效，不用重启容器。** 包括注册开关、未激活保留小时数、到期宽限天数、到期提醒天数、管理端总览刷新与到期检查间隔、portal 密码最短长度、是否允许用户在 Emby 客户端自助改密、通知 Webhook、Telegram 和企业微信。这些值存在数据库里，Compose 中的启动环境变量只作为**首次启动的初始值**；库里已有记录后，修改 Compose 默认值不会覆盖数据库设置。

环境变量只剩下启动期就得定的少数几项，均直接写在 `docker-compose.yml` 的 `environment` 中：

Docker Compose 固定映射管理端 `8000:8000`、用户端 `8080:8080`，并直接设置 `TZ=Asia/Shanghai`；代理、TMDB Key 和求片保留期只在后台设置页配置。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APEX_USER` / `APEX_PASSWORD` | `admin` / `admin` | 后台登录账号（直接改 Compose） |
| `APEX_ADMIN_PORT` / `APEX_PORTAL_PORT` | `8000` / `8080` | 容器内两个监听端口 |
| `APEX_COOKIE` | `false` | 挂 HTTPS 反代后设 `true` |
| `APEX_DATA` | `/data` | SQLite 库与 `secret.key` |
| `APEX_IMAGE` | `/image` | 已确认求片海报目录 |

### 企业微信通知

在后台「设置」页填写企业微信自建应用的企业 ID（CorpID）、应用 AgentID 和 Secret，保存后即可发送注册、到期和求片通知。Secret 使用与 Emby API Key 相同的 `secret.key` 加密后存入数据库；设置页不会回显，留空表示保留已配置值。

`企业微信 API 中转地址` 默认是 `https://qyapi.weixin.qq.com`，也可以填写 Movie-Pilot 文档所说的固定公网 IP 反向代理地址；`通知 HTTP 代理地址` 则是本机访问网络时使用的 HTTP/HTTPS/SOCKS5 代理，三种通知渠道可以分别勾选是否使用。两者不能混填。

日志级别在后台「设置」页调整，默认 `WARNING`，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`。设为 `INFO` 时会记录安全脱敏后的企业微信回复、通知发送、测试消息和 Emby Webhook 事件；凭据、请求体和完整 URL 查询值仍不会记录。总览“最近动作”只保留主要运营动作 10 条，`/api/v1/logs` 显示全部日志，支持 `level=info|warning|error` 和 `page` 分页，每页 10 条。

企业微信管理端还需要把控制器访问企业微信 API 的出口公网 IP 加入应用的「可信 IP」；否则 `gettoken` 和发送消息都会被拒绝。

管理员白名单填写允许接收企业微信通知并使用管理菜单和命令的成员 `userid`，使用英文逗号分隔，例如 `zhangsan,lisi`。白名单非空时通知只发给这些成员；留空时通知发给应用可见范围内的 `@all`，同时应用可见范围内的成员都可执行命令。

如果还要在企业微信管理端配置应用回调，请填写 Token 和 EncodingAESKey，并将唯一回调 URL 指向 `https://你的域名/wechat`（直连公网时为 `http://公网IP:8000/wechat`）。程序按官方文档 10514 支持 GET 验证、POST 查询签名校验和 AES 解密；回调只接受 XML，主动 OpenAPI 才使用 JSON。反向代理必须保留 GET/POST、查询参数和原始 XML 请求体。程序可通过文本消息或菜单执行管理员命令：`帮助`、`状态`、`用户`、`会话`、`同步`、`启用`、`停用`、`停止`，以及账户和激活码管理命令。Token 和 EncodingAESKey 只用于回调，不影响主动发送消息；项目不提供旧 `/wecom/callback` 路径。

应用菜单通过正式版本化接口 `POST /api/v1/settings/wecom/menu/sync` 创建、`GET /api/v1/settings/wecom/menu` 查询、`POST` 或 `DELETE /api/v1/settings/wecom/menu/delete` 删除；这些接口均要求管理员 Session，写操作还要求 CSRF。Vue 设置页已接入菜单按钮，旧 `/settings/wecom/menu/*` 路由未注册；后端接口也可由管理端脚本或 API 客户端调用。菜单固定为三组、9 个子菜单：用户管理（增加用户、删除用户、启用账户、停用账号）、账号激活（生成激活码、删除激活码、查询激活码）、求片（正在求片、已求片）。对应稳定 `EventKey` 为 `user_add`、`user_delete`、`user_enable`、`user_disable`、`code_generate`、`code_delete`、`code_query`、`request_pending`、`request_in_library`，均按企业微信 `click` 菜单执行同名语义命令；“已求片”只查询 `MediaRequest.status=in_library` 的已入库记录。菜单 JSON 遵循一级 `button` 1-3 个、二级 `sub_button` 1-5 个、一级名称不超过 16 字节、二级名称不超过 40 字节、`click.key` 不超过 128 字节的限制。写操作会返回命令格式，实际命令通过企业微信文字发送；同步以及启用/停用/停止属于耗时命令，回调会先返回 `success`，完成后再向发起人定向发送结果。

“删除激活码”菜单改为交互式操作：点击菜单或发送无参数命令后，系统只列出 `used_at IS NULL` 的未使用激活码并按 `1、2、3...` 编号；回复序号会回显待删除项，必须再次回复“确认”才删除。回复“取消”、非法序号或超过 5 分钟均不会删除；确认前如果激活码已被兑换，执行阶段会拒绝删除。交互状态按 userid 隔离、仅保存在当前进程内，旧文本命令 `删除激活码 <激活码> 确认` 继续兼容。

## 数据与备份

SQLite 库和加密密钥 `secret.key` 都落在容器内 `/data`，确认入库后下载的 TMDB `w342` 海报落在 `/image`；compose 分别映射到项目下的 `./data` 和 `./image`，直接在宿主机上即可备份。目录权限由 `entrypoint.sh` 在启动时修正（root 改归属后降权运行），不需要手动 `chown`。

备份：直接复制 `./data` 目录即可，或者打个包：

```bash
tar czf emby-controller-backup.tar.gz -C ./data .
```

`secret.key` 丢了的话，已存的 API Key 就解不开了，需要重新录入服务器。所以备份时别漏掉它。

## 安全说明

Emby Apex 管理后台自身只有一个管理员账号，没有多管理员角色体系；这里的 Emby 用户 Policy 编辑不等于后台 RBAC。表单带 CSRF 校验，会话 Cookie 签名，封面图经服务端代理（不把 Emby 地址和 Key 暴露给浏览器）。**不建议把管理端口（8000）暴露到公网**，请在反向代理或防火墙层限制访问。

用户端 portal（8080）是设计上要对外的那一个：注册、登录、账户、兑换、改密和求片读写都限定在登录者自己的账号及所属服务器上，碰不到服务器配置和别人的管理数据。两个 app 的会话 Cookie 名不同（`ec_portal` / `ec_session`）。两边共用同一个签名密钥，所以 portal 的 Cookie 在管理端也是「签名有效」的，但里面只有 `portal_user_id` 这一个键，而管理端鉴权只认 `admin` 键 —— 拿用户登录态换不出管理权限。

不管对外暴露哪个端口，都套一层带 HTTPS 的反向代理，并把 `APEX_COOKIE` 设为 `true`。

## 技术栈

FastAPI + SQLAlchemy(async) + APScheduler，前端为 `frontend/` 下的 Vue 3 + Vite + TypeScript 单页应用。

两个 ASGI 应用共用下面这一套模型和服务层，只是挂的路由不同：`main.py` 是管理后台（含定时任务），`portal_main.py` 是用户端账户中心。

```
app/
  main.py         管理后台装配与生命周期（定时任务在这里起）
  portal_main.py  用户端 portal 装配（独立端口、独立 cookie，不起定时任务）
  config.py       环境变量
  models.py       数据表
  db.py           会话、建表与轻量迁移
  security.py     登录、会话、CSRF、API Key 加解密、用户端会话
  emby.py         Emby HTTP 客户端（含用户增删改与播放权限开关）
  services.py     业务逻辑（同步、Webhook 播放、启停、到期、自助注册、兑换码、未激活清理）
  scheduler.py    定时任务
  stats.py        历史聚合
  notify.py       通知（Webhook、Telegram、企业微信）
  logging_config.py 日志级别与凭据脱敏（httpx/httpcore 及业务记录）
  wecom.py        企业微信 access_token、应用消息和回调加解密
  wecom_commands.py 企业微信回调管理员命令
  tmdb.py         TMDB 服务端客户端与海报下载
  api/             `/api/v1` 管理端、portal 与企业微信接口
  shell.py         管理端和 portal 的静态 Vue 壳路由
  static/          logoicon.png 与本地 Vue 3.5.13 生产版
```

## 求片与海报

管理员在「设置」页填写 TMDB API Key（明文存于 `app_settings`，清空保存会停用 TMDB；不会发送到 portal），并可设置代理和历史保留天数。用户登录后进入「求片中心」，可按聚合、电影、电视剧或 TMDB ID 搜索，年份只影响排序优先级。提交范围绑定用户所属 Emby 服务器，同一用户的进行中请求不能重复；被拒绝后可以重新提交。

管理端按“服务器 + TMDB ID + 媒体类型”合并请求。确认入库先提交控制器状态，不触发 Emby 扫描，再由后台任务把 TMDB `w342` 海报保存到 `/image/posters`，因此不会被海报网络延迟阻塞；下载失败不会阻止确认，页面回退到远程海报或图标占位。搜索结果已包含详情字段，点击“查看详情”直接在页面展示；直接 TMDB ID 链接仍使用 60 秒详情缓存。

portal 的求片页面通过 `/api/v1/requests*` 搜索、查看详情和提交，写操作需要 CSRF。TMDB API Key、代理和求片历史保留天数在管理端「设置」页配置，保存后立即生效；Key 不会发送到 portal。管理端提供 TMDB 连接测试，错误会区分代理、网络、Key、限流和服务端故障。

注册和到期通知同样只在主数据提交成功后发送。到期提醒、停用/播放回收和自动删除分别使用持久化时间或状态字段去重，因此服务重启和重复调度不会重复发送；Webhook、Telegram、企业微信任一渠道失败只记录脱敏日志，不影响注册、兑换、停用或删除。

portal 账户页的“刷新”每 10 秒可用一次，会向 Emby 读取当前账号最新停用与播放状态并更新页面，失败时保留旧数据。管理端用户页提供全量手动同步；并发全量同步会合并，用户和 Policy 无变化时不重复落库。

用户的 `playback_source` 决定播放权限来源：自助注册和管理员新建账号为 `controller`，Emby 外部改动会在同步时被控制器校正；老账号认领或为已有账号开通 portal 后为 `emby`，portal 刷新会跟随 Emby 当前 `EnableMediaPlayback`。

## 播放历史写入策略

播放状态不再定时请求 Emby `/Sessions`，而由 Emby 向 `POST /webhook?token=embyapex` 推送开始、进度、暂停、恢复和停止事件。开始/进度/暂停/恢复仅更新内存播放会话，停止事件或管理进程正常退出时一次性写入 `playback_records`；重启不会恢复旧内存会话，数据库遗留的未结束记录会在启动时收尾。该取舍可能丢失进程崩溃时尚未结束的整段播放，但减少了网络和磁盘写入。SQLite 连接保持 WAL、`synchronous=FULL`、`busy_timeout` 和 `wal_autocheckpoint`，兑换码、设置、求片记录、操作日志和服务器配置仍在业务操作中立即提交。scheduler 已移除 `_poll_job` 和 `_stale_playback_job`；部署日志若仍出现旧任务名称，需重新构建并重建容器。
