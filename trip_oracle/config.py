"""目标目的地配置：长春（CCT）出发，高铁/动车 2~3 小时圈。

字段说明：
  name    展示名
  station 12306 车站中文名（用于自动解析电报码）
  lat/lon 目的地的经纬度（取天气用）
  days    建议游玩天数
  tags    偏好标签（自然/城市/网红/演出），供推荐排序用
  stay    经济型快捷酒店平日价（元/晚，2026-11 校准；节假日另上浮，预算为下限估计）
"""

ORIGIN_NAME = "长春"
ORIGIN_STATION = "长春"
ORIGIN_CODE = "CCT"
ORIGIN_LATLON = (43.88, 125.32)

DESTINATIONS = [
    {"name": "吉林市", "station": "吉林", "lat": 43.84, "lon": 126.55,
     "days": 1, "tags": ["自然", "城市"], "stay": 150},
    {"name": "四平", "station": "四平", "lat": 43.17, "lon": 124.35,
     "days": 1, "tags": ["城市"], "stay": 120},
    {"name": "沈阳", "station": "沈阳", "lat": 41.80, "lon": 123.43,
     "days": 2, "tags": ["城市", "演出", "网红"], "stay": 190},
    {"name": "哈尔滨", "station": "哈尔滨", "lat": 45.80, "lon": 126.53,
     "days": 2, "tags": ["城市", "网红"], "stay": 210},
    {"name": "延吉", "station": "延吉", "lat": 42.89, "lon": 129.51,
     "days": 2, "tags": ["网红", "美食"], "stay": 180},
    {"name": "长白山", "station": "长白山", "lat": 42.02, "lon": 128.06,
     "days": 3, "tags": ["自然"], "stay": 260},
]

# 采集范围：从今天起往后 N 天（含今天）
FORECAST_DAYS = 8

# 余票字段在 12306 返回行中的位置（按 59 段布局）
SEAT_FIELDS = {
    "swz": 32,   # 商务座
    "tz": 25,    # 特等座
    "zy": 31,    # 一等座
    "ze": 30,    # 二等座
    "rz": 24,    # 软座
    "rw": 23,    # 软卧
    "yw": 28,    # 硬卧
    "yz": 29,    # 硬座
    "wz": 26,    # 无座
}
