"""CharTunes —— 从音游谱面包中搜索、下载并提取音乐文件。

支持平台：
    osu!    需登录 cookie（搜索与下载全程）
    Phira   免登录（可选 cookie）
    Malody  需 key+uid（可由账密自动登录换取）

快速上手::

    from chartunes import PhiraClient, MalodyClient, OsuClient

    # Phira：免登录
    phira = PhiraClient()
    page = phira.search("DEADMAN")
    music = phira.download_music(page.items[0])
    music.save("./out")           # -> ./out/<曲名>.mp3

    # Malody：账密自动登录（或 MalodyClient(key=..., uid=...) 手动传入）
    malody = MalodyClient.login("user", "password")
    songs = malody.search("Sakuzyo")
    charts = malody.charts(songs[0])
    bundle = malody.download_bundle(charts[0])
    bundle.music.save("./out")    # -> ./out/<曲名>.ogg

    # osu!：浏览器 F12 抄 cookie
    osu = OsuClient(cookie="osu_session=...")
    page = osu.search("Sakuzyo")
    music = osu.download_music(page.items[0])

所有出站请求带轻度防风控：仿真实客户端指纹、cookie 会话保持、
随机抖动节流、超时/5xx 指数退避重试。参数一律走字典编码，杜绝注入。
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import random
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

__version__ = "0.1.0"
__all__ = [
    "CharTunesError", "AuthError", "NetworkError", "ParseError",
    "NotFoundError", "ExtractionError",
    "SongInfo", "ChartInfo", "SearchPage", "ExtractedFile", "MalodyBundle",
    "OsuClient", "PhiraClient", "MalodyClient",
]

log = logging.getLogger("chartunes")
log.addHandler(logging.NullHandler())

# 浏览器指纹（osu / Phira 用；Malody 仿游戏客户端：无 UA + MaVersion）
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(name: str, fallback: str = "untitled") -> str:
    """去掉文件名非法字符，压掉首尾空白与点号。"""
    cleaned = _ILLEGAL_FN.sub("_", name).strip(" .")
    return cleaned or fallback


# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------

class CharTunesError(Exception):
    """模块基础异常。"""


class AuthError(CharTunesError):
    """凭证缺失 / 失效 / 被拒绝。"""


class NetworkError(CharTunesError):
    """网络层失败（超时、连接错误、5xx 重试耗尽）。"""


class ParseError(CharTunesError):
    """响应结构与预期不符。"""


class NotFoundError(CharTunesError):
    """请求的资源不存在（如谱面无视频文件）。"""


class ExtractionError(CharTunesError):
    """谱面包解包 / 音乐定位失败。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ExtractedFile:
    """提取出的文件（内存中）。"""
    filename: str          # 建议保存名（含扩展名）
    data: bytes
    format: str            # mp3 / ogg / png / avi / ...
    source: str = ""       # 来源描述

    def save(self, path: str | Path) -> Path:
        """保存到目录（拼 filename）或完整文件路径，返回实际路径。"""
        p = Path(path)
        if p.suffix:                       # 视为完整文件路径
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.data)
        else:                              # 视为目录
            p.mkdir(parents=True, exist_ok=True)
            p = p / _safe_filename(self.filename)
            p.write_bytes(self.data)
        return p

    def __len__(self) -> int:
        return len(self.data)


@dataclass
class SongInfo:
    """歌曲级条目（Malody 搜索结果）。"""
    platform: str
    song_id: Any                 # Malody sid
    title: str = ""
    artist: str = ""
    cover: str | None = None     # 完整封面 URL（可能为空）
    duration: int | None = None  # 秒
    bpm: float | None = None
    extra: dict = field(default_factory=dict)
    _client: Any = field(default=None, compare=False, repr=False)

    def charts(self) -> list["ChartInfo"]:
        """该歌曲下的难度（谱面）列表。"""
        if self._client is None:
            raise CharTunesError("该 SongInfo 未绑定客户端，无法查询难度")
        return self._client.charts(self)

    def download_cover(self) -> ExtractedFile:
        if self._client is None:
            raise CharTunesError("该 SongInfo 未绑定客户端")
        return self._client.download_cover(self)


@dataclass
class ChartInfo:
    """谱面级条目（osu 谱面集 / Phira 谱面 / Malody 单难度）。"""
    platform: str
    chart_id: Any                # osu set_id / Phira id / Malody cid
    title: str = ""
    artist: str = ""
    difficulty: str | None = None   # 难度名（Malody version）
    charter: str | None = None      # 谱师
    extra: dict = field(default_factory=dict)
    _client: Any = field(default=None, compare=False, repr=False)

    def download_music(self) -> ExtractedFile:
        """下载谱面包并提取音乐（默认 mp3 / ogg 原样返回，不转码）。"""
        if self._client is None:
            raise CharTunesError("该 ChartInfo 未绑定客户端，无法下载")
        return self._client.download_music(self)

    def download_cover(self) -> ExtractedFile:
        if self._client is None:
            raise CharTunesError("该 ChartInfo 未绑定客户端")
        return self._client.download_cover(self)


@dataclass
class SearchPage:
    """一页搜索结果。"""
    items: list[ChartInfo]
    total: int | None = None     # 结果总数（若接口提供）
    cursor: str | None = None    # osu 翻页游标（cursor_string）


@dataclass
class MalodyBundle:
    """Malody 一个难度的完整下载产物。"""
    music: ExtractedFile                 # 音乐（通常 ogg，无加密）
    cover: ExtractedFile | None = None   # 曲绘（meta.background 匹配）
    chart_file: ExtractedFile | None = None  # .mc 原始谱面包（zip）
    manifest: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 公共客户端基座
# ---------------------------------------------------------------------------

class _BaseClient:
    """httpx 封装：指纹 headers、cookie 会话、节流抖动、退避重试、流式下载。"""

    name = "base"

    def __init__(
        self,
        headers: dict[str, str | None],
        *,
        timeout: float = 30.0,
        throttle: tuple[float, float] | None = (0.3, 0.8),
        max_retries: int = 3,
    ):
        self._throttle = throttle
        self._max_retries = max_retries
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.log = logging.getLogger(f"chartunes.{self.name}")
        self._http = httpx.Client(
            headers=headers, timeout=timeout, follow_redirects=True,
        )

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "_BaseClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 节流：最小间隔 + 随机抖动（线程安全） -------------------------------
    def _wait(self) -> None:
        with self._lock:
            if self._throttle:
                lo, hi = self._throttle
                wake = self._last_request + random.uniform(lo, hi)
                delay = wake - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            self._last_request = time.monotonic()

    # -- 请求：参数一律字典传入（防注入），超时/5xx 退避重试 ------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._wait()
            try:
                resp = self._http.request(method, url, params=params, data=data)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp,
                    )
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt == self._max_retries:
                    break
                backoff = 0.5 * (2 ** attempt) + random.uniform(0, 0.3)
                self.log.warning("%s 请求失败(%s)，%.1fs 后第 %d 次重试",
                                 url, e, backoff, attempt + 1)
                time.sleep(backoff)
        raise NetworkError(f"请求失败（已重试 {self._max_retries} 次）：{url}") from last_exc

    def _get_bytes(self, url: str, *, params: dict | None = None) -> bytes:
        """流式下载到内存，统一处理 4xx。"""
        self._wait()
        try:
            with self._http.stream("GET", url, params=params) as resp:
                if resp.status_code in (401, 403):
                    raise AuthError(f"下载被拒绝（HTTP {resp.status_code}）：{url}")
                if resp.status_code == 404:
                    raise NotFoundError(f"资源不存在（404）：{url}")
                if resp.status_code >= 400:
                    raise NetworkError(f"下载失败（HTTP {resp.status_code}）：{url}")
                chunks = []
                for chunk in resp.iter_bytes(1 << 16):
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as e:
            raise NetworkError(f"下载超时：{url}") from e
        except httpx.TransportError as e:
            raise NetworkError(f"下载失败：{url}") from e

    @staticmethod
    def _json(resp: httpx.Response, what: str = "响应") -> dict:
        try:
            return json.loads(resp.text)
        except ValueError as e:
            raise ParseError(f"{what}不是合法 JSON（HTTP {resp.status_code}）") from e


# ---------------------------------------------------------------------------
# osu!
# ---------------------------------------------------------------------------

class OsuClient(_BaseClient):
    """osu! 后端。搜索与下载全程需要登录 cookie（F12 -> Network -> 抄 Cookie 头）。

    注意：未登录时搜索接口可能返回"结构正确但内容错误"的结果——
    请务必使用已登录会话的 cookie。
    """

    name = "osu"
    _SEARCH = "https://osu.ppy.sh/beatmapsets/search"

    def __init__(self, cookie: str, **kw: Any):
        if not cookie or "=" not in cookie:
            raise AuthError("osu 需要登录 cookie（形如 'osu_session=...' 的完整 Cookie 头）")
        super().__init__(
            {
                "User-Agent": _BROWSER_UA,
                "Referer": "https://osu.ppy.sh/",
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
            },
            **kw,
        )

    # -- 搜索 ------------------------------------------------------------
    def search(
        self,
        q: str,
        *,
        cursor: str | None = None,
        include_covers: bool = False,
        include_preview: bool = False,
    ) -> SearchPage:
        """搜索谱面集。covers（8 图）与 preview_url 默认不返回，按需开启。"""
        params: dict[str, Any] = {"q": q, "s": "any"}
        if cursor:
            params["cursor_string"] = cursor
        resp = self._request("GET", self._SEARCH, params=params)
        if resp.status_code in (401, 403):
            raise AuthError("osu 搜索被拒绝：cookie 失效或未登录")
        payload = self._json(resp, "osu 搜索")
        sets = payload.get("beatmapsets")
        if sets is None:
            raise AuthError("osu 搜索返回结构异常——通常是未登录（内容正确性不可信）")
        items = [
            self._parse_set(s, include_covers, include_preview) for s in sets
        ]
        return SearchPage(items, total=payload.get("total"),
                          cursor=payload.get("cursor_string"))

    @staticmethod
    def _parse_set(s: dict, include_covers: bool, include_preview: bool) -> ChartInfo:
        extra: dict[str, Any] = {
            "creator": s.get("creator"),
            "status": s.get("status"),
            "video": s.get("video"),
            "bpm": s.get("bpm"),
            "nsfw": s.get("nsfw"),
        }
        if include_covers:
            extra["covers"] = s.get("covers") or {}
        if include_preview:
            extra["preview_url"] = s.get("preview_url")
        return ChartInfo(
            platform="osu",
            chart_id=s.get("id"),        # 谱面集 id（子难度里叫 beatmapset_id，值相同）
            title=s.get("title") or "",
            artist=s.get("artist") or "",
            extra=extra,
        )

    # -- 下载 ------------------------------------------------------------
    def download_osz(self, chart: ChartInfo) -> bytes:
        """下载谱面集 .osz（zip）原始字节。CDN 重定向自动跟随。"""
        url = f"https://osu.ppy.sh/beatmapsets/{chart.chart_id}/download"
        return self._get_bytes(url)

    def download_music(self, chart: ChartInfo) -> ExtractedFile:
        """下载 osz 并提取歌曲 mp3。

        定位方式：读包内 .osu 的 [General] AudioFilename（权威），
        失败回退"最大 mp3"。其余音频均为 key 音，一律忽略。
        """
        osz = self.download_osz(chart)
        try:
            zf = zipfile.ZipFile(io.BytesIO(osz))
        except zipfile.BadZipFile as e:
            raise ExtractionError("osz 解包失败：内容不是合法 zip") from e
        member = self._locate_audio(zf, chart)
        return ExtractedFile(
            filename=_titled_filename(member, chart.title, "mp3"),
            data=zf.read(member),
            format=Path(member).suffix.lstrip(".").lower() or "mp3",
            source=f"osu beatmapset {chart.chart_id}",
        )

    def download_video(self, chart: ChartInfo) -> ExtractedFile:
        """提取谱面集内视频文件（仅显式请求时调用；只扫描 .avi/.mp4）。"""
        osz = self.download_osz(chart)
        zf = zipfile.ZipFile(io.BytesIO(osz))
        videos = [
            n for n in zf.namelist()
            if n.lower().endswith((".avi", ".mp4")) and not n.endswith("/")
        ]
        if not videos:
            if chart.extra.get("video") is False:
                raise NotFoundError(f"谱面集 {chart.chart_id} 不含视频（video=false）")
            raise NotFoundError(f"谱面集 {chart.chart_id} 内未找到 .avi/.mp4 文件")
        best = max(videos, key=lambda n: zf.getinfo(n).file_size)
        return ExtractedFile(
            filename=_titled_filename(best, chart.title, "mp4"),
            data=zf.read(best),
            format=Path(best).suffix.lstrip(".").lower(),
            source=f"osu beatmapset {chart.chart_id}",
        )

    def download_cover(self, chart: ChartInfo, size: str = "cover") -> ExtractedFile:
        """下载谱面集封面。需在 search 时 include_covers=True。"""
        covers: dict | None = chart.extra.get("covers")
        if not covers:
            raise NotFoundError("无封面信息：请在 search 时传 include_covers=True")
        url = covers.get(size) or covers.get("cover")
        if not url:
            raise NotFoundError(f"covers 中不存在尺寸 '{size}'")
        data = self._get_bytes(url)
        return ExtractedFile(filename=f"{_safe_filename(chart.title)}_cover.jpg",
                             data=data, format="jpg",
                             source=f"osu covers[{size}]")

    def download_preview(self, chart: ChartInfo) -> ExtractedFile:
        """下载预览音频。需在 search 时 include_preview=True。"""
        url = chart.extra.get("preview_url")
        if not url:
            raise NotFoundError("无预览链接：请在 search 时传 include_preview=True")
        data = self._get_bytes(url)
        return ExtractedFile(filename=f"{_safe_filename(chart.title)}_preview.mp3",
                             data=data, format="mp3", source="osu preview_url")

    # -- 音乐定位 ------------------------------------------------------------
    @staticmethod
    def _locate_audio(zf: zipfile.ZipFile, chart: ChartInfo) -> str:
        names = [n for n in zf.namelist() if not n.endswith("/")]

        # 1) 权威路径：.osu [General] AudioFilename
        wanted: set[str] = set()
        for n in names:
            if n.lower().endswith(".osu"):
                ref = _osu_audio_ref(
                    zf.read(n).decode("utf-8-sig", errors="replace"))
                if ref:
                    wanted.add(ref)
        for target in wanted:
            hit = _match_member(names, target)
            if hit:
                return hit

        # 2) 回退：最大的 mp3
        mp3s = [n for n in names if n.lower().endswith(".mp3")]
        if not mp3s:
            raise ExtractionError(
                f"osz 内未定位到音频（.osu 引用={sorted(wanted) or '无'}，mp3 数=0）")
        log.warning("osz %s：AudioFilename 未命中，回退最大 mp3", chart.chart_id)
        return max(mp3s, key=lambda n: zf.getinfo(n).file_size)


def _match_member(names: list[str], target: str) -> str | None:
    """在 zip 成员中按文件名匹配（精确 -> 带目录 -> 忽略大小写）。"""
    if target in names:
        return target
    suffix = "/" + target
    for n in names:
        if n.endswith(suffix):
            return n
    lower = target.lower()
    lsuffix = "/" + lower
    for n in names:
        if n.lower() == lower or n.lower().endswith(lsuffix):
            return n
    return None


def _osu_audio_ref(text: str) -> str | None:
    """读 .osu 文本 [General] 段的 AudioFilename。"""
    m = re.search(r"^\s*AudioFilename\s*:\s*(.+?)\s*$", text,
                  re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m and m.group(1) else None


def _norm_for_match(s: str) -> str:
    """归一化：小写、仅保留字母数字（含全角/Unicode 字母）。"""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _member_matches_title(stem: str, title: str) -> bool:
    """zip 成员词干是否已'对应'曲名（归一化后相等，或包含曲名，
    如 'Artist - Title' 包含 'Title'）。"""
    n_stem, n_title = _norm_for_match(stem), _norm_for_match(title)
    return bool(n_title) and (n_stem == n_title or n_title in n_stem)


def _titled_filename(member_name: str, title: str, default_ext: str = "") -> str:
    """提取文件的建议保存名：成员名与曲名对应则保留原名，
    否则（如 'audio.mp3' 这类随手命名）回退为 '曲名.扩展名'。"""
    name = Path(member_name).name
    ext = Path(name).suffix.lstrip(".").lower() or default_ext
    if _member_matches_title(Path(name).stem, title):
        return name
    titled = _safe_filename(title) if _norm_for_match(title) else ""
    return f"{titled}.{ext}" if titled else name


def _osu_background_ref(text: str) -> str | None:
    """读 .osu 文本 [Events] 段的 Background 引用（形如 0,0,"bg.jpg",0,0）。"""
    m = re.search(r'^\s*0,\s*\d+\s*,\s*"(.+?)"', text, re.MULTILINE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Phira
# ---------------------------------------------------------------------------

class PhiraClient(_BaseClient):
    """Phira 后端。搜索免登录；cookie 可选传入以备风控收紧。"""

    name = "phira"
    _SEARCH = "https://phira.5wyxi.com/chart"

    def __init__(self, cookie: str | None = None, **kw: Any):
        headers = {
            "User-Agent": _BROWSER_UA,
            "Referer": "https://phira.moe/",
            "Accept": "application/json",
        }
        if cookie:
            headers["Cookie"] = cookie
        super().__init__(headers, **kw)

    # -- 搜索 ------------------------------------------------------------
    def search(self, q: str, *, page: int = 1, page_size: int = 28,
               order: str = "name") -> SearchPage:
        params = {"pageNum": page_size, "page": page, "order": order, "search": q}
        resp = self._request("GET", self._SEARCH, params=params)
        if resp.status_code >= 400:
            raise NetworkError(f"Phira 搜索失败（HTTP {resp.status_code}）")
        payload = self._json(resp, "Phira 搜索")
        results = payload.get("results")
        if not isinstance(results, list):
            raise ParseError("Phira 搜索返回结构异常（缺少 results）")
        items = [self._parse_chart(r) for r in results]
        return SearchPage(items, total=payload.get("count"))

    @staticmethod
    def _parse_chart(r: dict) -> ChartInfo:
        return ChartInfo(
            platform="phira",
            chart_id=r.get("id"),
            title=html.unescape(r.get("name") or ""),
            artist=html.unescape(r.get("composer") or ""),
            charter=html.unescape(r.get("charter") or "") or None,
            extra={
                "illustration": r.get("illustration"),
                "file": r.get("file"),
                "preview": r.get("preview"),
                "level": r.get("level"),
                "difficulty": r.get("difficulty"),
                "illustrator": r.get("illustrator"),
                "ranked": r.get("ranked"),
            },
        )

    # -- 下载 ------------------------------------------------------------
    def download_package(self, chart: ChartInfo) -> bytes:
        """下载谱面包原始字节（无扩展名，本质 zip）。"""
        url = chart.extra.get("file")
        if not url:
            raise NotFoundError("该条目缺少 file 下载链接")
        return self._get_bytes(url)

    def download_music(self, chart: ChartInfo) -> ExtractedFile:
        """下载谱面包并提取包内唯一 mp3。"""
        raw = self.download_package(chart)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise ExtractionError("Phira 谱面包解包失败：内容不是合法 zip") from e
        mp3s = [n for n in zf.namelist()
                if n.lower().endswith(".mp3") and not n.endswith("/")]
        if not mp3s:
            raise ExtractionError("Phira 包内没有 mp3 文件")
        if len(mp3s) > 1:
            log.warning("Phira %s 包内有 %d 个 mp3，取最大者", chart.chart_id, len(mp3s))
        best = max(mp3s, key=lambda n: zf.getinfo(n).file_size)
        return ExtractedFile(
            filename=f"{_safe_filename(chart.title)}.mp3",
            data=zf.read(best), format="mp3",
            source=f"phira chart {chart.chart_id}",
        )

    def download_cover(self, chart: ChartInfo) -> ExtractedFile:
        """下载曲绘（响应无扩展名，按 magic bytes 判定真实格式，缺省 png）。"""
        url = chart.extra.get("illustration")
        if not url:
            raise NotFoundError("该条目缺少 illustration 链接")
        data = self._get_bytes(url)
        ext = _sniff_image_ext(data) or "png"
        return ExtractedFile(filename=f"{_safe_filename(chart.title)}.{ext}",
                             data=data, format=ext,
                             source=f"phira illustration {chart.chart_id}")


def _sniff_image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------------------
# Malody
# ---------------------------------------------------------------------------

class MalodyClient(_BaseClient):
    """Malody 后端。全程需要 key+uid：

    - ``MalodyClient.login(name, password)`` 账密自动登录（psw 为无盐 md5）；
    - ``MalodyClient(key=..., uid=...)`` 直接传入已有凭证（可长期缓存，
      实测多次登录签发多个 key 且旧 key 不立即作废）。
    """

    name = "malody"
    API = "http://m.mugzone.net"
    CDN = "http://cdn0.machart.top"      # cdn1/cdn2 不存在，仅 cdn0
    COVER_HOST = "http://cni.machart.top"
    MAVERSION = 262919                   # 客户端版本号（登录 v 参数 + MaVersion 头）
    DEFAULT_DEVICE_ID = "3665630189535747679_0"   # h 参数；语义待验证，可配置

    def __init__(self, *, key: str | None = None, uid: int | None = None,
                 name: str | None = None, password: str | None = None,
                 device_id: str | None = None, **kw: Any):
        self._device_id = device_id or self.DEFAULT_DEVICE_ID
        self._name = name
        self._password = password
        self.key: str | None = key
        self.uid: int | None = uid
        super().__init__(
            {
                "Referer": "http://m.mugzone.net",
                "Accept": "*/*",
                "MaVersion": str(self.MAVERSION),
            },
            **kw,
        )
        # 游戏客户端不发 User-Agent，删除 httpx 注入的默认 UA 以贴近真实指纹
        if "user-agent" in self._http.headers:
            del self._http.headers["user-agent"]
        if not (key and uid) and not (name and password):
            raise AuthError("需要 (key, uid) 或 (name, password) 其一")

    # -- 登录 ------------------------------------------------------------
    @classmethod
    def login(cls, name: str, password: str, *, device_id: str | None = None,
              **kw: Any) -> "MalodyClient":
        c = cls(name=name, password=password, device_id=device_id, **kw)
        c._do_login()
        return c

    def _do_login(self) -> None:
        assert self._name and self._password
        form = {
            "name": self._name,
            "psw": hashlib.md5(self._password.encode()).hexdigest(),  # 无盐 md5（已实测）
            "v": self.MAVERSION,
            "h": self._device_id,
        }
        resp = self._request("POST", f"{self.API}/cgi/login", data=form)
        if resp.status_code >= 400:
            raise AuthError(f"Malody 登录失败（HTTP {resp.status_code}）")
        payload = self._json(resp, "Malody 登录")
        if payload.get("code") != 0:
            raise AuthError(f"Malody 登录被拒绝：{payload}")
        d = payload.get("data") or {}
        self.key, self.uid = d.get("key"), d.get("uid")
        if not (self.key and self.uid):
            raise ParseError(f"Malody 登录返回缺少 key/uid：{payload}")
        self.log.debug("Malody 登录成功 uid=%s", self.uid)

    # -- cgi 统一入口（带 key+uid 与自动重登） ------------------------------
    def _cgi(self, path: str, params: dict) -> dict:
        if not (self.key and self.uid):
            if self._name and self._password:
                self._do_login()
            else:
                raise AuthError("Malody 凭证缺失（key/uid）")
        merged = {**params, "key": self.key, "uid": self.uid}
        for attempt in (1, 2):
            resp = self._request("GET", f"{self.API}{path}", params=merged)
            if resp.status_code >= 400:
                raise NetworkError(f"cgi {path} 失败（HTTP {resp.status_code}）")
            payload = self._json(resp, f"cgi {path}")
            code = payload.get("code")
            if code == 0:
                data = payload.get("data")
                return data if data is not None else {}
            if code == -1 and attempt == 1 and self._name and self._password:
                self.log.warning("key 失效（code=-1），自动重登")
                self._do_login()
                merged = {**params, "key": self.key, "uid": self.uid}
                continue
            raise AuthError(f"cgi {path} 返回 code={code}：{payload}")
        raise AuthError(f"cgi {path} 重登后仍失败")

    # -- 搜索（两段式） ------------------------------------------------------
    def search(self, q: str) -> list[SongInfo]:
        """按关键词搜歌（type=1）。返回歌曲级列表。"""
        data = self._cgi("/cgi/list", {"type": 1, "word": q, "org": 1})
        songs = []
        for e in data if isinstance(data, list) else []:
            cover = e.get("cover") or ""
            songs.append(SongInfo(
                platform="malody",
                song_id=e.get("id"),
                title=e.get("title") or "",
                artist=e.get("artist") or "",
                cover=(f"{self.COVER_HOST}{cover}" if cover else None),
                duration=e.get("length"),
                bpm=e.get("bpm"),
                extra={"st": e.get("st"), "uptime": e.get("uptime"),
                       "mode_mask": e.get("mode"), "_word": q},
                _client=self,
            ))
        return songs

    def charts(self, song: SongInfo) -> list[ChartInfo]:
        """歌曲下的难度列表（type=2，word 沿用搜索词以贴近游戏行为）。"""
        data = self._cgi("/cgi/list", {
            "type": 2, "sid": song.song_id,
            "word": song.extra.get("_word", ""),
        })
        out = []
        for e in data if isinstance(data, list) else []:
            out.append(ChartInfo(
                platform="malody",
                chart_id=e.get("cid"),
                title=song.title,
                artist=song.artist,
                difficulty=e.get("version"),
                charter=e.get("creator"),
                extra={"mode": e.get("mode"), "length": e.get("length"),
                       "size": e.get("size"), "pc": e.get("pc"),
                       "time": e.get("time"), "charter_uid": e.get("uid")},
                _client=self,
            ))
        return out

    # -- 下载 ------------------------------------------------------------
    def _manifest(self, chart: ChartInfo) -> dict:
        data = self._cgi("/cgi/chart/download", {"v": 2, "cid": chart.chart_id})
        if "list" not in data:
            raise ParseError(f"download manifest 结构异常：{data}")
        return data

    def _fetch_cdn_file(self, sid: Any, uid: Any, item: dict) -> tuple[dict, bytes]:
        # URL: cdn0.machart.top/<sid>/<manifest.uid>/<file>
        # （中段是 manifest 顶层 uid，非固定值——uid=0 的谱面才长 /0/）
        url = f"{self.CDN}/{sid}/{uid}/{item['file']}"
        data = self._get_bytes(url)
        # hash 语义（实测）：.mc 为包内 JSON 的 md5——CDN 的 zip 是现场打的，
        # 外层字节不稳定；其余文件（ogg/jpg）为原始字节 md5。
        target = data
        if item["name"].lower().endswith(".mc"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                members = [n for n in zf.namelist() if n.lower().endswith(".mc")]
                if members:
                    target = zf.read(members[0])
            except zipfile.BadZipFile:
                pass
        digest = hashlib.md5(target).hexdigest()
        if digest != item.get("hash"):
            raise NetworkError(
                f"文件校验失败 md5 不符：{item.get('name')} "
                f"(期望 {item.get('hash')}, 实得 {digest})")
        return item, data

    def download_bundle(self, chart: ChartInfo, *, max_workers: int = 3) -> MalodyBundle:
        """下载一个难度的完整文件包（并发下载 + md5 校验）。

        兼容两类谱面：
        - 原生 .mc：note[] 中 type:1 且带 sound 的条目即 BGM 轨；
        - osu! 移植谱（manifest 无 .mc、含 .osu）：读 .osu 的 AudioFilename；
        - 都没有：回退 manifest 中最大的音频文件。
        """
        manifest = self._manifest(chart)
        sid = manifest.get("sid") or manifest.get("dsid")
        uid = manifest.get("uid") or 0
        items = manifest.get("list") or []
        if not items:
            raise NotFoundError(f"cid {chart.chart_id} 的 manifest 为空")
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            fetched = list(pool.map(
                lambda it: self._fetch_cdn_file(sid, uid, it), items))

        by_name = {it["name"]: data for it, data in fetched}
        mc_item = next((it for it, _ in fetched if it["name"].lower().endswith(".mc")),
                       None)
        osu_item = next((it for it, _ in fetched if it["name"].lower().endswith(".osu")),
                        None)
        if mc_item is None and osu_item is None:
            raise ParseError("manifest 中既没有 .mc 也没有 .osu 谱面文件")

        mc_doc = self._load_mc(by_name[mc_item["name"]]) if mc_item else None
        meta = mc_doc.get("meta") or {} if mc_doc else {}
        names = list(by_name)
        title_base = (meta.get("song") or {}).get("title") or chart.title

        # ---- 音乐定位链 -------------------------------------------------
        music_name: str | None = None
        if mc_doc:
            bgm = _malody_bgm_name(mc_doc)
            if bgm in by_name:
                music_name = bgm
        if music_name is None and osu_item:
            ref = _osu_audio_ref(by_name[osu_item["name"]].decode(
                "utf-8-sig", errors="replace"))
            if ref:
                music_name = _match_member(names, ref)
        if music_name is None:
            music_name = _fallback_audio(by_name)
        if music_name is None:
            raise ExtractionError(
                f"无法定位音乐：manifest 名单={names}")
        ext = Path(music_name).suffix.lstrip(".").lower() or "ogg"
        music = ExtractedFile(
            filename=f"{_safe_filename(title_base)}.{ext}",
            data=by_name[music_name], format=ext,
            source=f"malody cid {chart.chart_id}",
        )

        # ---- 曲绘定位链 -------------------------------------------------
        bg: str | None = None
        if mc_doc:
            bg = meta.get("background") or None
            if bg and bg not in by_name:
                bg = None
        if bg is None and osu_item:
            ref = _osu_background_ref(by_name[osu_item["name"]].decode(
                "utf-8-sig", errors="replace"))
            if ref:
                hit = _match_member(names, ref)
                bg = hit if hit and hit.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")) else None
        cover = None
        if bg:
            bext = Path(bg).suffix.lstrip(".").lower() or "png"
            cover = ExtractedFile(
                filename=f"{_safe_filename(title_base)}.{bext}",
                data=by_name[bg], format=bext,
                source=f"malody cid {chart.chart_id}",
            )

        chart_item = mc_item or osu_item
        chart_file = ExtractedFile(filename=chart_item["name"],
                                   data=by_name[chart_item["name"]],
                                   format=Path(chart_item["name"]).suffix.lstrip("."),
                                   source=f"malody cid {chart.chart_id}")
        return MalodyBundle(music=music, cover=cover, chart_file=chart_file,
                            manifest=manifest)

    def download_music(self, chart: ChartInfo) -> ExtractedFile:
        return self.download_bundle(chart).music

    def download_cover(self, target: ChartInfo | SongInfo) -> ExtractedFile:
        """曲绘：SongInfo 走 cni 封面链接；ChartInfo 走整包提取。"""
        if isinstance(target, SongInfo):
            if not target.cover:
                raise NotFoundError("该歌曲没有封面（cover 为空）")
            data = self._get_bytes(target.cover)
            ext = _sniff_image_ext(data) or "png"
            return ExtractedFile(
                filename=f"{_safe_filename(target.title)}.{ext}",
                data=data, format=ext, source="malody cover")
        bundle = self.download_bundle(target)
        if bundle.cover is None:
            raise NotFoundError(f"cid {target.chart_id} 的包内没有曲绘")
        return bundle.cover

    @staticmethod
    def _load_mc(raw: bytes) -> dict:
        """CDN 下到的 .mc 是 zip，内含纯 JSON 谱面；兼容直接是 JSON 的情况。"""
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            members = [n for n in zf.namelist() if n.lower().endswith(".mc")]
            if members:
                raw = zf.read(members[0])
        except zipfile.BadZipFile:
            pass
        try:
            return json.loads(raw.decode("utf-8-sig", errors="replace"))
        except ValueError as e:
            raise ParseError(".mc 谱面解析失败：不是合法 JSON") from e


def _malody_bgm_name(mc_doc: dict) -> str | None:
    """在 .mc 的 note[] 中找 BGM 轨（type:1 且带 sound 字段的条目）。"""
    for note in mc_doc.get("note") or []:
        if note.get("type") == 1 and note.get("sound"):
            return note["sound"]
    return None


def _fallback_audio(by_name: dict[str, bytes]) -> str | None:
    """兜底：manifest 里唯一/最大的音频扩展名文件。"""
    audio_exts = (".ogg", ".mp3", ".wav", ".flac")
    audios = [n for n in by_name if n.lower().endswith(audio_exts)]
    if not audios:
        return None
    return max(audios, key=lambda n: len(by_name[n]))
