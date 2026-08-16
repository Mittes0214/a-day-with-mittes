"""天气查询模块

调用 Open-Meteo 免费 API（无需 key）获取指定地点的实时天气和未来 3 天预报。
- 地理编码：geocoding-api.open-meteo.com（地名 → 经纬度）
- 天气数据：api.open-meteo.com（当前实况 + 每日预报）

作者：Mittes
版本：1.0.0
"""

from typing import Any

import aiohttp
import urllib.parse


# WMO 天气代码 → 中文描述
_WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "中毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _wind_dir(degrees: float) -> str:
    """将风向角度转为中文方向。"""
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[round(degrees / 45) % 8]


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    """异步 HTTP GET，返回解析后的 JSON。"""
    async with session.get(url, timeout=_HTTP_TIMEOUT) as resp:
        return await resp.json()


async def fetch_weather(location: str) -> str:
    """查询指定城市/地点的天气，返回格式化文本。

    返回数据包含当前天气状况、气温、体感温度、湿度、风速风向、未来 3 天预报。
    任何 HTTP / 解析失败都会作为可读文本返回，调用方不需要 try/except。
    """
    headers = {"User-Agent": "MaiBot-WeatherTool/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        # 第一步：地理编码，将地名转为经纬度
        geo_url = _GEOCODING_URL + "?" + urllib.parse.urlencode({
            "name": location,
            "count": 1,
            "language": "zh",
            "format": "json",
        })
        try:
            geo = await _fetch_json(session, geo_url)
        except Exception as exc:
            return f"地理位置查询失败：{exc}"

        results = geo.get("results")
        if not results:
            return f"未找到「{location}」的位置信息，请尝试其他写法（如城市拼音或英文名）。"

        place = results[0]
        lat: float = place["latitude"]
        lon: float = place["longitude"]
        city: str = place.get("name", location)
        admin: str = place.get("admin1", "")
        country: str = place.get("country", "")

        # 拼接显示名
        display = city
        if admin and admin != city:
            display += f"({admin})"
        if country:
            display += f" · {country}"

        # 第二步：查询天气（当前实况 + 未来 4 天，跳过今天取后 3 天）
        weather_url = _WEATHER_URL + "?" + urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
                "precipitation",
            ]),
            "daily": ",".join([
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
            ]),
            "timezone": "auto",
            "forecast_days": 4,
            "wind_speed_unit": "ms",
        })
        try:
            data = await _fetch_json(session, weather_url)
        except Exception as exc:
            return f"天气数据获取失败：{exc}"

    cur: dict[str, Any] = data.get("current", {})
    daily: dict[str, Any] = data.get("daily", {})

    # 解析当前天气
    wcode = int(cur.get("weather_code") or 0)
    desc = _WMO_CODES.get(wcode, f"未知天气({wcode})")
    temp = cur.get("temperature_2m", "?")
    feels = cur.get("apparent_temperature", "?")
    humidity = cur.get("relative_humidity_2m", "?")
    wind_spd = cur.get("wind_speed_10m", "?")
    wind_dir_deg = float(cur.get("wind_direction_10m") or 0)
    precip = float(cur.get("precipitation") or 0)

    lines = [
        f"{display} 实时天气",
        f"天气：{desc}",
        f"气温：{temp}°C（体感 {feels}°C）",
        f"湿度：{humidity}%  {_wind_dir(wind_dir_deg)}风 {wind_spd}m/s",
    ]
    if precip > 0:
        lines.append(f"当前降水：{precip}mm")

    # 解析未来 3 天预报（index 0 为今天，跳过）
    dates: list[str] = daily.get("time", [])
    codes: list[int] = daily.get("weather_code", [])
    t_max: list[float] = daily.get("temperature_2m_max", [])
    t_min: list[float] = daily.get("temperature_2m_min", [])
    p_sum: list[float] = daily.get("precipitation_sum", [])

    if len(dates) > 1:
        lines.append("")
        lines.append("未来 3 天：")
        for i in range(1, min(4, len(dates))):
            dcode = int(codes[i]) if i < len(codes) else 0
            ddesc = _WMO_CODES.get(dcode, str(dcode))
            dmax = t_max[i] if i < len(t_max) else "?"
            dmin = t_min[i] if i < len(t_min) else "?"
            dp = float(p_sum[i]) if i < len(p_sum) else 0
            row = f"  {dates[i]}  {ddesc}  {dmin}~{dmax}°C"
            if dp > 0:
                row += f"  降水{dp}mm"
            lines.append(row)

    return "\n".join(lines)


async def fetch_weather_brief(location: str) -> str:
    """查询指定城市/地点的天气，返回一句话摘要。

    格式：晴，25°C（体感27°C），湿度60%，东南风3m/s
    用于注入到日程上下文。任何失败都返回空串，调用方静默忽略即可。
    """
    headers = {"User-Agent": "MaiBot-WeatherTool/1.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            geo_url = _GEOCODING_URL + "?" + urllib.parse.urlencode({
                "name": location,
                "count": 1,
                "language": "zh",
                "format": "json",
            })
            geo = await _fetch_json(session, geo_url)
            results = geo.get("results")
            if not results:
                return ""

            place = results[0]
            lat = place["latitude"]
            lon = place["longitude"]

            weather_url = _WEATHER_URL + "?" + urllib.parse.urlencode({
                "latitude": lat,
                "longitude": lon,
                "current": ",".join([
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "weather_code",
                    "wind_speed_10m",
                    "wind_direction_10m",
                ]),
                "timezone": "auto",
                "wind_speed_unit": "ms",
            })
            data = await _fetch_json(session, weather_url)
    except Exception:
        return ""

    cur = data.get("current", {})
    wcode = int(cur.get("weather_code") or 0)
    desc = _WMO_CODES.get(wcode, f"未知天气({wcode})")
    temp = cur.get("temperature_2m", "?")
    feels = cur.get("apparent_temperature", "?")
    humidity = cur.get("relative_humidity_2m", "?")
    wind_spd = cur.get("wind_speed_10m", "?")
    wind_dir_deg = float(cur.get("wind_direction_10m") or 0)

    return f"{desc}，{temp}°C（体感{feels}°C），湿度{humidity}%，{_wind_dir(wind_dir_deg)}风{wind_spd}m/s"
