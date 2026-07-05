# MineSentinel Minecraft 日志审计

这是一个 AstrBot 插件，用来直接读取 Minecraft 服务器运行日志并生成 AI 巡检总结。当前版本不再依赖 Java 端插件、WebSocket、聊天桥、远程命令或玩家绑定；核心输入只有 Minecraft `logs/latest.log`、历史 `.log` 和压缩 `.log.gz`。

## 功能

- 直接按路径读取单服或 Velocity 群组服日志。
- 启动时回扫最近窗口，避免 `latest.log` 轮转或压缩后 8 小时总结缺日志。
- 实时尾读 `latest.log`，轮转、截断或重启后会自动补读近期日志；单源异常带 supervisor 自动重启（指数退避，最多 10 次）。
- 过滤服务器报错死循环：同类 ERROR/WARN/Exception 只保留首条和周期性摘要。
- **Drain3 模板化 + 统计异常检测**：每条日志解析为 `(templateId, template, params)`，按 `(server_id, template_id)` 维护 EWMA 突增基线和分位数阈值，新模板/突增会标记 `new_template` / `anomaly_spike` 并提升 severity。
- **OpenTelemetry Logs Data Model**：每条 observation 的 `context.otel` 写入 `severityNumber` / `eventName` / `body` / `resource.service.name` / `attributes.template.id` 等字段，落盘 JSONL 保持嵌套 dict 结构，便于 Loki / OTel-compatible 系统按字段检索。
- AI 生成五段式巡检报告，并保留图片渲染和完整 JSONL 附件。
- 社区管理单独分类：ban、kick、mute、report、spam、grief、cheat、举报、封禁、禁言、刷屏等会进入 `community`，不混入普通插件报错。
- 注重性能和内存安全：追加读取限制字节数，末尾补读按块读取，回扫分批写入 JSONL，报告窗口有内存上限和优先级采样；突发日志走 backlog 排队（不静默丢弃），模板/异常状态有 per-server 上限和 TTL 清理。

## 高级日志分析链路

MineSentinel 的日志处理不是简单的"读日志 + 循环过滤 + AI 总结"，而是一条结构化分析管线：

```
raw log line
  │
  ▼ sanitize          去 ANSI / 控制字符；截断超长行
  │
  ▼ Drain3 template   按 server_id 分 namespace 解析为 (templateId, template, params)
  │                   新模板 → tags += new_template
  │
  ▼ anomaly score     对 (server_id, template_id) 维护 EWMA 基线 + 分位数阈值
  │                   突增 → tags += anomaly_spike，score 提升 severity
  │
  ▼ loop_filter       同 templateId 的 ERROR/WARN 合并为周期摘要（loop_suppressed）
  │
  ▼ rules             12 类分类 + critical/high/medium/low 严重级别 + 推荐动作
  │
  ▼ storage           normalize → compact（保持 OTel 嵌套 dict）→ JSONL 落盘
  │
  ▼ LLM report        五段式巡检 + 异常证据（预计算，不让 LLM 重新检测）
```

关键设计：

- **模板按 server_id 分 namespace**：不同后端服的插件组合、语言、日志模式差异大，全局共享 parse tree 会导致 A 服模板污染 B 服的 `new_template` 判定。每个 `server_id` 维护独立的 Drain3 tree，上限 16 个 namespace。
- **异常检测有界**：`max_templates_per_server`（默认 500）超过时淘汰最久未见的；`inactive_template_ttl_hours`（默认 24h）周期清理不活跃模板。snapshot 输出 `per_server_count` / `cleanup_count` / `last_cleanup_ms`。
- **突发日志 backlog**：单轮超过 `max_lines_per_poll` 时，前 N 行本轮处理，剩余完整行存入 backlog 留到下一轮；backlog 超 `max_lines*4` 才丢弃最旧行并写一条 synthetic observation 标注"本窗口审计日志不完整"。
- **OTel 字段不被字符串化**：`compact_value` 对嵌套 dict/list 递归 compact 而非 JSON dump 成字符串，`context.otel` 在落盘 JSONL 里保持可查询的嵌套对象。

## Rust 加速（可选）

`mine_sentinel_rs` 是用 PyO3 写的原生扩展，用于加速 CPU 热路径（`normalize_record` / `record_to_json` / `json_line` / `dedupe_key`）。**它是可选依赖**：未安装时 codec 自动降级为纯 Python 实现，行为完全等价，只是慢一些。插件不会因为缺少 wheel 而加载失败。

启用方式：从 GitHub Actions "Build Rust wheels" 工作流下载对应平台的 wheel，然后：

```bash
pip install mine_sentinel_rs-<version>-<platform>.whl
python -c "import mine_sentinel_rs; print('rust core enabled')"
```

不需要在目标机器本地编译 Rust。如果 `import mine_sentinel_rs` 失败，插件照常工作，只是日志里不会出现 "rust core enabled"。

## 命令

- `/mc help`：查看 MineSentinel 审计命令。
- `/mc monitor status`：查看日志源、存储目录、最近错误和报告状态。
- `/mc report now [服务器ID] [30m|8h]`：立即生成报告，例如 `/mc report now survival 8h`。

## 最小配置

```yaml
mine_sentinel:
  enabled: true
  runtime_log:
    enabled: true
    sources:
      - server_id: survival
        server_name: 生存服
        server_type: minecraft      # minecraft | velocity，默认 minecraft
        root: "D:\\minecraftserver"  # 服务器根目录，自动读取 root/logs/latest.log
      # Velocity 群组服示例（多服指定）：
      # - server_id: proxy
      #   server_name: 代理
      #   server_type: velocity
      #   logs_dir: "/opt/velocity/logs"   # 直接指定日志目录，优先级高于 root
      # - server_id: creative
      #   server_type: minecraft
      #   log_file: "/opt/creative/logs/latest.log"  # 直接指定文件，优先级最高
    backfill_on_start: true
    backfill_window_minutes: 480
    loop_filter_enabled: true
    poll_interval_seconds: 5         # 轮询间隔；AstrBot 仅从文件系统读取日志，不与 MC 进程通信，不影响 mspt/tps
    max_bytes_per_poll: 262144       # 单次轮询最大读取字节数
    max_lines_per_poll: 200          # 单次轮询最大写入行数
  storage:
    enabled: true
  report:
    enabled: true
    interval_hours: 8
    default_window_minutes: 480
    delivery_targets:
      - group:123456789
    send_as_image: true
    send_full_log_file: true
```

每个 source 支持三种日志路径指定方式，优先级从高到低：

1. `log_file`：直接指定 `latest.log` 路径
2. `logs_dir`：直接指定日志目录，自动读取 `<logs_dir>/latest.log`
3. `root`：服务器根目录，自动读取 `<root>/logs/latest.log`

也支持把 source 写成字符串（自动按上述规则切分）。`server_type` 用于报告分类：`minecraft`（Paper/Spigot/Purpur/Folia 等均归一为 minecraft）或 `velocity`（代理服）。

Velocity 群组服请把 Velocity 根目录和每个后端服分别添加为一个 source，`server_type` 分别设为 `velocity` 和 `minecraft`。

**性能说明**：MineSentinel 通过异步文件尾读 (`asyncio.to_thread`) 从 `logs/latest.log` 增量读取，单次读取有字节数和行数上限，不与 Minecraft 服务端进程通信，不会影响服务器的 mspt 和 tps。可以通过 `poll_interval_seconds`、`max_bytes_per_poll`、`max_lines_per_poll` 进一步调优。

**启动提示**：如果 `runtime_log.sources` 为空或全部无效，启动时会输出 WARN 日志提示用户去配置。

## 按小时总结模式（推荐，零 MC 性能影响）

如果你不需要实时告警，只想定期收到 AI 总结报告，可以启用 **hourly 模式**：完全不轮询 `latest.log`，改为每整点直接从 `logs/` 目录读取上一小时的日志（含 `.log.gz` 归档），调 AI 生成一份小时总结；累积 `hours_per_cycle` 份后（默认 8 小时）再调 AI 把多份小时总结整合成一份周期报告发送。这样 MC 服务端的 mspt/tps 完全不受影响。

```yaml
mine_sentinel:
  enabled: true
  runtime_log:
    enabled: true
    sources:
      - server_id: survival
        server_name: 生存服
        server_type: minecraft
        root: "/opt/paper"
  hourly_summary:
    enabled: true              # 启用后自动禁用轮询
    hours_per_cycle: 8         # 每 8 小时整合一次发送
    window_minutes: 60         # 单次小时总结窗口
    poll_enabled: false        # 是否同时启用实时轮询（默认关闭）
    provider_id: ""            # 留空复用 report.provider_id 或默认 provider
    max_records_per_hour: 5000
    max_log_lines_per_hour: 20000
    retention_cycles: 2        # 磁盘保留多少个历史周期
  report:
    enabled: false             # hourly 模式下建议关闭旧的定时报告
    delivery_targets:
      - group:123456789        # 周期报告投递目标
```

**Minecraft 日志保存流程**（来自 log4j2.xml 默认配置）：
- `logs/latest.log` 是当前会话实时日志
- 服务器重启或跨天时，`latest.log` 被压缩归档为 `logs/YYYY-MM-DD-N.log.gz`（N 是当天第几次归档）
- 日志行格式 `[HH:MM:SS] [Thread/LEVEL]: message`，时间戳只有时分秒，日期从文件名/mtime 推断
- 滚动策略：`TimeBasedTriggeringPolicy`（按天）+ `OnStartupTriggeringPolicy`（启动时）

hourly 模式会自动遍历 `logs/` 目录里的 `latest.log` 和所有 `.log.gz` 归档，按时间戳过滤出目标小时的日志行，无需用户介入。

**启动行为**：
- 启动时如果当前不是整点（比如 14:35 启动），会立即补读 14:00~14:35 这段已过部分的日志并生成总结
- 之后每个整点（15:00、16:00...）读取上一完整小时的日志
- 累积到 `hours_per_cycle` 份小时总结后，调用 AI 整合为周期报告并发送到 `report.delivery_targets`
- 没有配置 LLM provider 时自动降级为规则启发式总结（不调用 AI）

**未配置日志源时**启动会输出 WARN：`hourly 模式已启用但未配置任何日志源，请在 mine_sentinel.runtime_log.sources 中至少添加一个服务器。`

每个 source 可以单独写 `delivery_targets` 或 `target_sessions`，用于把特定服务器报告发到指定 AstrBot 会话；全局 `mine_sentinel.report.delivery_targets` 用于总报告投递。目标建议优先使用 `/sid` 输出的完整 UMO，也支持 `group:`、`qq:` 简写。

## 报告与附件

报告正文默认渲染为 PNG；如果图片组件或字体加载失败，会自动回退为文本。完整窗口记录会导出到 `mine_sentinel/exports/*.jsonl` 并尝试作为群文件附件发送，方便管理员复核原始日志。

分类采用 Minecraft 服务器运营诊断体系，共 12 类，按固定优先级匹配（高优先级先命中）：

```
community > chat_review > player_feedback > community_ops
         > complaint > network > plugin > cross_server
         > moderation > bug > economy > daily
```

| 分类 | 含义 |
|------|------|
| `community` | 封禁、踢出、禁言、作弊、外挂、反作弊（anticheat/VL/xray/kill aura） |
| `chat_review` | 聊天审查：辱骂、广告、骚扰、刷屏、威胁、隐私泄露（discord.gg/开盒/人肉） |
| `player_feedback` | 玩家建议、功能请求（建议/希望/能不能/加个/优化/改进） |
| `community_ops` | 社区运营：活动、公告、奖励、投票、赛季、比赛 |
| `complaint` | 性能投诉：Can't keep up、Overloaded、TPS、MSPT、卡顿、延迟 |
| `network` | 网络/连接：connection reset、broken pipe、io.netty、socket、断连 |
| `plugin` | 插件：could not load/enable、dependency、softdepend、加载失败 |
| `cross_server` | 跨服/代理：Velocity、BungeeCord、proxy、forwarding、server switch |
| `moderation` | 权限/登录：whitelist、permission、auth、login、UUID、正版验证 |
| `bug` | 服务端异常：error、exception、failed、crash、NPE、报错、崩溃 |
| `economy` | 经济：Vault、shop、money、balance、trade、拍卖、金币 |
| `daily` | 日常：started/stopped/done/join/quit/connected/disconnected |
| `suggestion` | AI 补充建议分类，默认无关键词，由 LLM 按需写入 |

**严重级别**：

- `critical`：fatal/severe/crash、OutOfMemoryError、watchdog、server stopped、tick took too long、代理/后端大面积不可用 —— 直接告警，不受 `min_evidence_count` 限制。
- `high`：循环刷屏（loop_suppressed）、ERROR≥2、PERFORMANCE≥3、NETWORK≥5、插件加载失败、多服务器/多后端受影响、chat_review 命中威胁/隐私、community_ops 活动事故。
- `medium`：单条 ERROR、WARN≥2、单次性能警告、网络异常、权限/登录异常、单次聊天违规、3 条以上同类玩家建议。
- `low`：单条 WARN、普通 daily、单个玩家 join/quit、普通玩家建议、普通活动公告 —— 不告警。

**告警策略**：critical 直告；high 默认 `evidence_count >= 3` 告警；medium 仅在影响多服务器/多后端或证据数较多时告警；low 不告警。`chat_review` 默认不告警，除非 severity≥high、evidence_count≥5 或命中威胁/隐私敏感词；`player_feedback` 通常不告警，仅进入运营待办；`community_ops` 仅 high/critical 才告警。

**推荐动作**按分类细化：plugin 查依赖和版本、network 查代理连通性和转发配置、community 交社区管理流程复核、chat_review 优先处理威胁/隐私、critical 查 latest.log 和崩溃报告并评估回滚。详细规则见 [services/mine_sentinel/reporting/rules.py](services/mine_sentinel/reporting/rules.py)。

## 部署提示词

把下面这段给有本机文件读写权限的 AI 助手即可：

```text
你是 Minecraft + AstrBot 部署助手。请帮我部署 MineSentinel 日志审计插件，不要跳过备份和验证。

GitHub 仓库：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter
- 源码：仓库 master 分支，`git clone` 或下载 zip 均可。
- Rust 加速 wheel：仓库的 GitHub Actions → "Build Rust wheels" 工作流，从最近一次成功 run 的 Artifacts 下载对应平台 wheel。
  直接链接：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter/actions/workflows/rust-wheels.yml
- 不要在目标机器本地编译 Rust；只从上述 Actions 下载预编译 wheel。

开始前先向我索取：
1. 部署模式：单服 / Velocity 群组服。
2. Minecraft 服务器根目录；Velocity 群组服需要 Velocity 根目录和每个后端服根目录。
3. AstrBot 根目录和实际运行 Python 路径。
4. 接收报告的 AstrBot 会话 UMO，优先使用 /sid 输出；也可提供 group: 或 qq: 简写。
5. 服务器日志量量级：小服（<50 玩家日常）/ 大服（≥50 玩家日常或 mod 服、连锁机器多）。
6. 是否现在重启 AstrBot 和 Minecraft 服务端。

执行要求：
1. 检查目录存在，识别 AstrBot 插件目录、MineSentinel 数据目录和现有配置。
2. 从 GitHub 仓库安装 AstrBot 插件源码到插件目录；覆盖前把旧目录和配置备份到带时间戳的 backup 目录。
3. `pip install -r requirements.txt` 安装 Python 依赖（drain3 等可选依赖会启用模板化/异常检测，缺失时自动降级）。
4. **可选**：如需 Rust 加速，从 GitHub Actions "Build Rust wheels" 工作流（见上方链接）最近一次成功 run 的 Artifacts 下载对应平台 wheel 并 `pip install <wheel>.whl`；未安装时插件照常运行（纯 Python 降级）。不要在目标机器本地编译 Rust。
5. 在 mine_sentinel.runtime_log.sources 写入服务器根目录或 latest.log 路径；Velocity 群组服写入 Velocity 和所有后端服。
6. 开启 runtime_log、backfill_on_start、loop_filter_enabled、storage、report、send_as_image、send_full_log_file。
7. 报告目标写入 mine_sentinel.report.delivery_targets，优先使用 /sid 完整 UMO。
8. 按日志量量级选择性能档位（见下方"性能档位参考"），写入 runtime_log.template_parse_mode、runtime_log.anomaly_track_info、runtime_log.io_workers、report.export_format、report.export_reuse_existing。
9. 重启后执行 /mc monitor status，确认日志源数量、observation/export 目录、io_workers 是否生效。
10. 触发或等待一条 MC 日志后执行 /mc report now <服务器ID> 30m，验证图片报告和 JSONL 附件能发送；同时检查附件文件名含 _t<秒级时间戳> 后缀（手动连续 report now 不会复用旧附件）。
11. 最后汇总安装文件、备份位置、日志源 server_id、性能档位、是否启用 Rust 加速、验证结果和需要我手动确认的事项。

性能档位参考（写入 mine_sentinel 配置）：
- 小服默认档（开箱即用，功能完整）：
  runtime_log.template_parse_mode: all        # 全量 Drain3 模板解析
  runtime_log.anomaly_track_info: true        # 普通 INFO 也进入异常检测
  runtime_log.io_workers: 0                   # 沿用 asyncio 默认线程池
  report.export_format: jsonl                 # 明文 JSONL 附件
  report.export_reuse_existing: true          # 周期报告重试时复用同路径
- 大服性能优先档（≥50 玩家日常 / mod 服 / 高日志量）：
  runtime_log.template_parse_mode: interesting  # 只解析 WARN+ 和命中关键词的 INFO（如 crash/timeout/rollback/out of memory/lag/异常报错等）
  runtime_log.anomaly_track_info: false         # 普通 INFO 不进入异常检测（仅 fingerprint + 简化 observation）
  runtime_log.io_workers: 2                     # 专用有界 ThreadPoolExecutor，避免和 AstrBot 其他任务抢默认池
  report.export_format: jsonl.gz                # gzip 压缩附件，省磁盘和传输带宽
  report.export_reuse_existing: true            # 周期报告重试时复用同路径（手动 report now 因秒级时间戳不会误复用）

注意事项：
- observation 按天分片（YYYYMMDD.jsonl），清理粒度为天，当天文件会保留到跨天后才删；retention_minutes 仅决定哪一天的文件可删。若需小时级保留请告知我，后续可改 hourly shard。
- 若机器上已有旧版 .idx 偏移索引文件（无 #trust_legacy header），storage.trust_legacy_index 默认 true（按单调处理，保持向后兼容）；若怀疑旧 .idx 乱序导致漏日志，可设为 false 进入保守模式（全量扫描，性能下降但不漏）。
- 不要在目标机器本地编译 Rust；只从 GitHub Actions 下载预编译 wheel。
```

## 许可证

本插件沿用项目原有 AGPL-3.0 许可。图片报告默认使用 LXGW WenKai GB 字体缓存，字体项目采用 SIL Open Font License 1.1。
