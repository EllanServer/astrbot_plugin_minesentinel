# MineSentinel Minecraft 日志审计

这是一个 AstrBot 插件，用来直接读取 Minecraft 服务器运行日志并生成 AI 巡检总结。当前版本不再依赖 Java 端插件、WebSocket、聊天桥、远程命令或玩家绑定；核心输入只有 Minecraft `logs/latest.log`、历史 `.log` 和压缩 `.log.gz`。

## 功能

- 直接按路径读取单服或 Velocity 群组服日志。
- 启动时回扫最近窗口，避免 `latest.log` 轮转或压缩后 8 小时总结缺日志。
- 实时尾读 `latest.log`，轮转、截断或重启后会自动补读近期日志。
- 过滤服务器报错死循环：同类 ERROR/WARN/Exception 只保留首条和周期性摘要。
- AI 生成五段式巡检报告，并保留图片渲染和完整 JSONL 附件。
- 社区管理单独分类：ban、kick、mute、report、spam、grief、cheat、举报、封禁、禁言、刷屏等会进入 `community`，不混入普通插件报错。
- 注重性能和内存安全：追加读取限制字节数，末尾补读按块读取，回扫分批写入 JSONL，报告窗口有内存上限和优先级采样。

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
        root: "D:\\minecraftserver"
    backfill_on_start: true
    backfill_window_minutes: 480
    loop_filter_enabled: true
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

`root` 会自动定位到 `root/logs/latest.log`。也可以直接写 `log_file: "D:\\server\\logs\\latest.log"`。Velocity 群组服把 Velocity 根目录和每个后端服根目录都列到 `runtime_log.sources`。

每个 source 可以单独写 `delivery_targets` 或 `target_sessions`，用于把特定服务器报告发到指定 AstrBot 会话；全局 `mine_sentinel.report.delivery_targets` 用于总报告投递。目标建议优先使用 `/sid` 输出的完整 UMO，也支持 `group:`、`qq:` 简写。

## 报告与附件

报告正文默认渲染为 PNG；如果图片组件或字体加载失败，会自动回退为文本。完整窗口记录会导出到 `mine_sentinel/exports/*.jsonl` 并尝试作为群文件附件发送，方便管理员复核原始日志。

分类包括：

- `bug`：ERROR、Exception、插件加载失败、崩溃等。
- `complaint`：TPS、卡顿、超时、Can't keep up、Overloaded 等性能/可用性日志。
- `community`：社区管理事件，如封禁、踢出、禁言、举报、刷屏、作弊、破坏。
- `moderation`：权限、登录、白名单、认证相关日志。
- `economy`：经济、商店、Vault、金币等相关日志。
- `cross_server`：Velocity、proxy、backend、跨服转发相关日志。
- `daily`：普通启动、停止和信息日志。

## 部署提示词

把下面这段给有本机文件读写权限的 AI 助手即可：

```text
你是 Minecraft + AstrBot 部署助手。请帮我部署 MineSentinel 日志审计插件，不要跳过备份和验证。

开始前先向我索取：
1. 部署模式：单服 / Velocity 群组服。
2. Minecraft 服务器根目录；Velocity 群组服需要 Velocity 根目录和每个后端服根目录。
3. AstrBot 根目录和实际运行 Python 路径。
4. 接收报告的 AstrBot 会话 UMO，优先使用 /sid 输出；也可提供 group: 或 qq: 简写。
5. 是否现在重启 AstrBot 和 Minecraft 服务端。

执行要求：
1. 检查目录存在，识别 AstrBot 插件目录、MineSentinel 数据目录和现有配置。
2. 从 GitHub Actions 下载 astrbot_plugin_minecraft_adapter 主分支最新 successful wheel/source，不要在目标机器本地编译 Rust。
3. 安装 AstrBot 插件源码到插件目录；覆盖前把旧目录和配置备份到带时间戳的 backup 目录。
4. 用 AstrBot 实际 Python 安装 mine_sentinel_rs wheel，并验证 import mine_sentinel_rs 成功。
5. 在 mine_sentinel.runtime_log.sources 写入服务器根目录或 latest.log 路径；Velocity 群组服写入 Velocity 和所有后端服。
6. 开启 runtime_log、backfill_on_start、loop_filter_enabled、storage、report、send_as_image、send_full_log_file。
7. 报告目标写入 mine_sentinel.report.delivery_targets，优先使用 /sid 完整 UMO。
8. 重启后执行 /mc monitor status，确认日志源数量和 observation/export 目录。
9. 触发或等待一条 MC 日志后执行 /mc report now <服务器ID> 30m，验证图片报告和 JSONL 附件能发送。
10. 最后汇总安装文件、备份位置、日志源 server_id、wheel 文件名、验证结果和需要我手动确认的事项。
```

## 许可证

本插件沿用项目原有 AGPL-3.0 许可。图片报告默认使用 LXGW WenKai GB 字体缓存，字体项目采用 SIL Open Font License 1.1。
