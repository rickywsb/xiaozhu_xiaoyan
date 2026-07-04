"""core/news.py — 半导体 / 存储 / 光通信 资讯聚合（纯免费 RSS，无需 API key）

数据边界说明：
  本模块聚合的是**公开新闻 / 新闻稿 RSS**（Google News、行业垂直媒体、TrendForce
  公开 press release、yfinance 个股新闻），**不是**卖方深度研报或付费投研数据
  （TrendForce/Yole/大行研报正文均在付费墙内）。因此本页定位为「资讯聚合」，
  仅供辅助了解行业动向，非投研建议。

来源：
  • Google News RSS  — 按主题关键词搜索（news.google.com/rss/search）
  • 行业垂直媒体 RSS  — 存储: Blocks & Files；光通信: Gazettabyte；大盘: SEMI / EE Times
  • TrendForce       — 公开 press-center RSS（新闻稿摘要，正文引导至官网）
  • yfinance         — 每只持仓/关注标的的近期新闻
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── 常量 ─────────────────────────────────────────────────────────────────────

_TIMEOUT = 12
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Atom 命名空间
_ATOM = "{http://www.w3.org/2005/Atom}"

# 主题定义：关键词用于 Google News 搜索；feeds 是该主题的专属行业 RSS。
THEMES: dict[str, dict] = {
    "存储": {
        "emoji": "💾",
        "keywords": [
            "HBM memory", "DRAM price", "NAND flash", "HBM4",
            "memory shortage", "Micron HBM", "SK Hynix DRAM",
        ],
        "feeds": [
            ("Blocks & Files", "https://blocksandfiles.com/feed/"),
            # TrendForce 公开 press-center / 新闻稿（免费，最贴研报口吻）
            ("TrendForce", "https://www.trendforce.com/news/feed/"),
        ],
        "sector_tag": "存",   # 对应 portfolio.json 里的 sector
    },
    "光通信": {
        "emoji": "🔦",
        "keywords": [
            "800G optical transceiver", "1.6T optical", "silicon photonics",
            "co-packaged optics CPO", "optical networking datacenter",
            "Lightwave optical", "datacenter interconnect optics",
        ],
        # 光通信缺乏稳定的免费专属 RSS（Gazettabyte/Lightwave 被墙或改版），
        # 故纯靠上面的 Google News 关键词覆盖，相关性更高、噪声更少。
        "feeds": [],
        "sector_tag": "光",
    },
    "半导体大盘": {
        "emoji": "🔬",
        "keywords": [
            "semiconductor industry", "AI accelerator chip",
            "TSMC capex foundry", "wafer fab equipment WFE",
        ],
        "feeds": [
            ("SEMI", "https://www.semi.org/en/rss.xml"),
            ("EE Times", "https://www.eetimes.com/feed/"),
        ],
        "sector_tag": None,
    },
}


# ─── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: datetime | None = None
    summary: str = ""
    tickers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "published": self.published,
            "summary": self.summary,
            "tickers": list(self.tickers),
        }


# ─── 底层抓取 / 解析 ──────────────────────────────────────────────────────────

def _http_get(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    # RSS 2.0 pubDate (RFC 822)
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # Atom / ISO 8601
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_feed(xml_text: str, default_source: str) -> list[NewsItem]:
    """兼容 RSS 2.0 与 Atom，返回 NewsItem 列表。"""
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except Exception:
        return items

    # ── RSS 2.0：channel/item ──
    channel = root.find("channel")
    if channel is not None:
        for it in channel.findall("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link:
                continue
            src_el = it.find("source")
            source = (src_el.text.strip() if src_el is not None and src_el.text else default_source)
            items.append(NewsItem(
                title=_strip_html(title),
                link=link,
                source=source,
                published=_parse_date(it.findtext("pubDate")),
                summary=_strip_html(it.findtext("description") or "")[:280],
            ))
        return items

    # ── Atom：feed/entry ──
    for entry in root.findall(f"{_ATOM}entry"):
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        link = ""
        for lk in entry.findall(f"{_ATOM}link"):
            rel = lk.get("rel", "alternate")
            if rel == "alternate" or not link:
                link = lk.get("href", "") or link
        if not title or not link:
            continue
        published = (
            _parse_date(entry.findtext(f"{_ATOM}published"))
            or _parse_date(entry.findtext(f"{_ATOM}updated"))
        )
        summary = entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or ""
        items.append(NewsItem(
            title=_strip_html(title),
            link=link,
            source=default_source,
            published=published,
            summary=_strip_html(summary)[:280],
        ))
    return items


# ─── 各来源 ───────────────────────────────────────────────────────────────────

def fetch_google_news(query: str, limit: int = 15) -> list[NewsItem]:
    """Google News RSS 按关键词搜索（英文，近 30 天热度靠前）。"""
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query + ' when:30d')}&hl=en-US&gl=US&ceid=US:en"
    )
    xml_text = _http_get(url)
    if not xml_text:
        return []
    items = _parse_feed(xml_text, default_source="Google News")
    return items[:limit]


def fetch_feed(source_name: str, url: str, limit: int = 15) -> list[NewsItem]:
    """抓取单个行业 RSS/Atom feed。"""
    xml_text = _http_get(url)
    if not xml_text:
        return []
    items = _parse_feed(xml_text, default_source=source_name)
    return items[:limit]


def fetch_ticker_news(tickers: list[str], limit_per: int = 4) -> list[NewsItem]:
    """用 yfinance 拉每只标的的近期新闻。"""
    import config
    import yfinance as yf

    out: list[NewsItem] = []
    for t in tickers:
        if not t or t.upper() == config.CASH_TICKER:
            continue
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            continue
        for n in raw[:limit_per]:
            # yfinance 新版把字段放在 content 里，旧版是扁平结构
            content = n.get("content") if isinstance(n.get("content"), dict) else n
            title = content.get("title") or n.get("title") or ""
            if not title:
                continue
            link = (
                (content.get("canonicalUrl") or {}).get("url")
                if isinstance(content.get("canonicalUrl"), dict) else None
            ) or content.get("link") or n.get("link") or ""
            provider = (
                (content.get("provider") or {}).get("displayName")
                if isinstance(content.get("provider"), dict) else None
            ) or n.get("publisher") or "Yahoo Finance"
            ts = n.get("providerPublishTime")
            published = None
            if ts:
                try:
                    published = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                except Exception:
                    published = None
            if published is None:
                published = _parse_date(content.get("pubDate") or content.get("displayTime"))
            out.append(NewsItem(
                title=_strip_html(title),
                link=link,
                source=provider,
                published=published,
                summary=_strip_html(content.get("summary") or content.get("description") or "")[:280],
                tickers=[t.upper()],
            ))
    return out


# ─── 聚合 / 去重 / 排序 ───────────────────────────────────────────────────────

def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """按标题（归一化）+ 链接去重。"""
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it.title.lower())[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _sort_by_date(items: list[NewsItem]) -> list[NewsItem]:
    _min = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda x: x.published or _min, reverse=True)


def fetch_theme(theme_name: str, google_limit: int = 12, feed_limit: int = 12) -> list[NewsItem]:
    """聚合某个主题的全部来源（关键词 Google News + 该主题行业 feeds）。"""
    theme = THEMES.get(theme_name)
    if theme is None:
        return []
    items: list[NewsItem] = []
    for kw in theme["keywords"]:
        items.extend(fetch_google_news(kw, limit=6))
    for source_name, url in theme["feeds"]:
        items.extend(fetch_feed(source_name, url, limit=feed_limit))
    return _sort_by_date(_dedupe(items))


def fetch_all_themes() -> dict[str, list[NewsItem]]:
    """返回 {主题: [NewsItem, …]}。"""
    return {name: fetch_theme(name) for name in THEMES}


def theme_tickers(portfolio: dict, sector_tag: str | None) -> list[str]:
    """从 portfolio 中挑出属于某 sector（存/光）的标的 ticker。"""
    if not sector_tag:
        return []
    out: list[str] = []
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            if pos.get("sector") == sector_tag:
                t = (pos.get("yf_ticker") or "").upper()
                if t:
                    out.append(t)
    return sorted(set(out))


def portfolio_tickers(portfolio: dict) -> list[str]:
    """portfolio 全部标的（去 CASH）。"""
    import config
    out: list[str] = []
    for acc in portfolio.get("accounts", []):
        for pos in acc.get("positions", []):
            t = (pos.get("yf_ticker") or "").upper()
            if t and t != config.CASH_TICKER:
                out.append(t)
    return sorted(set(out))
