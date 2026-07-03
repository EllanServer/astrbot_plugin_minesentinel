# AstrBot Minecraft 适配器

连接 Minecraft 服务器和 AstrBot，实现消息互通和服务器管理功能。

> [!important]
> 本仓库是 [railgun19457/astrbot_plugin_minecraft_adapter](https://github.com/railgun19457/astrbot_plugin_minecraft_adapter) 的 EllanServer 下游维护版，用于保留本服适配、MineSentinel 图片巡检报告和 QQ 目标发送等改动。同步上游时请优先对比上游 `master` 分支。

![:name](https://count.getloli.com/@astrbot_plugin_minecraft_adapter?name=astrbot_plugin_minecraft_adapter&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> [!note]
> 孪生项目:[AstrBot Adapter](https://github.com/railgun19457/AstrBotAdapter)
> Minecraft插件，提供`WebSocket Server`和`REST API`，支持bukkit/paper/folia/velocity等常见插件服

## 主要功能
- 群服互通，支持将mc服务器与其他连接到AstrBot的平台互通消息
- 服务器管理，支持服务器状态查询，远程指令执行等
- AI聊天，支持游戏内和AstrBot聊天
- MineSentinel 观察分析，接收 MC 端只读观察批次，使用 AstrBot 模型生成报告和告警

## 命令
- `mc help` - 显示帮助信息和自定义指令列表
- `mc status` - 查看服务器状态
- `mc list` - 查看在线玩家列表
- `mc player <玩家ID>` - 查看玩家详细信息
- `mc cmd <指令>` - 远程执行服务器指令
- `mc bind <游戏ID>` - 绑定你的游戏ID
- `mc unbind` - 解除绑定
- `mc monitor status` - 查看 MineSentinel 观察缓存、去重和最近报告状态
- `mc report now [服务器ID] [时间窗口]` - 立即生成观察报告，例如 `mc report now survival 8h`

当前会话关联多个服务器时，需要区分服务器的指令会显示服务器列表，发送编号选择目标服务器

## 配置说明

> 支持添加多服务器，添加的每个服务器将作为独立的平台适配器处理
> 以下说明仅包含部分重要配置
 
### 服务器连接信息
  - 服务器ID - 用于区分不同服务器
  - 服务器地址、端口、认证token - 用于连接指定mc服务器
### 消息转发配置
  - 聊天消息格式为，消息发送到其他平台时（如QQ），使用的格式，支持占位符`{player}`和`{message}`，代表发送者游戏ID和消息内容
  - **目标会话** 消息转发和消息监听的目标会话，使用会话umo区分，可通过sid指令获取，**这个配置标志着MC服务器和群聊的互相绑定关系**，群聊中获取mc服务器信息和执行指令等，需要服务器和会话绑定
  - 转发前缀 - 带有该前缀的消息将转发到绑定的服务器中
  - 转发提醒方式 - 支持 文本提醒/贴表情/不提醒
### 指令相关配置
  - 指令 黑/白名单 -用于保障服务器安全，防止恶意指令影响服务器
  - 绑定功能，用户绑定后，提供占位符`{sender}`用于自定义指令
  - 自定义指令
    - 使用`<<>>`分隔，左侧为自定义指令，右侧为实际在服务器中执行的指令
    - 支持`{sender}`占位符，会自动替换为发送指令的用户绑定的游戏ID
    - 支持自定义参数占位符，格式为`<&xxx&>`,左右一致即可自动替换，参数之间使用空格分隔
    - 例 `tp <&X&> <&y&> <&z&><<>>tp {sender} <&X&> <&y&> <&z&>`
      - 其中`<&X&>` `<&y&>` `<&z&>`为三个自定义参数占位符，在`<<>>`的左右都要有
      - `{sender}`会在执行时替换为发送者的游戏ID
      - 假设用户A绑定了游戏ID `Misaka`，并在群聊中发送`tp 114 514 1919`,实际执行的指令为`tp Misaka 114 514 1919`
      - 自定义参数将用户输入的坐标参数传递到了实际指令中，{sender}参数则提供了tp的游戏ID

### MineSentinel 观察配置
  - `mine_sentinel.enabled` 控制是否接收和分析 MC 端 `OBSERVATION_BATCH`
  - `mine_sentinel.report.enabled` 默认开启，定时生成 AI 总结并发送到服务器配置中的 `target_sessions`
  - `mine_sentinel.report.interval_hours` 默认 `8`，可调整为其他小时数；旧配置中的 `interval_minutes` 仍兼容
  - `mine_sentinel.report.default_window_minutes` 默认 `480`，控制每次总结覆盖的观察窗口
  - `mine_sentinel.retention_minutes` 默认 `480`，建议不小于 `default_window_minutes`
  - `mine_sentinel.storage.enabled` 默认开启，完整 observation 会以 JSONL 落盘到插件数据目录下的 `mine_sentinel/observations`
  - `mine_sentinel.storage.cleanup_interval_seconds` 默认 `300`，写入仍实时落盘，但过期 JSONL 文件清理会节流，避免高频 batch 下反复扫描目录
  - MC 端 `schemaVersion=2` 的 `context` 字段会保留到 JSONL，用于按来源、消息类型、后端服、世界/维度重建同一问题的前因后果
  - `mine_sentinel.storage.dedupe_memory_limit` 默认 `100000`，报告读取/导出时去重 key 超过该数量会溢写临时 SQLite 文件，避免极端窗口下去重集合撑爆内存
  - 8 小时报告从硬盘读取窗口记录；插件不再保留 observation 内存缓存，避免聊天量大时撑爆内存
  - `mine_sentinel.dialogue.enabled` 默认开启，会专门从玩家聊天中识别卡顿、掉线、回档、丢物品、经济异常、外挂举报、跨服异常和体验建议等问题信号
  - `mine_sentinel.dialogue.min_issue_score`、`min_evidence_count`、`max_findings` 可调节对话问题发现的敏感度和报告展示数量
  - `mine_sentinel.dialogue.incident_gap_seconds` 默认 `1800`，同类玩家问题超过该间隔没有新反馈时会拆成新的 incident，避免 8 小时内不同时间段的问题被揉成一条
  - `mine_sentinel.dialogue.continuation_window_seconds` 默认 `90`，用于在同服/同后端短窗口内把“我也是”“一样”“+1”等跟进反馈归入最近明确问题
  - `mine_sentinel.dialogue.context_window_seconds` 默认 `120`、`context_messages_per_side` 默认 `2`，报告证据会带同服/同后端前后文聊天片段，避免 AI 和管理员只看孤立单句
  - `mine_sentinel.dialogue.custom_rules` 可追加服务器专属问题识别规则，例如 RPG 任务 NPC、领地、商店、抽奖、礼包、拍卖行等玩法黑话；规则会被清洗为安全的 category/severity/tag 后再参与报告，自定义 tag 会自动加 `custom_` 前缀并去重，避免和内置规则混组
  - 每次报告会在 `mine_sentinel/exports` 导出本次完整窗口 JSONL 文件，并在报告备注中给出文件路径
  - `mine_sentinel.report.send_as_image` 默认开启，报告正文会优先渲染为 PNG 图片发送，排版中会把事件卡片、风险提醒、建议处理和底部“引用上下文”分区展示；图片渲染失败时会自动回退为纯文本
  - 图片报告默认使用 [LXGW WenKai GB](https://github.com/lxgw/LxgwWenkaiGB) 的 `LXGWWenKaiGB-Regular.ttf`，首次渲染时会自动下载并缓存到插件数据目录的 `mine_sentinel/render_cache/fonts`
  - 若运行环境无法访问 GitHub/CDN，可手动放置中文字体到 AstrBot 数据目录的 `font.ttf`，或预先把 `LXGWWenKaiGB-Regular.ttf` 放到上述字体缓存目录
  - `mine_sentinel.report.send_full_log_file` 默认开启，会尝试把本次完整窗口 JSONL 作为群文件/附件发送；若平台不支持文件组件，文字报告仍会正常发送
  - `mine_sentinel.storage.include_raw` 默认关闭，报告不依赖 raw 字段；开启会增加磁盘占用和隐私风险
  - 8 小时报告的本地规则统计会尽量使用完整窗口记录，并在报告中列出具体玩家名
  - `mine_sentinel.report.max_records_in_memory` 默认 `50000`，用于限制单次报告放进内存分析的 observation 数；极端聊天量触发上限时会优先保留可疑玩家对话并在运维备注中说明，完整 JSONL 仍然落盘并可导出/上传
  - `mine_sentinel.report.max_ai_records` 默认 `120`，`max_ai_prompt_chars` 默认 `30000`，仅限制提交给 AI 的润色输入，不影响本地完整记录落盘
  - `mine_sentinel.report.provider_id` 为空时使用当前/目标会话正在使用的 AstrBot provider；填写 provider ID 时使用指定模型
  - `mine_sentinel.report.send_to_target_sessions` 开启后，手动报告、定时报告和告警会发送到服务器配置中的目标会话
  - `mine_sentinel.report.delivery_targets` 可独立指定 NapCat/AstrBot 发送目标，支持完整 UMO（如 `aiocqhttp:GroupMessage:123456789`）或简写 `group:QQ群号`、`qq:QQ号`、`private:QQ号`；纯数字默认按 QQ 群处理
  - `mine_sentinel.alert.enabled` / `min_severity` / `cooldown_seconds` 控制告警发送；即时告警默认只看最近 `30` 分钟，并且同服务器最短 `60` 秒分析一次

MineSentinel 不会向 MC 端下发处罚、RCON、远程指令或配置修改；它只在 AstrBot 侧聚合、去重、总结和通知。

## AI 总结部署

AI 总结链路由两部分组成：MC 端 Java 插件负责只读采集聊天、玩家事件和服务器指标；本 AstrBot 插件负责保存 JSONL、聚合事件、调用 AstrBot 模型生成总结，并把图片报告/附件发送到 QQ。

### 前置条件
- AstrBot 已配置可用的模型 Provider；`mine_sentinel.report.provider_id` 留空时使用当前或目标会话模型，填写时使用指定 Provider。
- MC 端 Java 插件已开启 `mine_sentinel.enabled`、`observation.enabled`、`chat.enabled` 和 `metrics.enabled`。
- 目标 QQ 群已拿到 UMO，例如 `aiocqhttp:GroupMessage:123456789`；也可以在 `delivery_targets` 中使用简写 `group:123456789`。

### 单服实现
1. MC 后端服安装 `AstrbotAdaptor-版本-Backend.jar`，Java 端保持 `proxyMode.enabled: false`。
2. 启动后从 Java 插件配置或 `/astrbot token show` 获取 token。
3. 在本插件配置的 `mc_servers` 中添加该服务器，填写 `server.host`、`server.port`、`server.token`；建议开启 `auto_server_id`，也可手动设置稳定的 `server.server_id`。
4. 在该服务器的 `message.target_sessions` 中填入要接收报告的 QQ 群 UMO。`mine_sentinel.report.send_to_target_sessions: true` 时，定时/手动报告会发到这些群。
5. 确认 `mine_sentinel.enabled: true`、`mine_sentinel.storage.enabled: true`、`mine_sentinel.report.enabled: true`。
6. 用 `mc monitor status` 查看是否收到 observation；用 `mc report now <服务器ID> 8h` 立即生成一次报告。

单服最小配置思路：

```yaml
mc_servers:
  - enabled: true
    server:
      server_id: survival
      host: 127.0.0.1
      port: 8765
      token: "从 Java 插件获取"
    auto_server_id: true
    message:
      target_sessions:
        - aiocqhttp:GroupMessage:123456789

mine_sentinel:
  enabled: true
  storage:
    enabled: true
  report:
    enabled: true
    interval_hours: 8
    default_window_minutes: 480
    send_to_target_sessions: true
    send_as_image: true
    send_full_log_file: true
```

### Velocity 群组服实现
1. Velocity 代理端安装 Java 插件 Velocity jar；所有需要纳入总结的后端服安装 Backend jar。
2. Java 后端服开启 `proxyMode.enabled: true` 并填写 Velocity 端 secret。后端服的 MineSentinel observation 会通过代理端统一转发到本 AstrBot 插件。
3. 每个后端服建议配置稳定的 `mine_sentinel.server_id` / `server_name`，例如 `lobby`、`survival`、`resource`。报告会使用这些字段区分问题来源。
4. 本插件 `mc_servers` 中通常只添加 Velocity 端连接，填写 Velocity 的 `server.host`、`server.port`、`server.token`。
5. 在 Velocity 服务器配置的 `message.target_sessions` 中填入接收总报告的 QQ 群；如果希望报告额外发送到其他群或私聊，使用 `mine_sentinel.report.delivery_targets`。
6. 用 `mc monitor status` 确认 observation 持续进入；用 `mc report now <服务器ID> 8h` 测试群组服报告。

群组服最小配置思路：

```yaml
mc_servers:
  - enabled: true
    server:
      server_id: velocity
      host: 127.0.0.1
      port: 8765
      token: "从 Velocity 端 Java 插件获取"
    auto_server_id: true
    message:
      target_sessions:
        - aiocqhttp:GroupMessage:123456789

mine_sentinel:
  enabled: true
  storage:
    enabled: true
  report:
    enabled: true
    interval_hours: 8
    default_window_minutes: 480
    send_to_target_sessions: true
    delivery_targets:
      - group:987654321
```

### 报告发送与排查
- `send_as_image` 默认开启，报告会优先以 PNG 图片发送；渲染失败会回退为文本。
- `send_full_log_file` 默认开启，会尝试附带完整窗口 JSONL，便于管理员复核原始上下文。
- 如果 QQ 没收到报告，先检查服务器 `message.target_sessions` 或 `mine_sentinel.report.delivery_targets`，再看 `mc monitor status` 是否有 observation。
- 如果报告为空，确认 Java 端 `mine_sentinel.chat.enabled` 和 `metrics.enabled` 已开启，并且 `retention_minutes` 不小于 `default_window_minutes`。

### AI 一键部署 Prompt

把下面这段发给具备本机文件读写和联网能力的 AI 助手，让它代为下载、安装和配置：

```text
你是 Minecraft + AstrBot 部署助手。请按下面要求帮我部署 MineSentinel AI 总结，不要跳过备份和验证。

项目链接：
- MC Java 插件仓库：https://github.com/EllanServer/AstrBotAdapter
- MC Java 插件 Actions：https://github.com/EllanServer/AstrBotAdapter/actions
- AstrBot 插件仓库：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter
- AstrBot 插件 Actions：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter/actions

开始前先向我索取这些信息，不要自行猜路径：
1. 部署模式：单服 / Velocity 群组服。
2. Minecraft 服务器根目录。单服给一个根目录；Velocity 群组服请给 Velocity 根目录和每个后端服根目录。
3. AstrBot 根目录。
4. 要接收 AI 总结的 QQ 群或 UMO，例如 group:123456789 或 aiocqhttp:GroupMessage:123456789。
5. 是否现在重启 MC 服务端和 AstrBot。

拿到路径后执行：
1. 检查所有目录是否存在，识别 plugins 目录、AstrBot 插件目录和现有配置文件。
2. 从 GitHub Actions 下载两个仓库最新 successful workflow 的构建产物和源码。优先使用 gh：
   - gh run list -R EllanServer/AstrBotAdapter --branch main --status success --limit 1
   - gh run download -R EllanServer/AstrBotAdapter <run-id> --dir ./downloads/AstrBotAdapter
   - gh run list -R EllanServer/astrbot_plugin_minecraft_adapter --branch master --status success --limit 1
   - gh run download -R EllanServer/astrbot_plugin_minecraft_adapter <run-id> --dir ./downloads/astrbot_plugin_minecraft_adapter
   如果没有 gh，就打开上面的 Actions 链接下载最新成功运行的 artifacts。若 Actions 没有源码 artifact，则同时下载源码 ZIP：
   - https://github.com/EllanServer/AstrBotAdapter/archive/refs/heads/main.zip
   - https://github.com/EllanServer/astrbot_plugin_minecraft_adapter/archive/refs/heads/master.zip
3. 安装 Java 插件：
   - 单服：把最新 Backend jar 放进该服务器根目录的 plugins/。
   - Velocity 群组服：把 Velocity jar 放进 Velocity 的 plugins/，把 Backend jar 放进每个后端服的 plugins/；如果产物包含 libs/，按 README 保持 libs/ 与 Velocity jar 同级。
4. 安装 AstrBot 插件：把 astrbot_plugin_minecraft_adapter 最新源码放进 AstrBot 的插件目录，目录名保持 astrbot_plugin_minecraft_adapter；如已有旧目录，先备份再覆盖。
5. 配置 Java 插件：
   - 单服保持 proxyMode.enabled=false。
   - Velocity 群组服先启动/读取 Velocity 端 secret，再给每个后端服写入 proxyMode.enabled=true 和 proxyMode.secret。
   - 确认 mine_sentinel.enabled、observation.enabled、chat.enabled、metrics.enabled 为 true。
6. 配置 AstrBot 插件：
   - 单服在 mc_servers 中添加后端服 host/port/token。
   - 群组服只添加 Velocity 端 host/port/token。
   - 把 QQ 群写入 message.target_sessions，必要时写入 mine_sentinel.report.delivery_targets。
   - 开启 mine_sentinel.enabled、storage.enabled、report.enabled、send_as_image、send_full_log_file。
7. 做备份：覆盖任何 jar、插件目录或配置文件前，先复制到带时间戳的 backup 目录。
8. 验证：
   - 启动或重启服务后检查 Java 插件和 AstrBot 日志。
   - 在 QQ/AstrBot 会话执行 mc monitor status，确认收到 observation。
   - 执行 mc report now <服务器ID> 30m 测试图片报告和 JSONL 附件发送。
9. 最后给我汇总：安装了哪些文件、备份位置、配置了哪些服务器 ID、测试命令结果、还需要我手动确认的事项。
```
  
## 更新日志
### v2.0.2 (2026-2-23)
- 将官方t2i改为本地pillow渲染
- 更新渲染模板
- 优化多服务器查询和渲染逻辑

### v2.0.1 (2026-2-15)
- 重构后首个正式版，对应mc端版本 v2.0.5
- 适配ws/http端口共用
- 添加服务器信息图片渲染功能
- 添加转发消息提醒模式选项
- 添加指令黑白名单功能
- 添加自定义指令，支持自定义占位符
- 添加用户绑定功能，用于自定义指令
- 适配多服务器配置
- 优化伪消息平台
### v2.0.0-beta (2026-2-2)
- 完全重写，适配全新2.0版本mc服务端插件

### v1.3.0 (2025-11-12)
- 添加AI对话功能
### v1.2.0 (2025-11-12)
- 进行两次重构
- 修复了数个bug
- 添加快捷转发功能

### v1.0.0 (2025-11-09)
- 首次发布
- 支持 WebSocket 实时通信
- 支持 REST API 查询
- 实现基本的消息转发和指令执行功能
- 支持自动重连

## 许可证

本子项目使用 GNU Affero General Public License v3.0，详见 [LICENSE](LICENSE)。

当前仓库是多许可证工作区：根目录文件使用 Apache License 2.0，`AstrBotAdapter/` 使用 MIT License，`astrbot_plugin_minecraft_adapter/` 使用 GNU AGPL v3.0。完整说明见根目录 [README.md](../README.md) 和 [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md)。

MineSentinel 图片报告运行时可自动缓存的 LXGW WenKai GB 字体由 [lxgw/LxgwWenkaiGB](https://github.com/lxgw/LxgwWenkaiGB) 提供，字体项目采用 SIL Open Font License 1.1。
