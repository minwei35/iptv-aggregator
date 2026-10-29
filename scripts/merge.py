#!/usr/bin/env python3
"""把多个 m3u 订阅源合并成一个，去重、可选探活，然后写出 out/playlist.m3u。

只用标准库，GitHub Actions 里不需要装任何依赖。

    python scripts/merge.py                # 合并 + 去重
    python scripts/merge.py --probe        # 再探一次活，剔掉连不上的
    python scripts/merge.py --probe --timeout 4

sources.txt 一行一个上游 URL；# 开头是注释。
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.txt"
OUT = ROOT / "out" / "playlist.m3u"
UA = "iptv-aggregator/1.0 (+https://github.com/)"

# 英文台名 → 中文台名。只翻译「确定能对上」的，查不到的原样保留，
# 免得把不认识的台改成错名字。键是大写、去掉画质/标记后的核心名。
NAME_ZH = {
    # 央视：必须用 EPG 里的写法（CCTV1 而不是 CCTV-1 综合），
    # 因为播放器是按「频道名 == EPG 的 display-name」精确匹配的，多一个字就匹配不上。
    "CCTV-1": "CCTV1",
    "CCTV-2": "CCTV2",
    "CCTV-3": "CCTV3",
    "CCTV-4": "CCTV4",
    "CCTV-5": "CCTV5",
    "CCTV-5+": "CCTV5+",
    "CCTV-6": "CCTV6",
    "CCTV-7": "CCTV7",
    "CCTV-8": "CCTV8",
    "CCTV-9": "CCTV9",
    "CCTV-10": "CCTV10",
    "CCTV-11": "CCTV11",
    "CCTV-12": "CCTV12",
    "CCTV-13": "CCTV13",
    "CCTV-14": "CCTV14",
    "CCTV-15": "CCTV15",
    "CCTV-16": "CCTV16",
    "CCTV-17": "CCTV17",
    "CCTV-4K": "CCTV4K",
    "CCTV+ 1": "CCTV+ 国际 1",
    "CCTV+ 2": "CCTV+ 国际 2",
    "CCTV-BILLIARDS": "CCTV 台球",
    "CCTV-HEALTH": "CCTV 卫生健康",
    "CCTV-GOLF & TENNIS": "CCTV 高尔夫网球",
    "CCTV-WEAPON & TECHNOLOGY": "CCTV 兵器科技",
    "CCTV-WOMEN'S FASHION": "CCTV 女性时尚",
    "CCTV-WORLD GEOGRAPHY": "CCTV 世界地理",
    "CCTV-CULTURE OF QUALITY": "CCTV 品质",
    "CCTV-NOSTALGIA THEATER": "CCTV 怀旧剧场",
    "CCTV-THE FIRST THEATER": "CCTV 第一剧场",
    "CCTV-STORM FOOTBALL": "CCTV 风云足球",
    "CCTV-STORM MUSIC": "CCTV 风云音乐",
    "CCTV-STORM THEATER": "CCTV 风云剧场",
    # 省级卫视 / 教育台 / 少儿
    "BEIJING SATELLITE TV": "北京卫视",
    "ANHUI TV": "安徽卫视",
    "BINGTUAN SATELLITE TV": "兵团卫视",
    "CETV-1": "中国教育 1 套",
    "CETV-2": "中国教育 2 套",
    "BRTV KAKU CHILDRENS CHANNEL": "卡酷少儿",
    "ZHEJIANG INTERNATIONAL CHANNEL": "浙江国际",
    "KANGBA TV": "康巴卫视",
    "NANCHANG NEWS & GENERALIST CHANNEL": "南昌新闻综合",
    "LANZHOU COMPREHENSIVE NEWS CHANNEL": "兰州新闻综合",
    "LANZHOU CULTURE & TOURISM CHANNEL": "兰州文旅",
    "SIPING TV": "四平电视台",
    "JILIN CITY CHANNEL": "吉林市频道",
    "JILIN LIFESTYLE CHANNEL": "吉林生活",
    "JILIN MOVIE CHANNEL": "吉林影视",
    "JILIN RURAL CHANNEL": "吉林乡村",
    "JIANGXI PUBLIC & AGRICULTURE CHANNEL": "江西公共农业",
    "ANSHUN COMPREHENSIVE NEWS CHANNEL": "安顺新闻综合",
    "CHAOZHOU PUBLIC CHANNEL": "潮州公共",
    "BAICHENG TV": "白城电视台",
    "ABN CHINA": "ABN 中国",
    "ANDO TV": "安多卫视",
    "ANGEL TV CHINESE": "天使卫视",
}

_MARK = re.compile(r"[\(\[](?:[^\)\]]*)[\)\]]")
_HD = re.compile(r"^(.*?)\s+(HD|SD|UHD|4K)$", re.IGNORECASE)


def localize(extinf: str) -> str:
    """把 #EXTINF 尾部的英文台名换成中文，查不到就原样返回。"""
    head, sep, name = extinf.rpartition(",")
    if not sep:
        return extinf
    markers = _MARK.findall(name)
    core = _MARK.sub("", name).strip()
    hd = ""
    m = _HD.match(core)
    if m:
        core, hd = m.group(1).strip(), " " + m.group(2).upper()
    zh = NAME_ZH.get(core.upper())
    if not zh:
        return extinf
    tail = (" " + " ".join(markers)) if markers else ""
    return f"{head},{zh}{hd}{tail}"

# 频道名里用来做去重键的噪声：画质/线路标记、括号注释
NOISE = re.compile(
    r"(\[(?:hd|fhd|uhd|sd|4k|8k|hevc|h\.?26[45]|多线路|备用|backup)\])"
    r"|(\((?:hd|fhd|uhd|sd|4k|8k|hevc|h\.?26[45]|多线路|备用|backup)\))",
    re.IGNORECASE,
)
QUALITY = re.compile(r"\b(hd|fhd|uhd|sd|4k|8k)\b", re.IGNORECASE)

# ---------------------------------------------------------------- 排序（国内 → 国外）
#
# 顺序按「用户翻台的直觉」排，而不是按上游的字母序：
#   0 央视（CCTV 按数字排）→ 1 省级卫视 → 2 其它国内台 → 3 港澳台 → 4 境外
# 依据是 iptv-org 给每个台带的 tvg-id，形如 CCTV1.cn@SD / PhoenixChinese.hk@SD，
# 点后面两位就是国家/地区码；没有 tvg-id 的按「来自中文源」处理，归国内。
_TVG_ID = re.compile(r'tvg-id="([^"]*)"')
_CCTV_NO = re.compile(r"^CCTV\s*-?\s*(\d+)", re.IGNORECASE)
_CCTV_PLUS = re.compile(r"^CCTV\+", re.IGNORECASE)
_CGTN = re.compile(r"^CGTN\b", re.IGNORECASE)
# 港澳台：tvg-id 国家码之外再用名字兜底（有些台 tvg-id 缺国家码）
_HMT_NAMES = (
    "凤凰", "无线", "TVB", "翡翠", "明珠", "东森", "中天", "民视", "台视", "华视",
    "中视", "公视", "三立", "年代", "非凡", "ViuTV", "HOY", "澳门", "澳视", "莲花",
    "香港", "台湾", "寰宇", "靖天", "龙华",
)
# 省级台：上游大多是英文名（Hunan TV / Jiangsu Satellite TV），靠省名关键词认，
# 再用 _SUB_CHANNEL 把「省台的细分频道」排除出去，它们算「其它国内台」。
_PROVINCE_WORDS = (
    "BEIJING", "TIANJIN", "HEBEI", "SHANXI", "NEI MONGOL", "INNER MONGOLIA",
    "LIAONING", "JILIN", "HEILONGJIANG", "SHANGHAI", "JIANGSU", "ZHEJIANG",
    "ANHUI", "FUJIAN", "JIANGXI", "SHANDONG", "HENAN", "HUBEI", "HUNAN",
    "GUANGDONG", "GUANGXI", "HAINAN", "CHONGQING", "SICHUAN", "GUIZHOU",
    "YUNNAN", "XIZANG", "TIBET", "SHAANXI", "GANSU", "QINGHAI", "NINGXIA",
    "XINJIANG", "SHENZHEN", "XIAMEN", "DRAGON TV",
)
_SUB_CHANNEL = (
    "CITY", "NEWS", "MOVIE", "VARIETY", "CHILDREN", "SPORTS", "PUBLIC",
    "ECONOMY", "LIFE", "EDUCATIONAL", "EDUCATION", "SCIENCE", "WOMEN",
    "AGRICULTURAL", "QILU", "TRAVEL", "CARTOON", "DRAMA", "DOCUMENTARY",
    "METRO", "TOURISM", "BUSINESS", "FINANCE", "ENTERTAINMENT", "SERIES",
    "THEATER", "FILM", "MUSIC", "LEGAL", "POLITICS", "STOCK", "COOKING",
    "FASHION", "GOLF",
)


def country_code(extinf: str) -> str:
    """从 tvg-id 里取国家/地区码（小写）；取不到返回空串。"""
    m = _TVG_ID.search(extinf)
    if not m:
        return ""
    core = m.group(1).split("@", 1)[0]
    return core.rsplit(".", 1)[1].lower() if "." in core else ""


def sort_key(item: tuple[str, str], index: int) -> tuple:
    """(大区, 组内主键, 组内次键, 原始序号) —— 稳定排序，同区之内保持上游顺序。"""
    extinf, _url = item
    name = channel_name(extinf)
    cc = country_code(extinf)
    up = name.upper()

    # 0 央视：CCTV 数字台按台号，CCTV+/CGTN 排后面，CCTV 付费频道（台球/风云剧场…）再后面
    if up.startswith("CCTV"):
        if _CCTV_PLUS.match(name):
            return (0, 50, 0, index)
        m = _CCTV_NO.match(name)
        if m:
            return (0, int(m.group(1)), 1 if "+" in name[:9] else 0, index)
        return (0, 40, 0, index)
    if _CGTN.match(name):
        return (0, 60, 0, index)

    # 1 省级卫视 / 省台主频道（国家码必须是 cn 或没标：广东卫视美洲版、天使卫视这类别算进来）
    if cc in ("", "cn"):
        if "卫视" in name or "SATELLITE" in up:
            return (1, 0, 0, index)
        if any(w in up for w in _PROVINCE_WORDS) and not any(w in up for w in _SUB_CHANNEL):
            return (1, 0, 0, index)

    # 3 港澳台
    if cc in ("hk", "tw", "mo") or any(k in name for k in _HMT_NAMES):
        return (3, 0, 0, index)

    # 2 其它国内台（含没标国家码的，来源本身就是中文清单）
    if cc in ("", "cn"):
        return (2, 0, 0, index)
    return (4, 0, 0, index)


def sort_items(items: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """返回 [(zone, EXTINF, URL)]，已按「国内 → 港澳台 → 境外」排好。"""
    decorated = [(sort_key(it, i), i, it) for i, it in enumerate(items)]
    decorated.sort(key=lambda t: (t[0], t[1]))
    return [(t[0][0], t[2][0], t[2][1]) for t in decorated]


# 「我的电视」是按 group-title 分组后**摊平**出翻台顺序的（M3uIptvParser: groupBy 保留首次出现顺序），
# 所以只排文件顺序没用 —— 组本身也要按这个次序出现，否则第一个出现的组会把它的台全挤到前面。
# 于是把 group-title 重写成 5 个区，组序天然就是国内 → 境外；顺带还能在「频道分组管理」里整组隐藏。
GROUP_NAMES = {0: "央视频道", 1: "省级卫视", 2: "地方频道", 3: "港澳台", 4: "境外频道"}
_GROUP_ATTR = re.compile(r'group-title="[^"]*"')


def set_group(extinf: str, group: str) -> str:
    """把 EXTINF 的 group-title 改成分区名（没有这个属性就补上）。"""
    if _GROUP_ATTR.search(extinf):
        return _GROUP_ATTR.sub(f'group-title="{group}"', extinf, count=1)
    head, sep, name = extinf.rpartition(",")
    if not sep:
        return extinf
    return f'{head} group-title="{group}",{name}'



def read_sources(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"missing {path}")
    urls = []
    # utf-8-sig：Windows 的 PowerShell 5.1 用 Out-File 写文件会带 BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse(text: str) -> list[tuple[str, str]]:
    """返回 [(EXTINF 行, URL), ...]。支持 m3u 和 m3u8，忽略 #EXTVLCOPT。"""
    out: list[tuple[str, str]] = []
    extinf = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            extinf = line
        elif line.startswith("#"):
            continue  # #EXTM3U / #EXTVLCOPT / #EXTGRP …
        else:
            if extinf:
                out.append((extinf, line))
                extinf = None
    return out


def channel_name(extinf: str) -> str:
    # #EXTINF:-1 tvg-id="..." group-title="...",频道名
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf


def dedupe_key(extinf: str) -> str:
    name = NOISE.sub("", channel_name(extinf))
    name = QUALITY.sub("", name)
    # 全角/半角、空白统一，避免「CCTV-1」和「CCTV 1」被当成两个台
    name = re.sub(r"[\s\-_·、]", "", name).casefold()
    return name


def probe(url: str, timeout: int) -> bool:
    """只看能不能连上并拿到 2xx/3xx，不读完整个流。"""
    if not url.lower().startswith(("http://", "https://")):
        return True  # rtmp/rtp 之类探不了，保留
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="探活，剔除连不上的源")
    ap.add_argument("--timeout", type=int, default=5, help="探活超时秒数（默认 5）")
    ap.add_argument("--no-sort", action="store_true", help="保持上游顺序，不做「国内 → 国外」排序")
    ap.add_argument("--sources", type=Path, default=SOURCES)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    seen: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    per_source: list[str] = []

    for src in read_sources(args.sources):
        try:
            text = fetch(src)
        except Exception as e:
            print(f"[skip] {src}: {e}", file=sys.stderr)
            per_source.append(src)
            continue
        entries = parse(text)
        kept = 0
        for extinf, url in entries:
            key = dedupe_key(extinf) or url
            if key in seen:
                continue  # 先到的优先，后到的视为重复
            seen[key] = (extinf, url)
            order.append(key)
            kept += 1
        print(f"[ok]   {src}: {len(entries)} 条，新增 {kept} 条")
        per_source.append(src)

    if not order:
        print("nothing merged; sources.txt empty or every source failed", file=sys.stderr)
        return 1

    items = [seen[k] for k in order]
    if args.probe:
        alive = []
        for i, (extinf, url) in enumerate(items, 1):
            if probe(url, args.timeout):
                alive.append((extinf, url))
            if i % 50 == 0:
                print(f"  probed {i}/{len(items)} …")
        print(f"[probe] {len(items)} → {len(alive)}")
        items = alive

    # 先把台名本地化，再排序：省级卫视是靠中文「卫视」二字认出来的，
    # 排完再翻译的话它们会全部落进「其它国内台」。
    items = [(localize(extinf), url) for extinf, url in items]
    if args.no_sort:
        print("[sort] skipped (--no-sort)")
        sorted_items = [(None, extinf, url) for extinf, url in items]
    else:
        sorted_items = sort_items(items)
        counts = {}
        for zone, _e, _u in sorted_items:
            counts[zone] = counts.get(zone, 0) + 1
        print("[sort] 央视 %d / 省级卫视 %d / 其它国内 %d / 港澳台 %d / 境外 %d"
              % (counts.get(0, 0), counts.get(1, 0), counts.get(2, 0),
                 counts.get(3, 0), counts.get(4, 0)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    lines.append("# merged by scripts/merge.py — do not edit by hand")
    for src in per_source:
        lines.append(f"#   source: {src}")
    for zone, extinf, url in sorted_items:
        if zone is not None:
            extinf = set_group(extinf, GROUP_NAMES[zone])
        lines.append(extinf)
        lines.append(url)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out} with {len(sorted_items)} channels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
