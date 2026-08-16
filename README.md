# Mittes 的一天 (A Day With Mittes)

为 Mittes 提供日常环境上下文工具，全部通过 Tool 按需暴露给 LLM，**不**注入到任何 prompt：

- 角色日程：按 星期 × 小时 配置 Mittes 当前在做什么，支持节假日识别
- 实时天气：基于 [Open-Meteo](https://open-meteo.com) 的天气与近 3 天预报查询（无需 API key）

> 本插件由原 `schedule_plugin` 演进而来，新增了天气查询能力。配置层完全向后兼容。

## 注册的组件

| 组件类型 | 名称 | 说明 |
|----------|------|------|
| Tool | `get_current_schedule` | 查询 Mittes 当前活动状态（当前时段 + 下一时段，含节假日标记） |
| Tool | `get_weather` | 查询指定城市的实时天气和近 3 天预报 |

两个 Tool 都遵循"对方明确询问时才调用"的语义：日程仅在被问及"在做什么"时返回；天气仅在被问及天气或回复需要参考天气时返回。返回数据由 bot 用自己的语气转述给用户。

## 日程配置

配置文件：`config.toml`。按星期分表，键名格式为 `schedule_{weekday}.time_range_{HH}_{NN}`：

```toml
[schedule_mon]
time_range_00_01 = "穿着丝绸睡裙趴在窗边看夜景"
# ... 每天共 24 个时段
time_range_23_00 = "舒舒服服地泡在浴缸里玩手机"

[schedule_tue]
# ...
```

支持的星期前缀：`schedule_mon`、`schedule_tue`、`schedule_wed`、`schedule_thu`、`schedule_fri`、`schedule_sat`、`schedule_sun`。

时段键格式为 `time_range_HH_NN`（起止小时；23 点跨零点写作 `time_range_23_00`）。
某时段留空时，当天当前时段恰好落在该格则整个工具返回「当前没有日程信息」，建议每格都填。

### 顶层键 `weather_location`

`get_current_schedule` 返回时会附带此地点的当前天气摘要。取值用英文/罗马字地名
（如 `Tokyo`、`Shanghai`），避免中文被地理编码匹配到同名小地名。

> **TOML 陷阱**：这是顶层键，**必须写在所有 `[表]` 头之前**——放在任何表头之后
> 会被归入上一个表，插件按顶层读取将永远取不到（静默回退默认值 `Tokyo`）。

## 天气查询

`get_weather` 不需要任何配置即可工作，直接调用 Open-Meteo 公开 API：

- 地理编码：`https://geocoding-api.open-meteo.com/v1/search`（地名 → 经纬度）
- 天气数据：`https://api.open-meteo.com/v1/forecast`（当前实况 + 每日预报）

返回内容包含：城市名、当前天气状况、气温、体感温度、湿度、风速风向、未来 3 天预报。

## 数据目录

- `data/holidays/{year}.json` — 在线节假日数据缓存
- `data/schedules/{date}_{节日}.json` — 节假日专属日程缓存

## 安装

1. 把本目录放进 MaiBot 部署的 `plugins/` 下；
2. **复制 `config.example.toml` 为 `config.toml`**——本插件使用手动配置读取，
   `config.toml` 不会自动生成；不复制时日程工具会一直返回「当前没有日程信息」
   （天气工具无配置也能用）；
3. 把各时段活动改成你的角色设定，按需调整 `weather_location`；
4. 重启 MaiBot。
