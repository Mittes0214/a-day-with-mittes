# Mittes 的一天 (A Day With Mittes)

MaiBot 插件。给角色一份每天自动生成的日程，让它影响她的状态和表达方式。

## 它做什么

- 每天自动生成次日全天日程，**一次调用出十来段**。一天切成十来个变长时段，
  每段有当时的故事、心情、所在地点和表达方式。写手看得见全天骨架，
  所以一件事可以跨几段发展再收，而不是十段各写各的。
- 表达方式跟着时段走，并在第三轮根据每一段的故事和心情单独生成。
  但她不会把日程内容念出来（"我现在正在洗碗呢"那种）。
- 每段挑一件值得说的小事。可以用它回应相关话题，也可以在对话松下来时独立吐槽或分享；
  不会先答完再生硬追加。换不换题由 planner 决定；说过之后 planner 和 replyer 两边
  同时不再提（`stop_after_shared` 可以只关掉 replyer 那半边，用来观察她会不会自己重复）。
  关联起来的几个群算同一个"说过"。
- 心情有起伏但不随机。每周随机安排几次"不顺心的事"，让心情跟着具体事件走。
- 能查她在哪、穿什么。穿搭工具返回对应套装的设定图，planner 与 replyer 共用原图，
  可以问此刻，也可以问她今天早些时候或明天。
- 结果永久归档进 SQLite，自带管理页，可以热编辑表达方式、按日期生成日程或重跑 topic。
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
run_at          = "12:00"             # 每天几点跑批次，生成的是次日全天
model           = "claude-sonnet-5"   # 全天生成
topic_model     = "claude-sonnet-5"   # 第二轮抽取
expression_model = "claude-sonnet-5"  # 第三轮表达方式
base_task       = "memory"             # 基座任务，见下
temperature     = 0.9

negative_event_quota  = 2          # 每周安排几次"不顺心的事"
negative_medium_ratio = 0.3        # 其中判为"中等"强度的比例

[manner]
enabled          = false            # 表达方式（replyer 的 A 块）总开关。
                                    # 关掉后第三轮不生成、replyer 不注入、管理页不显示；
                                    # 库里已有的 manner 原样保留，改回 true 就恢复

[topic]
enabled          = true             # 谈资总开关，关掉后两条通道都不注入、工具也不暴露
stop_after_shared = true            # false：用掉之后仍然每轮注入
linked_groups    = []               # 例 [["1016077305","1059037995"]]：组内任一群说过就都算说过
linked_platform  = "qq"

[topic.reply_channel]               # 通道一：接话（常驻，只准接话不准转话题）
enabled = true

[topic.pitch_channel]               # 通道二：开口（get_mittes_topic，只在窗口期暴露）
enabled            = true
fresh_minutes      = 30             # 新鲜期：时段开始后 30 分钟内
breakpoint_minutes = 60             # 断点期：从"下班/睡醒"起最多 60 分钟
busy_kinds         = ["睡眠","工作","上学"]
idle_kinds         = ["自由","外出","通勤"]
lookback_minutes   = 10             # 「对方正在回应她」往回看多久
max_pitches        = 3              # 每段每会话最多被取材几次

[observability]
report_group_id  = ""              # 批次结果报到哪个群，留空则不报
report_platform  = "qq"
weather_location = "Tokyo"         # 生成时查天气预报用的地名

[components]
enable_get_mittes_schedule = true  # 关掉的 Tool 不会出现在 LLM 的工具列表里
enable_get_mittes_outfit   = true
enable_get_mittes_topic    = true
enable_get_weather         = true
```

三个 `*_model` 填的是 `model_config.toml` 里 `[[models]].name` 的模型名，不是任务名。
`base_task` 填任务名，只用来借它的 `hard_timeout`，挑一个超时够宽的
（全天生成实测 147~195 秒，240 秒只剩两成余量）。原因见 [DESIGN.md](DESIGN.md)。

## 目录

| 目录 | 内容 |
|---|---|
| `generation/` | 三阶段 LLM 生成管线、prompt 加载器与完整 prompt |
| `schedule/` | 日程骨架、内存状态、SQLite 归档、节假日和负面事件排期 |
| `character/` | 衣柜查询代码与衣柜资产 |
| `weather/` | 实时天气和预报查询 |
| `observability/` | WebUI 推理过程记录 |
| `frontend/` | 日程归档与管理页面 |
| `data/` | 数据库、节假日缓存等运行时数据，不进版本库 |

手写资产包括 `schedule/skeleton.toml`、`character/wardrobe.toml`、
`character/outfit_images.toml` 和
`generation/prompts/*.prompt`。prompt 一次请求使用一个完整文件，改配方不用碰 Python。

骨架的 `outfit` 填的是衣柜里的套装名。名字进 prompt 给 story 用，
服装细节保存在衣柜并展示在管理页，穿搭工具只返回设定图。两种粒度为什么分开，见 [DESIGN.md](DESIGN.md)。

`get_mittes_outfit` 按查询时刻选择套装，成功结果只有 `【Mittes 的服饰参考资料】`。
插件在 planner 请求前，于成功查询的结果之后单独补一条 `user` 多模态资料；
在 replyer 请求前，于 `reply_reference` 之前补入相同图片。资料内容只有标题和原图，
不追加服装清单、时间或套装名。

图片仅存在于模型请求，不通过 `content_items` 返回，不注册 `tool_result:…` 媒体索引，
也不作为可转发的图片写入聊天历史。服饰工具不提供发送或转发设定图的功能，从来源上去掉
转发通道，不靠发送前取消消息，以免 planner 误以为发送失败而反复尝试。

图片关联使用主程序的逻辑轮次 ID 和工具调用 ID，全部在插件内部完成。
同一轮查询多个时刻时按顺序保留不同套装；跨轮、跨会话不沿用，失败的查询不注入。
replyer 第一次请求固定图片名单，重试保持不变。replyer 实际选中的模型必须支持视觉；
非视觉模型会跳过图片并记录警告，不退回文字清单。planner 沿用主程序的视觉模型配置。

换季时同步更新 `outfit_images.toml` 和图片文件，映射必须覆盖衣柜中的全部套装。
图片首次读取和编码在线程中完成，随后缓存；替换图片后重启插件以刷新缓存。

回归检查（在 MaiBot 根目录运行，不请求模型）：
`uv run python -m pytest plugins/01_a_day_with_mittes/tests/test_outfit_images.py -q`。

一天从凌晨 02:00 算起，跨零点那段写成 `24:00-26:00`，归属当天。

骨架一共九张表：周一到周日各一张，外加两张祝日表。**哪天用哪张不由星期几决定**——
日本的法定祝日学校放假，按周中骨架排会让她在法定假日的白天照旧"在上课"
（`上学` 是机器读的 busy kind，那几段她不会主动开话头）。祝日落在店定休日（周一）
走全休表，否则走全天班表；校历上那些不是祝日又改变整天形状的日子
（文化祭、考试周）写进 `skeleton.toml` 末尾的 `[calendar.override]`。
"今天是连休第几天、明天还用不用上学"不写进骨架，生成当天现算后注进 prompt。

LLM 只负责写"这些事实今天具体表现成什么样"，绝不回写骨架。
换季或换角色时，**骨架和衣柜这两份**整份替换、不打补丁——它们是配套的，
骨架的 `outfit` 填的是衣柜里的名字，只换一边会对不上。prompt 不跟着换季走。

## 命令

仅 operator 可用。

| 命令 | 作用 |
|---|---|
| `/status` | 当前时段的各字段、所在地点、时段边界 |
| `/status day` | 今天各段的骨架 + 生成状态 |
| `/status prompt` | 本时段实际注入的原文 |
| `/status topic` | 当前时段的话题、用掉没有、取材次数、谈资工具露不露 |
| `/status db` | 归档库覆盖范围、段数、文件大小 |
| `/status batch [日期\|today]` | 立即跑一次批次，默认次日 |
| `/status topics [日期]` | 只重跑第二轮（地点时段轴 + 话题） |
| `/status neg` | 本周"不顺心的事"排期 |
| `/status neg reroll` / `clear` | 重摇 / 清空本周排期 |
| `/status neg add <日期> <时段> [轻微\|中等]` | 手动指定一条 |

`/status prompt` 最常用，直接看到注入了什么、注在哪，不用去翻日志。

耗时的几条（`batch` / `topics`）会立刻返回"已在后台开始"，跑完再报结果。
**跑的期间不要改插件目录里的文件，也不要重启**：主程序有文件监听热重载，
会把后台批次连协程一起带走，且不留日志。

## 查看与管理日程

```bash
python frontend/viewer.py                    # 只有本机能开
python frontend/viewer.py --lan              # 局域网内可开，启动时打印地址
python frontend/viewer.py --lan --port 9000
```

纯标准库，不需要装依赖，也不需要 bot 的虚拟环境。左侧可以指定日期生成日程；
选中一天后可以重新生成当日日程、重新生成 topic，或只重跑当日全部表达方式。每段的表达方式都可直接编辑，
保存后由运行中的插件同步更新内存和归档，不用重启 bot。打开时自动定位到当前时段。

当日穿搭以设定图横向展示，相同搭配只显示一次，此刻穿着的搭配排在最前并标记。
左侧「衣柜」展示全部搭配的图片画廊；点击图片或日程中的穿搭名，可以放大查看和打开原图。
服饰文字说明及固定细节默认折叠，需要时再展开。图片与模型参考资料共用
`character/outfit_images.toml`，没有设定图的临时搭配会明确标注；图片按需加载，宽屏衣柜为双列，手机为单列。

> `--lan` 监听 `0.0.0.0` 且**没有鉴权**，同一局域网内知道地址的人不但能看到内容，
> 也能编辑表达方式和触发生成。别往公网做端口映射。

右侧是一张月历，正文宽度封了 900px，宽屏下原本空着的那一条就用来放它：

- **周六蓝、周日和祝日红**；鼠标停上去显示祝日名。
- **祝日和连休铺底色**，连休整段连成一条带子（中间的格子不留圆角，跨周就断在行尾）。
  只给祝日铺底色是不够的——凑成连休的那两天周末看着还是普通周末，一眼分不出这是五连休。
  普通的两天周末不铺，不然满屏都是底色，连休反而不显眼了。
- **有归档的日子下面有个点**，点一下直接跳过去；当前正在看的那天高亮，今天描细框。
- 下面列出**本月祝日**和**连休**（连着三天以上的非工作日才算，两天的普通周末不列）。
- 每个祝日下面有一句中文说明，连休下面写清楚是哪几天凑出来的（周末还是哪个祝日），
  黄金周 / 白银周额外挂个牌子。
- 日本没有调休补班，所以不存在"周末被征用去上班"这种格子。祝日撞上周日，补休顺延到
  之后头一个平日（振替休日，标"顺延"）；两个祝日中间夹的那天也放（国民の休日，标"夹日"）。
  这两种在 holidays-jp 的数据里本来就是独立条目，直接照着标。
- 页面的滚动条跟着主题走。浏览器默认那条在深色下是亮白的，比页面里任何内容都抢眼。

祝日表走 `/api/holidays?year=`：先读插件下好的缓存（`data/plugins/<插件ID>/holidays/JP/<年>.json`），
没有就由 viewer 自己去 holidays-jp 下一份写进同一个文件——插件只在生成那一年的日程时才下，
而日历可以被翻到任意一年。取不到时日历照常画，只是那一年没有祝日标记，页面上会写明。
**这是 viewer 唯一一处对外网发的请求。**

> 窄于 1240px 时日历自动收起（`aside` 238 + 正文 900 已经占满），
> 手机那档（≤640px）改成堆在正文下面。

## 归档库

`data/schedule.db`，SQLite，永久保留。四张表：

| 表 | 一行是什么 |
|---|---|
| `days` | 一天，存批次元信息（天气、节假日、耗时、脉络） |
| `segments` | 一个时段，存骨架快照 + 生成出来的 story / manner / mood / places / topic |
| `shares` | 某条话题在某个会话的状态：注入过几次、被取材过几次、有没有被用掉 |
| `admin_jobs` | 管理页递交给运行中插件的编辑与生成任务 |

删掉数据库就退回纯手写的底稿状态。库开了 WAL；只做外部查询时请用只读方式打开：

```python
con = sqlite3.connect("file:.../data/schedule.db?mode=ro", uri=True)
```

往表里加列时要同步往 `schedule/db.py` 的 `_EXPECTED_COLUMNS` 加一条。
不能靠删库重建，`CREATE TABLE IF NOT EXISTS` 对已存在的表不做任何改动。

## 更多

[DESIGN.md](DESIGN.md)：为什么这样设计、写生成类 prompt 的两条原则、常见问题。

其中「四个通道」和「两条 prompt 原则」这两部分和本角色无关，
做别的 LLM 生成功能大概也用得上。

## 许可

GPL-v3.0-or-later
