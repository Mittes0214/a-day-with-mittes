# Mittes 的一天 (A Day With Mittes)

MaiBot 插件。给角色一份每天自动生成的日程，让它影响她的状态和说话方式。

## 它做什么

- 每天自动生成次日全天日程，**一次调用出十来段**。一天切成十来个变长时段，
  每段有当时的故事、心情、所在地点和说话方式。写手看得见全天骨架，
  所以一件事可以跨几段发展再收，而不是十段各写各的。
- 说话方式跟着时段走。累了话短，忙起来回得慢，刚睡醒有点迷糊。
  但她不会把日程内容念出来（"我现在正在洗碗呢"那种）。
- 每段挑一件值得说的小事。聊到相关的就提一句，说过之后就不再提。
- 心情有起伏但不随机。每周随机安排几次"不顺心的事"，让心情跟着具体事件走。
- 能查她在哪、穿什么。两个工具，可以问此刻，也可以问她今天早些时候或明天。
- 结果永久归档进 SQLite，自带一个网页浏览器随时翻看。
- 生成失败不影响聊天。冷启动或调用失败时用手写底稿顶着，回复链路不阻塞。

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
model        = "claude-sonnet-5"   # 全天生成
topic_model  = "claude-sonnet-5"   # 第二轮抽取
base_task    = "memory"            # 基座任务，见下
temperature  = 0.9

negative_event_quota  = 2          # 每周安排几次"不顺心的事"
negative_medium_ratio = 0.3        # 其中判为"中等"强度的比例

[observability]
report_group_id  = ""              # 批次结果报到哪个群，留空则不报
weather_location = "Tokyo"

[components]
enable_get_mittes_schedule = true  # 关掉的 Tool 不会出现在 LLM 的工具列表里
enable_get_mittes_outfit   = true
enable_get_weather         = true
```

两个 `*_model` 填的是 `model_config.toml` 里 `[[models]].name` 的模型名，不是任务名。
`base_task` 填任务名，只用来借它的 `hard_timeout`，挑一个超时够宽的
（全天生成实测 147~195 秒，240 秒只剩两成余量）。原因见 [DESIGN.md](DESIGN.md)。

## 两份手写资产

| 文件 | 装什么 |
|---|---|
| `schedule_skeleton.toml` | 一周的时段骨架：每段的时间、名称、地点、穿搭名、同处的人、性质 |
| `wardrobe.toml` | 几套穿搭，每套一个名字 + 从头到脚的细节 |

骨架的 `outfit` 填的是衣柜里的套装名。名字进 prompt 给 story 用，
从头到脚的细节只给查询工具。两种粒度为什么分开，见 [DESIGN.md](DESIGN.md)。

一天从凌晨 02:00 算起，跨零点那段写成 `24:00-26:00`，归属当天。

LLM 只负责写"这些事实今天具体表现成什么样"，绝不回写骨架。
换季或换角色时，这两份文件整份替换，不打补丁。

## 命令

仅 operator 可用。

| 命令 | 作用 |
|---|---|
| `/status` | 当前时段的各字段、所在地点、时段边界 |
| `/status day` | 今天各段的骨架 + 生成状态 |
| `/status prompt` | 本时段实际注入的原文 |
| `/status topic` | 当前时段的话题、关键词、分享状态 |
| `/status db` | 归档库覆盖范围、段数、文件大小 |
| `/status batch [日期\|today]` | 立即跑一次批次，默认次日 |
| `/status topics [日期]` | 只重跑第二轮（地点时段轴 + 话题） |
| `/status regen` | 定向重写当前时段，新旧并排 |
| `/status next` | 提前生成下一段但不切换 |
| `/status neg` | 本周"不顺心的事"排期 |
| `/status neg reroll` / `clear` | 重摇 / 清空本周排期 |
| `/status neg add <日期> <时段> [轻微\|中等]` | 手动指定一条 |

`/status prompt` 最常用，直接看到注入了什么、注在哪，不用去翻日志。

耗时的几条（`batch` / `topics` / `regen` / `next`）会立刻返回"已在后台开始"，跑完再报结果。
**跑的期间不要改插件目录里的文件，也不要重启**：主程序有文件监听热重载，
会把后台批次连协程一起带走，且不留日志。

## 查看日程

```bash
python viewer.py                    # 只有本机能开
python viewer.py --lan              # 局域网内可开，启动时打印地址
python viewer.py --lan --port 9000
```

纯标准库，不需要装依赖，也不需要 bot 的虚拟环境。左侧按日期列表，
右侧是当天各段的完整内容，外加地点时段轴、话题和"她有没有把这件事说出去"。
打开时自动定位到当前时段。

> `--lan` 监听 `0.0.0.0` 且**没有鉴权**，同一局域网内知道地址的人都能看到全部内容。
> 别往公网做端口映射。

## 归档库

`data/schedule.db`，SQLite，永久保留。三张表：

| 表 | 一行是什么 |
|---|---|
| `days` | 一天，存批次元信息（天气、节假日、耗时、脉络） |
| `segments` | 一个时段，存骨架快照 + 生成出来的 story / manner / mood / places / topic |
| `shares` | 某条话题在某个会话的状态：注入过几次、有没有说出口、说的原话 |

删掉数据库就退回纯手写的底稿状态。库开了 WAL，自己写前端时请用只读方式打开：

```python
con = sqlite3.connect("file:.../data/schedule.db?mode=ro", uri=True)
```

往表里加列时要同步往 `schedule_db.py` 的 `_EXPECTED_COLUMNS` 加一条。
不能靠删库重建，`CREATE TABLE IF NOT EXISTS` 对已存在的表不做任何改动。

## 更多

[DESIGN.md](DESIGN.md)：为什么这样设计、写生成类 prompt 的两条原则、常见问题。

其中「四个通道」和「两条 prompt 原则」这两部分和本角色无关，
做别的 LLM 生成功能大概也用得上。

## 许可

GPL-v3.0-or-later
