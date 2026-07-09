# MineSentinel 监控管理报告 AI

MineSentinel 是一个面向 AstrBot 的 Minecraft 运行日志监控与管理报告插件。它只读读取服务器 `latest.log`、历史 `.log` 和 `.log.gz`，通过规则归因、异常检测、训练数据式清洗和 AstrBot 模型能力生成可直接发给管理组的五段式报告。

当前项目已经从旧的 Minecraft Adapter 转为纯监控报告 AI：不再包含 Java 端插件、WebSocket、聊天桥、远程命令、玩家绑定或跨端控制链路。仓库名 `astrbot_plugin_minecraft_adapter` 仅为了兼容原 AstrBot 插件仓库与历史安装路径。

## 核心能力

- 直接读取单服、Velocity 群组服和多后端服日志；每个 source 可配置独立 `server_id`、`server_name`、`server_type` 和投递目标。
- 启动时回扫最近窗口，实时尾读 `latest.log`，支持日志轮转、截断、重启补读和 `.log.gz` 归档读取。
- 将日志解析为 observation，写入 JSONL，并保留 OpenTelemetry Logs Data Model 风格字段，便于后续接 Loki / OTel-compatible 系统。
- 对重复 ERROR/WARN/Exception 做循环过滤，突发 backlog 不静默丢弃，避免报错风暴把 AI prompt 和存储打爆。
- 可选 Drain3 模板化与 EWMA/分位数异常检测，识别 `new_template`、`anomaly_spike`、突发 TPS/MSPT/GC/网络/插件异常。
- 先做确定性分类和事故聚合，再把压缩后的证据交给 AI；AI 负责表达和归纳，不承担第一层检测。
- 输出五段式报告、图片正文、文本兜底和完整窗口 JSONL/JSONL.GZ 附件。

## 分析链路

```text
raw log line
  -> sanitize              去 ANSI / 控制字符 / 超长行裁剪
  -> runtime hints          快速抽取时间、等级、线程、插件、聊天、Vulcan、Hikari、ops hint
  -> template/anomaly       Drain3 模板、EWMA 基线、新模板和突增标记
  -> loop filter            合并同类死循环报错
  -> rule classifier        确定性分类、严重级别、推荐动作、事故聚合
  -> LLM clean              URL/邮箱/UUID/IP/token 脱敏，质量评分，clean hash 去重
  -> prompt sampling        按重要度、异常、结构化上下文抽样，过滤低价值日常指标
  -> report sections        五段式监控管理报告
  -> delivery/export        AstrBot 会话投递、图片渲染、JSONL 附件
```

## 五段式报告

报告固定使用以下 section id，方便前端、图片渲染和后续自动化消费：

- `overall`：总体状态、窗口范围、服务器健康概览。
- `incidents`：明确异常事件，例如插件报错、网络超时、数据库连接、经济/商店问题。
- `community`：社区管理和聊天秩序，例如举报、刷屏、封禁、禁言、反作弊告警。
- `player_problems`：玩家问题/投诉识别，例如卡顿反馈、进服失败、功能不可用。
- `risk_actions`：风险、处置建议和下一步动作。

文本与图片报告共用 5 分钟事故窗口：同一作用域内连续证据会合并，超过 5 分钟无新证据则拆成新事件；安静窗口作为补充说明，不占用事件编号。插件更新检查、兼容性、本地化和轻微调度延迟会在“聊天与社区观察”中汇总展示，但不会抬高重点事件数量。

## 智能分类

内置分类包括 `daily`、`complaint`、`bug`、`network`、`plugin`、`economy`、`community`、`chat_review`、`player_feedback`、`community_ops`、`moderation`、`cross_server`、`suggestion`。

分类优先使用结构化 runtime hints 和 ops hints，再回退到关键词、上下文、日志等级、线程、插件名和事故聚合。真实样本 `tests/fixtures/mclogs_pbfhCaI.log` 来自 [mclo.gs/pbfhCaI](https://mclo.gs/pbfhCaI)，用于验证 QuickShop/经济异常、数据库异常、插件异常、网络异常、离线模式/认证绕过风险会进入正确分类；Malformed JSON、JSON/NBT 转换失败会归入插件配置/数据解析异常，MythicMobs 内容定义、插件依赖、外部 API 凭据、外部资源获取和不安全运行模式会给出独立运维子类型，而不是泛化 Java bug。同时 Hikari 生命周期日志、AstrbotAdapter/CMI 正常代理握手不会误报为管理事件；Java 纯堆栈帧和装饰横幅只作为上下文，插件更新检查、兼容性/弃用、本地化资源键缺失、单个插件任务调度延迟等低风险 WARN 会进入观察分类，不升级为管理事件。

可用配置控制分类入口：

```yaml
mine_sentinel:
  runtime_log:
    category_enabled:
      chat_review: true
      player_feedback: true
      cross_server: false
    category_whitelist: []
```

`category_whitelist` 非空时只保留白名单分类；`category_enabled` 可按分类关闭检查项。`daily` 是兜底分类，始终保留。

## Rust 加速

`mine_sentinel_rs` 是可选 PyO3 扩展，不安装也能用纯 Python 路径完整运行。安装后会加速热路径：

- JSONL codec：`normalize_record`、`record_to_json`、`json_line`、`dedupe_key`。
- runtime hints：日志等级、时间、线程、插件、聊天、Vulcan、Hikari、ops hint 批处理。
- observation priority：高日志量窗口下的优先级抽样。
- AI sampling features：prompt 入模前的清洗 key、质量评分、低价值指标过滤。

Rust 与纯 Python 回退共享同一份清洗语义：每行只做一次 ANSI/控制字符 transport pass，再派生脱敏文本、指纹和质量标记；默认 daily-noise 规则合并为单次正则扫描。即使目标平台暂时没有 wheel，高日志量摄取也不会重复清洗同一行。

推荐从 GitHub Actions 的 `Build Rust wheels` 下载对应平台 wheel：

```bash
pip install mine_sentinel_rs-<version>-<platform>.whl
python -c "import mine_sentinel_rs; print('rust core enabled')"
```

本地开发可运行：

```bash
cargo fmt --manifest-path rust/Cargo.toml --check
cargo check --manifest-path rust/Cargo.toml
```

Windows 本地 `cargo check` 需要 MSVC `link.exe`。目标机器不需要安装 Rust；缺少 wheel 时插件自动降级，不会影响 AstrBot 加载。

## 安装

将仓库放入 AstrBot 插件目录，保持目录名为 `astrbot_plugin_minecraft_adapter`：

```bash
git clone https://github.com/EllanServer/astrbot_plugin_minecraft_adapter.git
pip install -r astrbot_plugin_minecraft_adapter/requirements.txt
```

在 AstrBot 插件管理中启用后，插件会注册 `/ms` 命令组，并在数据目录下使用 `plugin_data/mine_sentinel`。首次启动若发现旧路径 `plugin_data/astrbot_plugin_minecraft_adapter/mine_sentinel`，会自动迁移 MineSentinel 历史数据。

## 部署/升级提示词

把下面的提示词交给有本机文件读写权限的 AI 助手即可。第一段用于全新部署，第二段用于已安装旧版插件时升级。

### 全新部署

```text
你是 Minecraft + AstrBot 部署助手。请帮我部署 MineSentinel 监控管理报告 AI 插件，不要跳过备份、配置确认和验证。

GitHub 仓库：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter
- 源码：仓库 main 分支，插件目录名必须保持为 astrbot_plugin_minecraft_adapter。
- Rust 加速 wheel：GitHub Actions -> "Build Rust wheels" 工作流，下载最近一次成功 run 的对应平台 Artifacts。
  直接链接：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter/actions/workflows/rust-wheels.yml
- 不要在目标机器本地编译 Rust；没有可用 wheel 时保持纯 Python 降级运行。

开始前先向我索取：
1. 部署模式：单服 / Velocity 群组服 / 多个独立服务器。
2. Minecraft 服务器根目录；Velocity 群组服需要 Velocity 根目录和每个后端服根目录，也可以直接提供 logs/latest.log 路径。
3. AstrBot 根目录、插件目录和实际运行 Python 路径。
4. 接收报告的 QQ 群号 / QQ 号 / AstrBot 会话 UMO：
   - 完整 UMO 最稳，推荐在 AstrBot 里对目标会话执行 /sid 获取，例如 napcat:GroupMessage:123456。
   - QQ 群号可写成 group:123456789，QQ 号可写成 qq:10001。
   - 多个目标写成列表；若不同 server_id 要发到不同群/QQ，请逐一说明。
5. 日志量量级：小服默认档 / 大服性能优先档。
6. 是否允许现在重启 AstrBot。

执行要求：
1. 检查目录存在，识别 AstrBot 插件目录、MineSentinel 数据目录和现有配置。
2. 安装前把旧插件目录、配置文件和 plugin_data/mine_sentinel 备份到带时间戳的 backup 目录；如果没有旧数据也要说明。
3. 从 GitHub main 分支 clone 或覆盖安装源码到 AstrBot 插件目录。
4. 使用 AstrBot 实际运行的 Python 执行 pip install -r requirements.txt。
5. 可选启用 Rust 加速：只从上方 GitHub Actions 下载预编译 wheel 并 pip install <wheel>.whl；安装失败不要阻塞插件运行，记录为纯 Python 降级。
6. 在 mine_sentinel.runtime_log.sources 写入服务器 root、logs_dir 或 log_file；Velocity 群组服要把 Velocity 和所有后端服分别写成 source。
7. 开启 runtime_log、backfill_on_start、loop_filter_enabled、storage、report、send_as_image、send_full_log_file，并保留五段式报告输出。
8. 自动检测 AstrBot 已配置 bot 平台；读取 <AstrBot 根目录>/data/cmd_config.json 时用 utf-8-sig，识别 enable=true 的 aiocqhttp / qq_official / qq_official_webhook。若用户提供的是 group: 或 qq: 简写，优先解析到可用 QQ 平台；若平台不唯一，列出候选让我选择。
9. 全局投递目标写入 mine_sentinel.report.delivery_targets；单服单独投递写入 source.delivery_targets 或 source.target_sessions。send_to_target_sessions 默认保持 true。
10. 按日志量选择性能档位：
    - 小服默认档：runtime_log.template_parse_mode=all，runtime_log.anomaly_track_info=true，runtime_log.io_workers=0，report.export_format=jsonl，report.export_reuse_existing=true。
    - 大服性能优先档：runtime_log.template_parse_mode=interesting，runtime_log.anomaly_track_info=false，runtime_log.io_workers=2，report.export_format=jsonl.gz，report.export_reuse_existing=true。
11. 重启 AstrBot 后执行 /ms monitor status，确认日志源数量、轮询状态、observation/export 目录和 io_workers 生效。
12. 触发或等待一条 Minecraft 日志后执行 /ms report now <服务器ID> 30m；再执行 /ms report now 8h 验证全局报告。
13. 确认报告包含 overall、incidents、community、player_problems、risk_actions 五段，图片报告和 JSONL/JSONL.GZ 附件能发送到目标会话。
14. 最后汇总安装文件、备份位置、日志源 server_id、性能档位、投递目标、是否启用 Rust 加速、验证命令结果和需要我手动确认的事项。
```

### 已安装升级

```text
你是 Minecraft + AstrBot 升级助手。请把当前 astrbot_plugin_minecraft_adapter 升级为最新 MineSentinel 监控管理报告 AI，不要跳过备份、迁移检查和回归验证。

GitHub 仓库：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter
- 目标版本：main 分支。
- Rust 加速 wheel：GitHub Actions -> "Build Rust wheels" 工作流，下载最近一次成功 run 的对应平台 Artifacts。
  直接链接：https://github.com/EllanServer/astrbot_plugin_minecraft_adapter/actions/workflows/rust-wheels.yml
- 不要在目标机器本地编译 Rust；缺少 wheel 时允许纯 Python 降级。

升级前先记录并备份：
1. 当前插件目录、当前 git 分支、未提交改动和远端地址。
2. AstrBot 根目录、插件配置文件、实际运行 Python 路径。
3. plugin_data/astrbot_plugin_minecraft_adapter/mine_sentinel 和 plugin_data/mine_sentinel；两个路径存在任意一个都要备份。
4. 现有 mine_sentinel 配置、报告投递目标和日志源。

执行要求：
1. 如果旧插件目录有未提交改动，先生成 diff 备份，不要直接丢弃。
2. 切到 main 分支并拉取 https://github.com/EllanServer/astrbot_plugin_minecraft_adapter.git 最新代码；目录名继续保持 astrbot_plugin_minecraft_adapter。
3. 用 AstrBot 实际运行的 Python 执行 pip install -r requirements.txt。
4. 可选升级 Rust 加速 wheel：只从 GitHub Actions 下载预编译 wheel 并 pip install <wheel>.whl；失败时记录原因并继续纯 Python 路径。
5. 保留 mine_sentinel 下的日志源、投递目标、报告周期和导出配置；移除旧 Minecraft Adapter 的 Java 端插件、WebSocket、聊天桥、远程命令、玩家绑定等废弃配置。
6. 检查旧路径 plugin_data/astrbot_plugin_minecraft_adapter/mine_sentinel 是否已自动迁移到 plugin_data/mine_sentinel；迁移前后文件数量和最新 observation/export 文件要对得上。
7. 如果发现旧 .idx 偏移索引来自早期版本，优先备份后删除让新版重建；不想删除时把 mine_sentinel.storage.trust_legacy_index 设为 false 进入保守扫描模式。
8. 按日志量重新选择性能档位：
   - 小服默认档：template_parse_mode=all，anomaly_track_info=true，io_workers=0，export_format=jsonl。
   - 大服性能优先档：template_parse_mode=interesting，anomaly_track_info=false，io_workers=2，export_format=jsonl.gz。
9. 重启 AstrBot 后执行 /ms monitor status，确认日志源、轮询、backlog、异常检测、报告投递目标都正常。
10. 执行 /ms report now <服务器ID> 30m 和 /ms report now 8h，验证图片报告、文本兜底和完整窗口附件。
11. 检查报告包含 overall、incidents、community、player_problems、risk_actions 五段；正常启动/关闭、Hikari/连接池生命周期、代理握手和 Unknown or incomplete command 不应被误报为管理事件。
12. 最后汇总升级文件、备份位置、迁移结果、保留/删除的旧配置、性能档位、Rust 加速状态、验证命令结果和仍需人工确认的风险。
```

## 最小配置

```yaml
enabled: true
mine_sentinel:
  enabled: true
  retention_minutes: 480
  runtime_log:
    enabled: true
    sources:
      - server_id: survival
        server_name: 生存服
        server_type: minecraft
        root: /opt/minecraft/survival
        delivery_targets:
          - group:123456789
      - server_id: velocity
        server_name: 群组入口
        server_type: velocity
        logs_dir: /opt/minecraft/velocity/logs
    poll_interval_seconds: 5
    max_bytes_per_poll: 262144
    max_lines_per_poll: 200
    loop_filter_enabled: true
    template_parse_mode: all
  report:
    enabled: true
    interval_hours: 8
    default_window_minutes: 480
    delivery_targets:
      - group:123456789
    send_to_target_sessions: true
    send_as_image: true
    send_full_log_file: true
    export_format: jsonl
```

`sources` 支持字符串或对象。字符串可以是服务器根目录、`logs` 目录或 `latest.log` 路径；对象中 `log_file` 优先级最高，其次 `logs_dir`，最后 `root/logs/latest.log`。

投递目标建议优先使用 `/sid` 输出的完整 UMO，例如 `napcat:GroupMessage:123456`；也支持 `group:`、`qq:` 简写。source 级 `delivery_targets`/`target_sessions` 用于单服单独投递，全局 `mine_sentinel.report.delivery_targets` 用于周期总报告。

## 命令

- `/ms help`：查看 MineSentinel 命令。
- `/ms monitor status`：查看日志源、轮询、backlog、异常检测和报告状态。
- `/ms report now [服务器ID] [30m|8h]`：立即生成指定窗口报告；不传服务器 ID 时生成全局报告。

## Hourly 模式

如果只需要定期总结，不需要实时尾读，可以启用按小时总结模式。它每整点读取上一小时日志，支持 `.log.gz` 归档，生成小时摘要；累积 `hours_per_cycle` 后再整合为周期报告。

```yaml
mine_sentinel:
  runtime_log:
    enabled: false
  hourly_summary:
    enabled: true
    hours_per_cycle: 8
    poll_enabled: false
    max_records_per_hour: 5000
    max_log_lines_per_hour: 20000
  report:
    enabled: false
    delivery_targets:
      - group:123456789
```

该模式不持续轮询 `latest.log`，适合高负载服或只想要管理日报/班次报告的场景。未配置 LLM provider 时会退回规则启发式摘要。

## 性能建议

小服或默认部署：

```yaml
mine_sentinel:
  runtime_log:
    poll_interval_seconds: 5
    max_bytes_per_poll: 262144
    max_lines_per_poll: 200
    io_workers: 0
  report:
    max_records_for_ai: 160
    max_samples_per_issue: 4
```

大型多服或高日志量部署：

```yaml
mine_sentinel:
  runtime_log:
    poll_interval_seconds: 10
    max_bytes_per_poll: 524288
    max_lines_per_poll: 1000
    io_workers: 2
    anomaly_track_info: false
  report:
    max_records_for_ai: 240
    max_samples_per_issue: 3
    send_full_log_file: true
    export_format: jsonl.gz
```

关键原则：日志读取有字节/行数上限，报告窗口有采样上限，重复事故先聚合再入模。不要把原始日志整段塞给 AI。

规则报告在首次分类时同步生成运维计数，不再二次扫描整窗关键词。`ERROR/WARN` 以解析后的日志级别为准（级别缺失时回退结构化标签），`PERFORMANCE/NETWORK/PLUGIN/聊天审查` 等以最终主分类为准；单条聊天违规词只作为审查线索，达到玩家级重复行为阈值后才进入聊天审查事件计数。

玩家级刷屏检测使用单调时间窗和一次编辑以内的线性相似度判断，高聊天量窗口不会反复扫描窗口外消息。事件证据会归一化动态计数并优先保留不同错误/反作弊类型；五段式结构化输出统一使用中文事件标题、风险等级、聚合证据数、影响范围和处理建议，不暴露内部 `server_log_*` 标签。

## 开发与验证

推荐验证命令：

```bash
python -m unittest discover -s tests
python -m compileall -q services tests scripts
cargo fmt --manifest-path rust/Cargo.toml --check
cargo check --manifest-path rust/Cargo.toml
```

真实日志回归重点：

```bash
python -m unittest tests.test_mine_sentinel.MineSentinelRealLogPbfhCaITests
```

该测试会确认显式 `log_file` 只读取指定文件，pbfhCaI 样本能识别 `bug`、`plugin`、`network`、`economy`，五段式 section id 稳定，并过滤 Hikari/AstrbotAdapter/CMI 生命周期噪声。

## 迁移说明

- 旧 Adapter 的 Java 插件、WebSocket、聊天桥、远程命令、玩家绑定数据不会再被读取。
- MineSentinel observation、报告、导出附件统一放在 `plugin_data/mine_sentinel`。
- 旧 `.idx` 偏移索引若来自早期版本，建议删除后让新版本重建，以获得单调时间戳 seek 和窗口读取一致性。

## 许可证

本仓库根目录 `LICENSE` 为 GNU AGPL-3.0。图片报告默认字体缓存沿用项目原有字体策略；若使用 LXGW WenKai GB，字体项目采用 SIL Open Font License 1.1。
