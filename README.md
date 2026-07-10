# MineSentinel 监控管理报告 AI

MineSentinel 是一个面向 AstrBot 的 Minecraft 运行日志监控与管理报告插件。它只读读取服务器 `latest.log`、历史 `.log` 和 `.log.gz`，通过规则归因、异常检测、训练数据式清洗和 AstrBot 模型能力生成可直接发给管理组的五段式报告。

当前项目已经从旧的 Minecraft Adapter 转为纯监控报告 AI：不再包含 Java 端插件、WebSocket、聊天桥、远程命令、玩家绑定或跨端控制链路。仓库名 `astrbot_plugin_minecraft_adapter` 仅为了兼容原 AstrBot 插件仓库与历史安装路径。

## 核心能力

- 直接读取单服、Velocity 群组服和多后端服日志；每个 source 可配置独立 `server_id`、`server_name`、`server_type` 和投递目标。
- 启动时回扫最近窗口，实时尾读 `latest.log`，支持日志轮转、截断、重启补读和 `.log.gz` 归档读取。
- 将日志解析为 observation，写入 JSONL，并保留 OpenTelemetry Logs Data Model 风格字段，便于后续接 Loki / OTel-compatible 系统。
- 对重复 ERROR/WARN/Exception 做循环过滤，窗口切换前补发未落盘摘要；Vulcan 按玩家和检查项做带权压缩，压缩后总数与玩家分布不失真。
- 可选 Drain3 模板化与 EWMA/分位数异常检测，识别 `new_template`、`anomaly_spike`、突发 TPS/MSPT/GC/网络/插件异常。
- 先做确定性分类和事故聚合，再由 AI 针对重点 issue 复核命中原文前后各 40 条连续日志，生成“初步判断”和只读排查建议；证据不足时 AI 可受限扩展上下文，并可按配置调用 AstrBot 网页搜索核对官方资料，但不能修改严重度、计数、玩家、位置、证据、分类或 issue 集合。
- 输出五段式报告、现代化运维长图、文本兜底和完整窗口 JSONL/JSONL.GZ 附件；长图使用状态页眉、风险指标、编号章节、严重度事件卡和高密度证据区，便于管理组快速扫读。

## 分析链路

```text
raw log line
  -> sanitize              去 ANSI / 控制字符 / 超长行裁剪
  -> runtime hints          快速抽取时间、等级、线程、插件、聊天、Vulcan、Hikari、ops hint
  -> template/anomaly       Drain3 模板、EWMA 基线、新模板和突增标记
  -> loop filter            合并同类死循环报错
  -> rule classifier        确定性分类、严重级别、推荐动作、事故聚合
  -> LLM clean              URL/邮箱/UUID/IP/token 脱敏，质量评分，clean hash 去重
  -> prompt sampling        全通道脱敏、确定性抽样，过滤低价值日常指标
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

文本与图片报告默认使用 5 分钟事故窗口；玩家反馈按聊天主类先分流，再使用 15 分钟对话窗口，避免把普通建议、权限异常、传送问题和管理求助压成同一个全天事件。无论报告跨度多长，只要静默超过聚合窗口就会拆分，不会把全天同类 WARN 压成一个事故。重点事件按风险优先、时间次序展示；超过正文上限时明确给出省略数量并指向完整附件。服务器 WARN/ERROR 与同时间玩家技术反馈仍会按共同根因关联。安静窗口作为补充说明，不占用事件编号；插件更新检查、兼容性、本地化、普通建议和社区活动会进入观察摘要，不抬高重点事件数量。

## 智能分类

内置分类包括 `daily`、`complaint`、`bug`、`network`、`plugin`、`economy`、`community`、`chat_review`、`player_feedback`、`community_ops`、`moderation`、`cross_server`、`suggestion`。

分类优先使用结构化 runtime hints 和 ops hints，再回退到关键词、上下文、日志等级、线程、插件名和事故聚合。真实样本 `tests/fixtures/mclogs_pbfhCaI.log` 来自 [mclo.gs/pbfhCaI](https://mclo.gs/pbfhCaI)，用于验证 QuickShop/经济异常、数据库异常、插件异常、网络异常、离线模式/认证绕过风险会进入正确分类；Malformed JSON、JSON/NBT 转换失败会归入插件配置/数据解析异常，MythicMobs 内容定义、插件依赖、外部 API 凭据、外部资源获取和不安全运行模式会给出独立运维子类型，而不是泛化 Java bug。同时 Hikari 生命周期日志、AstrbotAdapter/CMI 正常代理握手不会误报为管理事件；Java 纯堆栈帧、诊断续行和装饰横幅只作为上下文，插件更新检查、兼容性/弃用、本地化资源键缺失、普通建议和单个插件任务调度延迟等低风险信号会进入观察分类，不升级为管理事件。

统计异常只表示模板出现频率偏离 EWMA/分位数基线，最多把有真实故障语义的事件提升到 `high`；`critical` 必须来自崩溃、OOM、watchdog、服务停止等确定性语义或结构化紧急分类。健康 `INFO` 即使获得高 anomaly score 也不会制造事件或虚增证据数。Vulcan 海量告警按玩家和检查类型聚合到第三段“聊天与社区观察”，不会被误写成服务器崩溃事故或回滚建议。普通“购买飞行/免费飞行”不会仅因出现“飞行”二字变成外挂举报，必须同时存在举报、怀疑、警告、停止、处罚或管理处理语义。

通用主报告润色模型使用最小 JSON 协议，只返回 `category/tag/incident_index` 绑定的 `suggested_action`；事件级深度诊断另行返回带证据索引的判断、建议和受限工具请求。独立的候选 issue reviewer 可以在高置信度下删除明确误报；其余事实始终来自确定性初稿。最终 prompt 的 timeline、fallback、聊天统计、异常样本和抽样记录统一经过 URL、邮箱、UUID、IP 与长 token 脱敏，避免清洗文本从旁路字段重新泄漏。

## AI 深度诊断与受限工具

AI 参与时，每个重点 issue 会先定位到证据命中记录，并读取该记录前后各 `ai_context_radius` 条连续原文，默认是 `40 + 命中行 + 40` 共 81 条。原文保留日志顺序和关键堆栈语义，但 URL、邮箱、IP、UUID、token 与控制字符仍会脱敏。模型必须引用实际提供的 `record_index`，报告才接受其“初步判断”和建议。

如果首轮证据不足，模型可以请求两类受限工具：

- `expand_context`：从当前可见记录边界继续向前或向后读取，上下文中心、半径和轮次都受配置限制。
- `web_search`：通过 AstrBot 已启用的内置 Tavily、Bocha、Brave、Firecrawl、百度 AI Search 或 Exa 工具检索插件官方文档、兼容矩阵与已知错误。搜索查询会再次脱敏，不允许检索玩家、IP、UUID 或 token；网络结果只辅助建议，不能覆盖本地日志事实。报告会显示实际采用的参考来源。

网页搜索复用 AstrBot 的配置和密钥，MineSentinel 不保存独立搜索密钥。请先在 AstrBot WebUI 开启网页搜索并选择提供方；具体配置参考 [AstrBot 函数调用文档](https://docs.astrbot.app/use/function-calling.html)。模型或搜索服务不支持工具时会自动退回本地上下文诊断，不影响规则报告生成。

默认最多深度诊断风险最高的 8 个 issue，每个 issue 首轮 1 次模型调用，只有模型明确认为证据不足时才追加上下文或搜索调用。希望降低模型费用时，可将 `ai_max_diagnosed_issues` 调为 3、`ai_context_expansion_rounds` 调为 1，或关闭 `ai_web_search_enabled`；关闭 `ai_diagnosis_enabled` 可恢复仅使用通用压缩报告 prompt 的模式。

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

`mine_sentinel_rs` 是可选 PyO3 扩展，不安装也能用纯 Python 路径完整运行。安装后只启用基准确认有收益的热路径：

- runtime hints：日志等级、时间、线程、插件、聊天、Vulcan、Hikari、ops hint 批处理；支持剥离 UTF-8 BOM/零宽传输字符并标记诊断续行。
- observation priority：高日志量窗口下的优先级抽样。
- AI sampling features：prompt 入模前的清洗 key、质量评分、低价值指标过滤。
- report category features：高日志量窗口（默认至少 8000 条）先跳过 Vulcan、daily noise、堆栈/诊断续行和已有 ops hint 的直达记录，再通过 Aho-Corasick 与 ASCII token 单次扫描为其余候选生成分类 bitmask；普通窗口保留惰性 Python 分类，Python 始终负责排除规则、优先级和最终结论。

逐条 JSONL codec 的 PyO3 对象转换在真实 5k 样本上慢于 CPython `json`/容器热路径，因此生产 wrapper 主动使用更快的 Python 实现；原生 codec ABI 仅保留给兼容与等价测试。项目不会为了“看起来 Rust 化”启用负优化。

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
    ai_diagnosis_enabled: true
    ai_context_radius: 40
    ai_context_line_chars: 1000
    ai_context_expansion_rounds: 2
    ai_max_context_radius: 160
    ai_max_diagnosed_issues: 8
    ai_tools_enabled: true
    ai_web_search_enabled: true
    ai_max_web_search_queries: 2
    ai_tool_timeout_seconds: 45
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
    max_ai_records: 160
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
    max_ai_records: 240
    max_samples_per_issue: 3
    send_full_log_file: true
    export_format: jsonl.gz
```

关键原则：日志读取有字节/行数上限，报告窗口有采样上限，重复事故先聚合再入模。普通日志和 `.gz` 按行流式读取，小时 observation 以 1024 条分块执行 hints，`.gz` LRU 同时受条目数与总字节限制；不会再把整个源文件、整小时 rows 和 hints 同时物化。不要把原始日志整段塞给 AI。

规则报告在首次分类时同步生成运维计数，不再二次扫描整窗关键词。`ERROR/WARN` 以解析后的日志级别为准（级别缺失时回退结构化标签），`PERFORMANCE/NETWORK/PLUGIN/聊天审查` 等以最终主分类为准；单条聊天违规词只作为审查线索，达到玩家级重复行为阈值后才进入聊天审查事件计数。

玩家级刷屏检测使用滑动时间窗、精确值/删除签名/短子串候选索引和原相似度复核，互不相似消息不再触发平方扫描；本地 4k 敌对样本由约 2.5 秒降到约 67 毫秒。报告窗口的 reservoir 使用固定局部随机序列，同一有序输入在缓存过期或重启后仍选择相同背景样本。事件证据会剔除健康 INFO、归一化动态计数，并按证据频次排序结构化子类型；五段式结构化输出统一使用中文事件标题、风险等级、聚合证据数、影响范围和处理建议，不暴露内部 `server_log_*` 标签。10k 行真实样本中，Rust 分类只扫描需要 fallback 的候选记录，避免对 Vulcan 与 ops hint 直达记录做无效预计算。

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
python -m unittest tests.test_mine_sentinel.MineSentinelRealLogV54kwMiTests
```

这些测试会确认显式 `log_file` 只读取指定文件，pbfhCaI 样本能识别 `bug`、`plugin`、`network`、`economy`，五段式 section id 稳定，并过滤 Hikari/AstrbotAdapter/CMI 生命周期噪声；v54kwmi 样本用于验证 10k 行窗口、4202 条 Vulcan 在在线 loop filter 前后计数一致、连接池 WARN 按时间切分、玩家反馈语义分流和统计异常严重度上限。

## 迁移说明

- 旧 Adapter 的 Java 插件、WebSocket、聊天桥、远程命令、玩家绑定数据不会再被读取。
- MineSentinel observation、报告、导出附件统一放在 `plugin_data/mine_sentinel`。
- 旧 `.idx` 偏移索引若来自早期版本，建议删除后让新版本重建，以获得单调时间戳 seek 和窗口读取一致性。

## 许可证

本仓库根目录 `LICENSE` 为 GNU AGPL-3.0。图片报告默认缓存 Noto Sans SC 无衬线字体，以获得更清晰的现代化长图排版；字体项目采用 SIL Open Font License 1.1，下载失败时自动回退到 AstrBot 自定义字体或系统中文字体。
