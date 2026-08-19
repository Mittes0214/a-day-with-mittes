# Mittes 的一天 (A Day With Mittes)

MaiBot 插件。给角色一份每天自动生成的日程，并让它自然地影响她的状态和说话方式。

## 它做什么

- **每天自动生成次日全天日程**。一天切成 10 个变长时段，每段由 LLM 写出当时的
  故事、心情、忙碌度和说话方式。
- **说话方式跟着时段走**。累了话短、忙起来回得慢、刚睡醒有点迷糊——
  但她**不会把日程内容念出来**（"我现在正在洗碗呢"那种）。
- **每段挑一件值得说的小事**。聊到相关的就自然提一句，说过之后就不再提。
- **心情有起伏但不随机**。每周随机安排几次"不顺心的事"，让心情跟着具体事件走。
- **结果永久归档**进 SQLite，自带一个网页浏览器随时翻看。
- **生成失败不影响聊天**。冷启动或调用失败时用手写底稿顶着，回复链路不阻塞。

顺带提供一个实时天气查询工具和日本节假日识别。

## 安装

1. 把整个目录放进 MaiBot 的 `plugins/` 下。
2. 复制 `config.example.toml` 为 `config.toml`，按下面说明改。
3. 重启 MaiBot。

首次启动会自动补生成今天和明天的日程，期间用底稿顶着。

## 配置

```toml
[generation]
run_at       = "12:00"             # 每天几点跑批次，生成的是次日全天
model        = "claude-sonnet-5"   # 时段生成
digest_model = "glm-5.2"           # 当日概要压缩
topic_model  = "glm-5.2"           # 话题提炼
base_task    = "memory"            # 基座任务，见下
temperature  = 0.9

negative_event_quota  = 2          # 每周安排几次"不顺心的事"
negative_medium_ratio = 0.3        # 其中判为"中等"强度的比例

[observability]
report_group_id = ""               # 批次结果报到哪个群，留空则不报
weather_location = "Tokyo"         # 天气查询用的地名

[components]
enable_get_current_schedule = true # 关掉的 Tool 不会出现在 LLM 的工具列表里
enable_get_weather = true
```

三个 `*_model` 填的是 **`model_config.toml` 里 `[[models]].name` 的模型名**，不是任务名。
`base_task` 填任务名，只用来借它的 `hard_timeout` 和统计管线——挑一个超时够宽的
（时段生成实测出现过 36.9 秒）。原因见 [DESIGN.md](DESIGN.md)。

日程骨架写在 `schedule_skeleton.toml`（7 天 × 10 段，手写，纳入版本库）。
它规定每段的时间、名称、地点、服装、同处的人和性质，LLM 只负责写"这些事实今天
具体表现成什么样"。换角色或换季就整份替换这个文件。

## 命令

仅 operator 可用。

| 命令 | 作用 |
|---|---|
| `/status` | 当前时段的各字段、时段边界 |
| `/status day` | 今天各段的骨架 + 生成状态 |
| `/status prompt` | 本时段实际注入的**原文** |
| `/status topic` | 当前时段的话题、关键词、分享状态 |
| `/status db` | 归档库覆盖范围、段数、文件大小 |
| `/status batch [日期\|today]` | 立即跑一次批次，默认次日 |
| `/status topics [日期]` | 只重跑话题提炼，默认今天 |
| `/status regen` | 强制重生成当前时段，新旧并排 |
| `/status next` | 提前生成下一段但不切换 |
| `/status neg` | 本周"不顺心的事"排期 |
| `/status neg reroll` / `clear` | 重摇 / 清空本周排期 |
| `/status neg add <日期> <时段> [轻微\|中等]` | 手动指定一条 |

`/status prompt` 最常用——直接看到注入了什么、注在哪，不用去翻日志。

耗时的几条（`batch` / `topics` / `regen` / `next`）会立刻返回"已在后台开始"，
跑完再报结果。**跑的期间不要改插件目录里的文件，也不要重启**——
主程序有文件监听热重载，会把后台批次连协程一起带走，且不留日志。

## 查看日程

```bash
python viewer.py                    # 只有本机能开
python viewer.py --lan              # 局域网内可开，启动时打印地址
python viewer.py --lan --port 9000
```

纯标准库，不需要装依赖，也不需要 bot 的虚拟环境。左侧按日期列表，
右侧是当天十段的完整内容，外加话题、关键词和"她有没有把这件事说出去"。
打开时自动定位到当前时段。

> `--lan` 监听 `0.0.0.0` 且**没有鉴权**，同一局域网内知道地址的人都能看到全部内容。
> 别往公网做端口映射。

## 归档库

`data/schedule.db`，SQLite，永久保留。三张表：

| 表 | 一行是什么 |
|---|---|
| `days` | 一天，存批次元信息（天气、节假日、成功段数、耗时、当日概要） |
| `segments` | 一个时段，存骨架快照 + 生成出来的 story / manner / mood / busy / topic |
| `shares` | 某条话题在某个会话的状态：注入过几次、有没有说出口、说的原话 |

删掉数据库就退回纯手写的底稿状态，LLM 绝不回写骨架。
库开了 WAL，自己写前端时请用只读方式打开：

```python
con = sqlite3.connect("file:.../data/schedule.db?mode=ro", uri=True)
```

往表里加列时要同步往 `schedule_db.py` 的 `_EXPECTED_COLUMNS` 加一条，
不能靠删库重建——原因见 [DESIGN.md](DESIGN.md)。

## 更多

[DESIGN.md](DESIGN.md) —— 为什么这样设计、写生成类 prompt 的两条原则、常见问题。

其中「三通道注入」和「两条 prompt 原则」这两部分和本角色无关，做别的 LLM 生成
功能大概也用得上。

## 许可

GPL-v3.0-or-later
