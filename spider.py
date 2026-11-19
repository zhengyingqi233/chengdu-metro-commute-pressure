# -*- coding: utf-8 -*-
"""成都地铁线网爬虫。

作业要求从 TransitFeeds 抓取 GTFS。TransitFeeds（OpenMobilityData）已于
2025 年 12 月停止更新，历史数据迁到 Mobility Database，且这两处目录目前都没有
成都地铁 GTFS 包。本脚本的顺序是：

1. 联网检索 TransitFeeds 接口和 Mobility Database 目录，寻找成都 GTFS 压缩包；
2. 若找到压缩包，下载并解析 stops / routes / trips / stop_times；
3. 若目录中没有成都数据包，则请求高德地图公开地铁线网 JSON（实时接口，不是写死的站点表），
   转成与 GTFS 对应的 CSV，供可视化页面使用。

所有网络请求都带超时和有限次重试，失败会记录原因并继续尝试下一条来源。
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 项目根目录下的 data 文件夹，CSV 都写到这里。
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# TransitFeeds 旧接口。不带有效 key 时通常会 401/403，脚本只把它当作“先检索”的一步。
TRANSITFEEDS_API = "https://api.transitfeeds.com/v1/getFeeds"
# Mobility Database 是 TransitFeeds 的后续目录，CSV 里含各城市最新 GTFS 下载地址。
MOBILITY_CATALOG_URL = "https://files.mobilitydatabase.org/feeds_v2.csv"
# 高德地铁城市列表。用它查出“成都市”的行政区划代码和拼音，避免把城市编号写死在解析逻辑里。
AMAP_CITY_LIST_URL = "https://map.amap.com/service/subway?srhdata=citylist.json"
# 单个城市的线网 JSON。adcode 与拼音来自上面的城市列表。
AMAP_CITY_URL = "https://map.amap.com/service/subway?srhdata={adcode}_drw_{spell}.json"

REQUEST_TIMEOUT = (10, 40)  # (连接超时秒, 读取超时秒)
MAX_RETRIES = 3
TARGET_CITY_NAMES = ("成都市", "成都")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chengdu_metro_spider")


def build_session() -> requests.Session:
    """创建带重试的会话。只对网关类错误重试，4xx 直接交给上层判断。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "chengdu-metro-course/1.0 "
                "(student data-visualization assignment; contact: local)"
            ),
            "Accept": "application/json,text/csv,application/zip,*/*",
        }
    )
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def http_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """发起 GET。超时、连接重置、HTTP 错误都会抛出，由调用方决定是否换数据源。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    logger.info("请求 %s", url)
    try:
        response = session.get(url, **kwargs)
    except requests.Timeout as exc:
        raise TimeoutError(f"请求超时: {url}") from exc
    except requests.RequestException as exc:
        raise ConnectionError(f"网络请求失败: {url} ({exc})") from exc
    if response.status_code >= 400:
        raise ConnectionError(f"HTTP {response.status_code}: {url}")
    return response


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两点球面距离，单位米。用于站间距和覆盖缺口。"""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(lng: float, lat: float) -> float:
    ret = (
        -100.0
        + 2.0 * lng
        + 3.0 * lat
        + 0.2 * lat * lat
        + 0.1 * lng * lat
        + 0.2 * math.sqrt(abs(lng))
    )
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(lng: float, lat: float) -> float:
    ret = (
        300.0
        + lng
        + 2.0 * lat
        + 0.1 * lng * lng
        + 0.1 * lng * lat
        + 0.1 * math.sqrt(abs(lng))
    )
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """高德坐标（GCJ-02）转地图底图使用的 WGS84。

    公开地图瓦片按 WGS84 显示。若不转换，成都站点会相对道路偏移大约几十到几百米。
    """
    if _out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lng * 2 - (lng + d_lng), lat * 2 - (lat + d_lat)


def discover_transitfeeds_zip(session: requests.Session) -> str | None:
    """在 TransitFeeds 及其后续目录中查找成都 GTFS 压缩包地址。

    返回 zip 的 URL；目录里没有成都，或目录暂时访问失败时返回 None。
    """
    candidates: list[str] = []

    # 1) 旧 TransitFeeds API。公开文档要求 key，这里用检索词探测，失败不中断。
    try:
        response = http_get(
            session,
            TRANSITFEEDS_API,
            params={"descendants": 1, "limit": 50, "search": "chengdu"},
        )
        text = response.text
        candidates.extend(re.findall(r"https?://[^\"'\s>]+\.zip", text))
        if "chengdu" in text.lower() or "成都" in text:
            logger.info("TransitFeeds API 响应中出现成都相关字样，长度 %s", len(text))
    except (TimeoutError, ConnectionError, ValueError) as exc:
        logger.warning("TransitFeeds API 不可用，继续查后续目录: %s", exc)

    # 2) Mobility Database 全量目录。TransitFeeds 停更后，最新 GTFS 地址在这份 CSV 里。
    try:
        response = http_get(session, MOBILITY_CATALOG_URL)
        # 目录可能是 UTF-8 CSV，整表检索比重写解析规则更稳妥：列名会随版本变化。
        catalog = response.content.decode("utf-8", errors="replace")
        if "chengdu" not in catalog.lower() and "成都" not in catalog:
            logger.info("Mobility Database 目录中没有成都地铁 GTFS 记录")
        else:
            for row in catalog.splitlines():
                lowered = row.lower()
                if "chengdu" not in lowered and "成都" not in row:
                    continue
                candidates.extend(re.findall(r"https?://[^\"'\s,]+\.zip", row))
                logger.info("目录命中一行: %s", row[:240])
    except (TimeoutError, ConnectionError, ValueError) as exc:
        logger.warning("Mobility Database 目录暂时无法访问: %s", exc)

    # 去重并保留看起来像 GTFS 包的链接。
    unique = []
    for url in candidates:
        if url not in unique:
            unique.append(url)
    if unique:
        logger.info("找到 %s 个候选压缩包", len(unique))
        return unique[0]
    return None


def _read_gtfs_table(archive: zipfile.ZipFile, name: str) -> pd.DataFrame:
    """从 GTFS 压缩包读取一张表。文件不存在时返回空表。"""
    names = {item.lower(): item for item in archive.namelist()}
    key = name.lower()
    if key not in names:
        logger.warning("GTFS 包中没有 %s", name)
        return pd.DataFrame()
    raw = archive.read(names[key])
    return pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig")


def parse_gtfs_zip(content: bytes, source_url: str) -> dict[str, pd.DataFrame]:
    """把 GTFS zip 解析成页面使用的几张表。只保留地铁（route_type=1）或名称含地铁的线路。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"下载内容不是有效的 GTFS zip: {source_url}") from exc

    routes = _read_gtfs_table(archive, "routes.txt")
    stops = _read_gtfs_table(archive, "stops.txt")
    trips = _read_gtfs_table(archive, "trips.txt")
    stop_times = _read_gtfs_table(archive, "stop_times.txt")
    if routes.empty or stops.empty or trips.empty or stop_times.empty:
        raise ValueError("GTFS 包缺少 routes/stops/trips/stop_times 中的必要文件")

    route_type = routes.get("route_type", pd.Series(dtype=str)).fillna("")
    route_name = (
        routes.get("route_long_name", pd.Series("", index=routes.index)).fillna("")
        + routes.get("route_short_name", pd.Series("", index=routes.index)).fillna("")
    )
    metro_mask = route_type.eq("1") | route_name.str.contains("地铁|metro|subway", case=False, regex=True)
    metro_routes = routes.loc[metro_mask].copy()
    if metro_routes.empty:
        raise ValueError("压缩包里没有可识别的地铁线路")

    metro_ids = set(metro_routes["route_id"])
    metro_trips = trips[trips["route_id"].isin(metro_ids)].copy()
    # 每条线路每个方向只保留一趟，避免时刻表重复把站间距算很多遍。
    metro_trips = metro_trips.sort_values(["route_id", "direction_id", "trip_id"])
    metro_trips = metro_trips.drop_duplicates(["route_id", "direction_id"], keep="first")
    trip_ids = set(metro_trips["trip_id"])
    times = stop_times[stop_times["trip_id"].isin(trip_ids)].copy()
    times["stop_sequence"] = pd.to_numeric(times["stop_sequence"], errors="coerce")
    times = times.dropna(subset=["stop_sequence"]).sort_values(["trip_id", "stop_sequence"])

    used_stops = stops[stops["stop_id"].isin(set(times["stop_id"]))].copy()
    used_stops["stop_lat"] = pd.to_numeric(used_stops["stop_lat"], errors="coerce")
    used_stops["stop_lon"] = pd.to_numeric(used_stops["stop_lon"], errors="coerce")
    used_stops = used_stops.dropna(subset=["stop_lat", "stop_lon"])

    routes_out = pd.DataFrame(
        {
            "route_id": metro_routes["route_id"],
            "route_short_name": metro_routes.get("route_short_name", metro_routes["route_id"]),
            "route_long_name": metro_routes.get("route_long_name", metro_routes["route_id"]),
            "route_color": metro_routes.get("route_color", "1F4E79").fillna("1F4E79").str.replace("#", "", regex=False),
            "route_type": metro_routes.get("route_type", "1"),
            "route_sort": range(1, len(metro_routes) + 1),
        }
    )
    stop_count = (
        times.merge(metro_trips[["trip_id", "route_id"]], on="trip_id")
        .groupby("route_id")["stop_id"]
        .nunique()
    )
    routes_out["stop_count"] = routes_out["route_id"].map(stop_count).fillna(0).astype(int)

    trip_route = metro_trips[["trip_id", "route_id"]].merge(
        routes_out[["route_id", "route_short_name"]],
        on="route_id",
        how="left",
    )
    stop_routes = times.merge(trip_route, on="trip_id")
    line_of_stop = stop_routes.groupby("stop_id")["route_id"].agg(lambda s: "|".join(sorted(set(map(str, s)))))
    name_of_stop = stop_routes.groupby("stop_id")["route_short_name"].agg(
        lambda s: "|".join(sorted(set(map(str, s))))
    )
    stops_out = pd.DataFrame(
        {
            "stop_id": used_stops["stop_id"],
            "stop_name": used_stops["stop_name"],
            "stop_lat": used_stops["stop_lat"],
            "stop_lon": used_stops["stop_lon"],
            "lines": used_stops["stop_id"].map(line_of_stop).fillna(""),
            "line_names": used_stops["stop_id"].map(name_of_stop).fillna(""),
        }
    )
    # 换乘按线路名称判断。同一条线的两个分支不算换乘。
    name_lines = stops_out.groupby("stop_name")["line_names"].agg(
        lambda series: {item for text in series for item in str(text).split("|") if item}
    )
    stops_out["is_transfer"] = stops_out["stop_name"].map(lambda name: int(len(name_lines.get(name, ())) >= 2))

    times = times.merge(
        metro_trips[["trip_id", "route_id", "direction_id"]],
        on="trip_id",
        how="left",
    )
    times = times.merge(
        routes_out[["route_id", "route_short_name", "route_color"]],
        on="route_id",
        how="left",
    )
    times = times.merge(
        stops_out[["stop_id", "stop_name", "stop_lat", "stop_lon"]],
        on="stop_id",
        how="left",
    )
    stop_times_out = times[
        [
            "trip_id",
            "route_id",
            "route_short_name",
            "route_color",
            "direction_id",
            "stop_sequence",
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon",
        ]
    ].dropna(subset=["stop_lat", "stop_lon"])

    segments = build_segments(stop_times_out)
    feed_info = pd.DataFrame(
        [
            {
                "source": "TransitFeeds/Mobility Database GTFS",
                "source_url": source_url,
                "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "coord_system": "GTFS 原始坐标（通常为 WGS84）",
                "note": "从在线 GTFS 压缩包解析，未使用本地静态站点表",
            }
        ]
    )
    return {
        "stops": stops_out,
        "routes": routes_out,
        "stop_times": stop_times_out,
        "segments": segments,
        "feed_info": feed_info,
    }


def fetch_amap_network(session: requests.Session) -> tuple[dict, str]:
    """从高德公开地铁接口抓取成都市线网。城市代码从城市列表接口动态取出。"""
    list_response = http_get(
        session,
        AMAP_CITY_LIST_URL,
        headers={"Referer": "https://map.amap.com/subway/index.html"},
    )
    try:
        city_list = list_response.json().get("citylist", [])
    except ValueError as exc:
        raise ValueError("城市列表不是合法 JSON") from exc

    chosen = None
    for city in city_list:
        if city.get("cityname") in TARGET_CITY_NAMES or city.get("spell") == "chengdu":
            chosen = city
            break
    if not chosen:
        raise LookupError("城市列表中没有成都市，无法继续抓取线网")

    adcode = str(chosen["adcode"])
    spell = str(chosen["spell"])
    url = AMAP_CITY_URL.format(adcode=adcode, spell=spell)
    # 接口偶尔返回空包，短暂停顿后重试一次，避免课堂演示时偶发失败。
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            response = http_get(
                session,
                url,
                headers={"Referer": "https://map.amap.com/subway/index.html"},
            )
            payload = response.json()
            if not payload.get("l"):
                raise ValueError("线网 JSON 中没有线路数组 l")
            return payload, url
        except (TimeoutError, ConnectionError, ValueError) as exc:
            last_error = exc
            logger.warning("第 %s 次抓取线网失败: %s", attempt, exc)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"高德线网接口抓取失败: {last_error}")


def parse_amap_payload(payload: dict, source_url: str) -> dict[str, pd.DataFrame]:
    """把高德线网 JSON 整理成 GTFS 风格的站点、线路、停站顺序和区间表。"""
    route_rows = []
    stop_rows = []
    time_rows = []

    for sort_index, line in enumerate(payload.get("l", []), start=1):
        stations = line.get("st") or []
        if len(stations) < 2:
            logger.warning("线路 %s 站点数不足，已跳过", line.get("ln"))
            continue
        route_id = str(line.get("ls") or f"line-{sort_index}")
        short_name = str(line.get("ln") or route_id)
        long_name = str(line.get("kn") or short_name)
        branch = str(line.get("la") or "").strip()
        if branch:
            long_name = f"{long_name} {branch}".strip()
        color = str(line.get("cl") or "1F4E79").replace("#", "")
        route_rows.append(
            {
                "route_id": route_id,
                "route_short_name": short_name,
                "route_long_name": long_name,
                "route_color": color,
                "route_type": "1",  # GTFS：1 表示地铁
                "route_sort": sort_index,
                "stop_count": len(stations),
            }
        )

        parsed_stops = []
        for sequence, station in enumerate(stations, start=1):
            sl = str(station.get("sl") or "")
            if "," not in sl:
                logger.warning("站点 %s 缺少坐标，已跳过", station.get("n"))
                continue
            lng_text, lat_text = sl.split(",", 1)
            try:
                lng_gcj = float(lng_text)
                lat_gcj = float(lat_text)
            except ValueError:
                logger.warning("站点 %s 坐标无法解析: %s", station.get("n"), sl)
                continue
            lng, lat = gcj02_to_wgs84(lng_gcj, lat_gcj)
            parsed_stops.append(
                {
                    "stop_id": str(station.get("sid") or f"{route_id}-{sequence}"),
                    "stop_name": str(station.get("n") or "").strip(),
                    "stop_lat": round(lat, 6),
                    "stop_lon": round(lng, 6),
                    "transfer_flag": str(station.get("t") or "0"),
                    "sequence": sequence,
                }
            )

        # 上行、下行各生成一趟，方便和 GTFS 的 direction_id 对应。下行是站点顺序反过来。
        directions = {0: parsed_stops, 1: list(reversed(parsed_stops))}
        for direction_id, ordered in directions.items():
            trip_id = f"{route_id}-dir{direction_id}"
            for sequence, stop in enumerate(ordered, start=1):
                time_rows.append(
                    {
                        "trip_id": trip_id,
                        "route_id": route_id,
                        "route_short_name": short_name,
                        "route_color": color,
                        "direction_id": direction_id,
                        "stop_sequence": sequence,
                        "stop_id": stop["stop_id"] if direction_id == 0 else f"{stop['stop_id']}-rev",
                        "stop_name": stop["stop_name"],
                        "stop_lat": stop["stop_lat"],
                        "stop_lon": stop["stop_lon"],
                    }
                )
                if direction_id == 0:
                    stop_rows.append(
                        {
                            "stop_id": stop["stop_id"],
                            "stop_name": stop["stop_name"],
                            "stop_lat": stop["stop_lat"],
                            "stop_lon": stop["stop_lon"],
                            "route_id": route_id,
                            "route_short_name": short_name,
                            "transfer_flag": stop["transfer_flag"],
                        }
                    )

    if not route_rows or not stop_rows:
        raise ValueError("线网 JSON 解析后没有可用站点")

    stops = pd.DataFrame(stop_rows)
    # lines 保存线路 id，供页面按线路筛选；line_names 保存“1号线”这类名称。
    # 1 号线有五根松、科学城两个分支，分支 id 不同但名称相同，不能当成换乘。
    id_sets = stops.groupby("stop_name")["route_id"].agg(lambda s: sorted(set(map(str, s))))
    name_sets = stops.groupby("stop_name")["route_short_name"].agg(lambda s: sorted(set(s)))
    flag_sets = stops.groupby("stop_name")["transfer_flag"].agg(lambda s: "1" in set(map(str, s)))
    stops["lines"] = stops["stop_name"].map(lambda name: "|".join(id_sets.get(name, [])))
    stops["line_names"] = stops["stop_name"].map(lambda name: "|".join(name_sets.get(name, [])))
    stops["is_transfer"] = stops["stop_name"].map(
        lambda name: int(len(name_sets.get(name, [])) >= 2 or bool(flag_sets.get(name, False)))
    )
    stops = stops.drop_duplicates("stop_id")
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "lines", "line_names", "is_transfer"]]

    routes = pd.DataFrame(route_rows)
    stop_times = pd.DataFrame(time_rows)
    segments = build_segments(stop_times)
    feed_info = pd.DataFrame(
        [
            {
                "source": "高德地图公开地铁线网接口（TransitFeeds 无成都 GTFS 时的在线回退）",
                "source_url": source_url,
                "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "coord_system": "已由 GCJ-02 转换为 WGS84",
                "note": (
                    "先检索了 TransitFeeds API 与 Mobility Database 目录，未获得成都地铁 GTFS 包。"
                    "当前 CSV 由上述 URL 实时返回的 JSON 解析得到，不是手写静态数据。"
                    "站间距反映线网覆盖结构，不是官方客流。"
                ),
            }
        ]
    )
    return {
        "stops": stops,
        "routes": routes,
        "stop_times": stop_times,
        "segments": segments,
        "feed_info": feed_info,
    }


def build_segments(stop_times: pd.DataFrame) -> pd.DataFrame:
    """按每一趟的停站顺序，计算相邻站距离。页面用 direction_id=0 画线和找缺口。"""
    rows = []
    ordered = stop_times.sort_values(["trip_id", "stop_sequence"])
    for trip_id, group in ordered.groupby("trip_id", sort=False):
        records = group.to_dict("records")
        for previous, current in zip(records, records[1:]):
            distance = haversine_m(
                float(previous["stop_lon"]),
                float(previous["stop_lat"]),
                float(current["stop_lon"]),
                float(current["stop_lat"]),
            )
            rows.append(
                {
                    "trip_id": trip_id,
                    "route_id": previous["route_id"],
                    "route_short_name": previous["route_short_name"],
                    "route_color": previous["route_color"],
                    "direction_id": previous["direction_id"],
                    "from_stop": previous["stop_name"],
                    "to_stop": current["stop_name"],
                    "from_lon": previous["stop_lon"],
                    "from_lat": previous["stop_lat"],
                    "to_lon": current["stop_lon"],
                    "to_lat": current["stop_lat"],
                    "distance_m": round(distance, 1),
                }
            )
    return pd.DataFrame(rows)


def save_tables(tables: dict[str, pd.DataFrame]) -> None:
    """把解析结果写成 UTF-8 BOM 的 CSV，Excel 打开中文不会乱码。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        path = DATA_DIR / f"{name}.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
        logger.info("写出 %s（%s 行）", path.name, len(frame))


def download_and_parse_gtfs(session: requests.Session, zip_url: str) -> dict[str, pd.DataFrame]:
    response = http_get(session, zip_url)
    if len(response.content) < 100:
        raise ValueError(f"压缩包过小，可能不是有效数据: {zip_url}")
    return parse_gtfs_zip(response.content, zip_url)


def main() -> int:
    session = build_session()
    tables = None

    try:
        zip_url = discover_transitfeeds_zip(session)
    except Exception as exc:  # 检索阶段的意外错误不应阻止回退数据源
        logger.warning("检索 GTFS 目录时出现未预期错误: %s", exc)
        zip_url = None

    if zip_url:
        try:
            tables = download_and_parse_gtfs(session, zip_url)
            logger.info("已使用 GTFS 压缩包: %s", zip_url)
        except (TimeoutError, ConnectionError, ValueError, zipfile.BadZipFile) as exc:
            logger.warning("GTFS 压缩包下载或解析失败，改用高德线网接口: %s", exc)

    if tables is None:
        try:
            payload, source_url = fetch_amap_network(session)
            tables = parse_amap_payload(payload, source_url)
            logger.info("已使用高德线网接口: %s", source_url)
        except (TimeoutError, ConnectionError, ValueError, LookupError, RuntimeError) as exc:
            logger.error("全部在线数据源失败: %s", exc)
            return 1

    try:
        save_tables(tables)
    except OSError as exc:
        logger.error("写入 data 目录失败: %s", exc)
        return 1

    logger.info(
        "完成。线路 %s 条，站点记录 %s 条，区间 %s 条。",
        len(tables["routes"]),
        len(tables["stops"]),
        len(tables["segments"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
