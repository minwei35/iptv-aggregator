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
    # 央视
    "CCTV-1": "CCTV-1 综合",
    "CCTV-2": "CCTV-2 财经",
    "CCTV-3": "CCTV-3 综艺",
    "CCTV-4": "CCTV-4 中文国际",
    "CCTV-5": "CCTV-5 体育",
    "CCTV-5+": "CCTV-5+ 体育赛事",
    "CCTV-6": "CCTV-6 电影",
    "CCTV-7": "CCTV-7 国防军事",
    "CCTV-8": "CCTV-8 电视剧",
    "CCTV-9": "CCTV-9 纪录",
    "CCTV-10": "CCTV-10 科教",
    "CCTV-11": "CCTV-11 戏曲",
    "CCTV-12": "CCTV-12 社会与法",
    "CCTV-13": "CCTV-13 新闻",
    "CCTV-14": "CCTV-14 少儿",
    "CCTV-15": "CCTV-15 音乐",
    "CCTV-16": "CCTV-16 奥林匹克",
    "CCTV-17": "CCTV-17 农业农村",
    "CCTV-4K": "CCTV-4K 超高清",
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

    args.out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    lines.append("# merged by scripts/merge.py — do not edit by hand")
    for src in per_source:
        lines.append(f"#   source: {src}")
    for extinf, url in items:
        lines.append(localize(extinf))
        lines.append(url)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out} with {len(items)} channels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
