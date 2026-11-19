# -*- coding: utf-8 -*-
"""成都地铁通勤压力可视化页面。

读取 spider.py 导出的 CSV，在课堂上演示三件事：
1. 线网和站点落在地图上，可按线路筛选；
2. 站点规模、换乘站、平均站间距等基础指标；
3. 相邻站过远的区段，作为通勤覆盖缺口。

这里的“压力”指线网结构：换乘站衔接了更多线路，远郊大站距意味着步行和接驳更长。
数据里没有官方客流，页面不把站间距说成拥挤度。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 天府广场是线网几何中心的参照站。坐标从抓取结果里读，不在代码里写死经纬度。
CENTER_STOP_NAME = "天府广场"
# 课堂演示的默认缺口阈值：相邻站超过 2 公里，步行接驳明显变长。
DEFAULT_GAP_M = 2000


def hex_to_rgb(color: str) -> list[int]:
    """线路颜色是 6 位十六进制。非法颜色退回深蓝色，避免地图图层报错。"""
    text = str(color or "").replace("#", "").strip()
    if len(text) != 6:
        return [31, 78, 121]
    try:
        return [int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)]
    except ValueError:
        return [31, 78, 121]


@st.cache_data(show_spinner="正在读取线网数据…")
def load_tables() -> dict[str, pd.DataFrame]:
    """读取 data 目录中的 CSV。文件缺失时给出明确提示，而不是抛出长堆栈。"""
    required = ["stops.csv", "routes.csv", "stop_times.csv", "segments.csv", "feed_info.csv"]
    missing = [name for name in required if not (DATA_DIR / name).exists()]
    if missing:
        raise FileNotFoundError("缺少 " + "、".join(missing) + "。请先在项目目录运行 python spider.py")

    tables = {}
    for name in required:
        tables[name.replace(".csv", "")] = pd.read_csv(DATA_DIR / name, encoding="utf-8-sig")
    return tables


def station_points(stops: pd.DataFrame, selected_route_ids: set[str]) -> pd.DataFrame:
    """把同一站名在不同线路上的记录收成一个点，便于地图点击。"""
    name_column = "line_names" if "line_names" in stops.columns else "lines"
    rows = []
    for name, group in stops.groupby("stop_name"):
        route_ids = []
        for raw in group["lines"].fillna(""):
            route_ids.extend([item for item in str(raw).split("|") if item])
        route_ids = sorted(set(route_ids))
        if selected_route_ids and not (set(route_ids) & selected_route_ids):
            continue
        line_names = []
        for raw in group[name_column].fillna(""):
            line_names.extend([item for item in str(raw).split("|") if item])
        # 按线路名称计数。同一条线的支线 id 不同，不应显示成两线换乘。
        line_count = len(set(line_names)) if name_column == "line_names" else len(route_ids)
        # 换乘站用橙色，普通站用蓝色。线数越多，点越大。
        if line_count >= 3:
            color = [196, 61, 45, 220]
        elif line_count == 2:
            color = [232, 122, 42, 210]
        else:
            color = [36, 99, 166, 190]
        rows.append(
            {
                "stop_name": name,
                "lon": float(group["stop_lon"].mean()),
                "lat": float(group["stop_lat"].mean()),
                "line_count": line_count,
                "is_transfer": int(line_count >= 2),
                "radius": 70 + line_count * 35,
                "color": color,
            }
        )
    return pd.DataFrame(rows)


def filter_direction(frame: pd.DataFrame, selected_route_ids: set[str]) -> pd.DataFrame:
    """地图和缺口只看每个线路的一个方向，避免上下行把同一段画两次。"""
    data = frame[frame["direction_id"].astype(str) == "0"].copy()
    if selected_route_ids:
        data = data[data["route_id"].astype(str).isin(selected_route_ids)]
    return data


def distance_to_center_km(points: pd.DataFrame, center_lon: float, center_lat: float) -> pd.Series:
    """站点到参照站的近似距离（公里）。成都范围不大，用平面近似足够分圈层。"""
    # 纬度 1 度约 111 公里；经度在北纬 30 度约 96 公里。
    d_lat = (points["lat"] - center_lat) * 111.0
    d_lon = (points["lon"] - center_lon) * 96.0
    return (d_lat**2 + d_lon**2) ** 0.5


def ring_label(distance_km: float) -> str:
    if distance_km < 8:
        return "核心区（<8公里）"
    if distance_km < 20:
        return "近郊（8–20公里）"
    return "远郊（≥20公里）"


def main() -> None:
    st.set_page_config(
        page_title="成都地铁通勤压力可视化",
        page_icon="🚇",
        layout="wide",
    )
    st.title("成都地铁通勤压力可视化分析")
    st.caption("数据分析可视化课程作业 · 线网覆盖与换乘衔接，不是官方客流热力")

    try:
        tables = load_tables()
    except FileNotFoundError as exc:
        st.error(str(exc))
        st.stop()
    except (OSError, pd.errors.ParserError, UnicodeError) as exc:
        st.error(f"CSV 读取失败：{exc}")
        st.stop()

    stops = tables["stops"]
    routes = tables["routes"].sort_values("route_sort")
    segments = tables["segments"]
    feed = tables["feed_info"].iloc[0]

    with st.sidebar:
        st.header("筛选")
        route_labels = {
            str(row.route_id): f"{row.route_short_name} · {row.route_long_name}"
            for row in routes.itertuples(index=False)
        }
        selected_labels = st.multiselect(
            "线路",
            options=list(route_labels.values()),
            default=list(route_labels.values()),
        )
        label_to_id = {label: route_id for route_id, label in route_labels.items()}
        selected_route_ids = {label_to_id[label] for label in selected_labels}
        gap_m = st.slider(
            "覆盖缺口阈值（米）",
            min_value=800,
            max_value=5000,
            value=DEFAULT_GAP_M,
            step=100,
            help="相邻两站球面距离超过该值时，记为通勤覆盖缺口。",
        )
        st.divider()
        st.markdown("**数据来源**")
        st.write(str(feed.get("source", "")))
        st.caption(str(feed.get("fetched_at", "")))
        st.caption(str(feed.get("note", "")))

    if not selected_route_ids:
        st.warning("请至少选择一条线路。")
        st.stop()

    points = station_points(stops, selected_route_ids)
    one_way = filter_direction(segments, selected_route_ids)
    if points.empty or one_way.empty:
        st.warning("当前筛选下没有可展示的站点。")
        st.stop()

    one_way = one_way.copy()
    one_way["is_gap"] = one_way["distance_m"] >= gap_m
    gaps = one_way[one_way["is_gap"]].sort_values("distance_m", ascending=False)

    center_match = points[points["stop_name"] == CENTER_STOP_NAME]
    if center_match.empty:
        center_lon = float(points["lon"].median())
        center_lat = float(points["lat"].median())
        center_note = "数据中没有天府广场，圈层改以站点坐标中位数为参照"
    else:
        center_lon = float(center_match.iloc[0]["lon"])
        center_lat = float(center_match.iloc[0]["lat"])
        center_note = f"圈层以{CENTER_STOP_NAME}为参照"

    points["distance_km"] = distance_to_center_km(points, center_lon, center_lat).round(2)
    points["ring"] = points["distance_km"].map(ring_label)

    transfer_count = int(points["is_transfer"].sum())
    mean_gap = float(one_way["distance_m"].mean())
    max_gap = float(one_way["distance_m"].max())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("线路", f"{routes['route_id'].astype(str).isin(selected_route_ids).sum()} 条")
    c2.metric("站点（去重）", f"{len(points)} 座")
    c3.metric("换乘站", f"{transfer_count} 座")
    c4.metric("平均站间距", f"{mean_gap / 1000:.2f} 公里")
    c5.metric("覆盖缺口段", f"{len(gaps)} 段")

    st.markdown("### 线网地图")
    st.caption(
        "蓝点是普通站，橙点是两线换乘，红点是三线及以上换乘。"
        "线段按站间距着色：短间距偏绿，超过阈值偏红。"
        + center_note
    )

    path_records = []
    for row in one_way.itertuples(index=False):
        # 超过阈值的区间用红色加粗，其余区间用线路色。
        if row.distance_m >= gap_m:
            color = [214, 69, 69]
            width = 90
        else:
            color = hex_to_rgb(row.route_color)
            width = 45
        path_records.append(
            {
                "path": [[row.from_lon, row.from_lat], [row.to_lon, row.to_lat]],
                "color": color,
                "width": width,
                "tooltip": f"{row.route_short_name} {row.from_stop} → {row.to_stop}：{row.distance_m:.0f} 米",
            }
        )
    paths = pd.DataFrame(path_records)
    point_layer = pdk.Layer(
        "ScatterplotLayer",
        data=points,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        radius_min_pixels=4,
        radius_max_pixels=16,
        pickable=True,
    )
    path_layer = pdk.Layer(
        "PathLayer",
        data=paths,
        get_path="path",
        get_color="color",
        get_width="width",
        width_min_pixels=2,
        pickable=True,
    )
    view = pdk.ViewState(
        latitude=float(points["lat"].mean()),
        longitude=float(points["lon"].mean()),
        zoom=10,
        pitch=0,
    )
    deck = pdk.Deck(
        layers=[path_layer, point_layer],
        initial_view_state=view,
        # Carto 底图不需要 Mapbox token，课堂电脑上也能打开。
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip={"text": "{stop_name}\n衔接线路数：{line_count}\n距参照点：{distance_km} 公里"},
    )
    st.pydeck_chart(deck, use_container_width=True)

    left, right = st.columns(2)
    spacing = (
        one_way.groupby("route_short_name", sort=False)["distance_m"]
        .mean()
        .div(1000)
        .round(2)
        .rename("平均站间距（公里）")
    )
    counts = (
        routes[routes["route_id"].astype(str).isin(selected_route_ids)]
        .set_index("route_short_name")["stop_count"]
        .rename("站点数")
    )
    with left:
        st.markdown("### 各线路平均站间距")
        st.bar_chart(spacing)
        st.caption("平均站间距越大，这条线两端的步行和公交接驳越长。")
    with right:
        st.markdown("### 各线路站点规模")
        st.bar_chart(counts)
        st.caption("站点多不代表覆盖均匀，要和左图的站间距一起看。")

    st.markdown("### 通勤覆盖缺口")
    st.write(
        f"当前阈值 **{gap_m} 米**。筛选范围内共有 **{len(gaps)}** 段相邻站超过该距离，"
        f"最长一段 **{max_gap / 1000:.2f} 公里**。"
        "这些区段是线网几何上的覆盖偏弱处：住在两站之间的人，走到车站的时间更长。"
    )
    if gaps.empty:
        st.success("按当前阈值，筛选线路上没有超距区段。可以把阈值调低再看。")
    else:
        show_gaps = gaps[
            ["route_short_name", "from_stop", "to_stop", "distance_m"]
        ].rename(
            columns={
                "route_short_name": "线路",
                "from_stop": "起点站",
                "to_stop": "终点站",
                "distance_m": "站间距（米）",
            }
        )
        st.dataframe(show_gaps, use_container_width=True, hide_index=True)

    st.markdown("### 圈层覆盖")
    st.caption(center_note + "。远郊若同时站间距偏大，通勤对地铁的依赖会更依赖接驳。")
    ring_order = ["核心区（<8公里）", "近郊（8–20公里）", "远郊（≥20公里）"]
    ring_summary = (
        points.groupby("ring")
        .agg(站点数=("stop_name", "count"), 换乘站=("is_transfer", "sum"), 平均距中心公里=("distance_km", "mean"))
        .reindex(ring_order)
        .dropna(how="all")
    )
    ring_summary["平均距中心公里"] = ring_summary["平均距中心公里"].round(1)
    ring_summary["换乘站"] = ring_summary["换乘站"].astype(int)
    st.dataframe(ring_summary, use_container_width=True)

    hub = points[points["is_transfer"] == 1].sort_values(
        ["line_count", "stop_name"], ascending=[False, True]
    )
    st.markdown("### 换乘衔接压力")
    st.caption("衔接线路越多，早晚高峰越容易在站厅和换乘通道汇合。这里只统计线网结构，没有进站量。")
    st.dataframe(
        hub[["stop_name", "line_count", "ring", "distance_km"]].rename(
            columns={
                "stop_name": "换乘站",
                "line_count": "衔接线路数",
                "ring": "圈层",
                "distance_km": "距参照点（公里）",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("指标说明（演示时可展开）"):
        st.markdown(
            """
- **站点**：按站名去重。同一站在不同线路上的坐标取平均。
- **换乘站**：站名出现在两条及以上线路，或数据源标记为换乘。
- **站间距**：相邻站的球面距离，只计算每个线路的一个行车方向。
- **覆盖缺口**：站间距大于侧边栏阈值的区间。阈值可以在课堂上改，方便对比核心区和市域快线。
- **圈层**：以天府广场为参照，分核心、近郊、远郊。这是空间分组，不是行政区。
- **数据边界**：来自线网公开接口或 GTFS，描述车站在哪里、站与站有多远，不包含客流、限流和列车满载率。
            """
        )


if __name__ == "__main__":
    main()
