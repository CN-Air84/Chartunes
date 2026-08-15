# -*- coding: utf-8 -*-
"""CharTunes 离线测试：全部基于仓库内抓包 fixtures 与内存构造数据，不访问网络。"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chartunes as ct  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load_json(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# fixtures 解析：osu
# ---------------------------------------------------------------------------

class TestOsuParse:
    def setup_method(self):
        self.client = ct.OsuClient(cookie="osu_session=fake", throttle=None)

    def test_parse_basic(self):
        page = self.client.search.__wrapped__ if False else None  # noqa: F841
        payload = load_json("osu_search.json")
        sets = payload["beatmapsets"]
        items = [ct.OsuClient._parse_set(s, False, False) for s in sets]
        first = items[0]
        assert first.platform == "osu"
        assert first.chart_id == 950681            # set 级 id
        assert first.artist == "Sakuzyo"
        assert first.title == "Fracture Ray"
        assert "covers" not in first.extra         # 默认不返回图片资源
        assert "preview_url" not in first.extra
        assert len(items) == 50 and payload["total"] == 109

    def test_parse_gated_resources(self):
        payload = load_json("osu_search.json")
        s = payload["beatmapsets"][0]
        item = ct.OsuClient._parse_set(s, True, True)
        assert set(item.extra["covers"]) == {
            "cover", "cover@2x", "card", "card@2x",
            "list", "list@2x", "slimcover", "slimcover@2x",
        }
        assert item.extra["preview_url"].startswith("https://b.ppy.sh/preview/")

    def test_search_http_layer(self, monkeypatch):
        """搜索应走 params 字典（防注入），并透传 cursor。"""
        captured = {}

        def fake_request(method, url, *, params=None, data=None):
            captured.update(method=method, url=url, params=params)
            return _FakeResponse(200, load_json("osu_search.json"))

        monkeypatch.setattr(self.client, "_request", fake_request)
        page = self.client.search("Sakuzyo & ?q=1", cursor="CURSOR")
        assert captured["url"] == "https://osu.ppy.sh/beatmapsets/search"
        assert captured["params"]["q"] == "Sakuzyo & ?q=1"   # 原样入参，未拼接
        assert captured["params"]["s"] == "any"
        assert captured["params"]["cursor_string"] == "CURSOR"
        assert page.cursor == payload_cursor(load_json("osu_search.json")) \
            if False else page.cursor is not None
        assert len(page.items) == 50


def payload_cursor(payload):
    return payload.get("cursor_string")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    @property
    def text(self):
        return json.dumps(self._body)


# ---------------------------------------------------------------------------
# fixtures 解析：Phira
# ---------------------------------------------------------------------------

class TestPhiraParse:
    def setup_method(self):
        self.client = ct.PhiraClient(throttle=None)

    def test_parse(self):
        payload = load_json("phira_chart.json")
        items = [ct.PhiraClient._parse_chart(r) for r in payload["results"]]
        first = items[0]
        assert first.platform == "phira"
        assert first.chart_id == 11222
        assert first.title == "DEADMAN'S BALLAD"
        assert first.artist == "cosMo@暴走"
        assert first.extra["file"].startswith("https://phira.5wyxi.com/files/")
        assert first.extra["illustration"].startswith("https://phira.5wyxi.com/files/")
        assert len(items) == 21 and payload["count"] == 21

    def test_search_params(self, monkeypatch):
        captured = {}

        def fake_request(method, url, *, params=None, data=None):
            captured.update(url=url, params=params)
            return _FakeResponse(200, load_json("phira_chart.json"))

        monkeypatch.setattr(self.client, "_request", fake_request)
        nasty = "a&b=c' --injection"
        self.client.search(nasty, page=2, page_size=50)
        assert captured["url"] == "https://phira.5wyxi.com/chart"
        assert captured["params"] == {
            "pageNum": 50, "page": 2, "order": "name", "search": nasty,
        }

    def test_download_music_unique_mp3(self, monkeypatch):
        pkg = make_zip({
            "chart.json": b"{}",
            "music.mp3": b"\x00" * 100,
            "illust.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
        })
        monkeypatch.setattr(self.client, "download_package", lambda chart: pkg)
        chart = ct.PhiraClient._parse_chart(load_json("phira_chart.json")["results"][0])
        music = self.client.download_music(chart)
        assert music.format == "mp3" and len(music) == 100
        assert music.filename == "DEADMAN'S BALLAD.mp3"

    def test_download_music_none(self, monkeypatch):
        monkeypatch.setattr(self.client, "download_package",
                            lambda chart: make_zip({"chart.json": b"{}"}))
        chart = ct.ChartInfo(platform="phira", chart_id=1, title="x")
        with pytest.raises(ct.ExtractionError):
            self.client.download_music(chart)


# ---------------------------------------------------------------------------
# fixtures 解析：Malody
# ---------------------------------------------------------------------------

@pytest.fixture()
def malody():
    return ct.MalodyClient(key="k" * 32, uid=1796800, throttle=None)


class TestMalodyParse:
    def test_song_search(self, malody, monkeypatch):
        monkeypatch.setattr(malody, "_cgi",
                            lambda path, params: load_json("step1_type1.json")["data"])
        songs = malody.search("Sakuzyo")
        assert len(songs) == 132
        first = songs[0]
        assert first.song_id == 536
        assert first.title == "AXION" and first.artist == "Sakuzyo"
        assert first.cover == ("http://cni.machart.top"
                               "/cover/536!small?time=1582298002")
        # cover 为空的条目应保持 None
        empty = next(s for s in songs if s.extra.get("st") == 1 and not s.cover)
        assert empty.cover is None
        assert first.extra["mode_mask"] == 161

    def test_difficulty_list(self, malody, monkeypatch):
        captured = {}

        def fake_cgi(path, params):
            captured.update(path=path, params=params)
            return load_json("step1.json")["data"]

        monkeypatch.setattr(malody, "_cgi", fake_cgi)
        song = ct.SongInfo(platform="malody", song_id=536, title="AXION",
                            artist="Sakuzyo", extra={"_word": "Sakuzyo"})
        charts = malody.charts(song)
        assert captured["path"] == "/cgi/list"
        assert captured["params"] == {"type": 2, "sid": 536, "word": "Sakuzyo"}
        assert len(charts) == 10
        first = charts[0]
        assert first.chart_id == 115257
        assert first.charter == "B-Leaf"
        assert first.difficulty == "4K Easy Lv.7"
        assert first.title == "AXION"                    # 冗余歌曲信息继承

    def test_manifest_pattern(self):
        """file == hash[:12]，hash 为 md5。"""
        data = load_json("step3.json")["data"]
        assert data["sid"] == 536
        for item in data["list"]:
            assert item["file"] == item["hash"][:12]
        names = {i["name"] for i in data["list"]}
        assert names == {"1649685000.mc", "08-axion.jpg", "1605271703.ogg"}

    def test_bgm_locate_from_real_mc(self):
        mc = json.loads((ROOT / "1649685000.mc").read_text(encoding="utf-8"))
        assert ct._malody_bgm_name(mc) == "1605271703.ogg"
        assert mc["meta"]["background"] == "08-axion.jpg"
        assert mc["meta"]["song"]["id"] == 536

    def test_load_mc_zip_wrapped(self):
        raw = (ROOT / "1649685000.mc").read_bytes()
        doc = ct.MalodyClient._load_mc(make_zip({"1649685000.mc": raw}))
        assert doc["meta"]["id"] == 115257

    def test_login_form_and_no_ua(self, monkeypatch):
        captured = {}

        def fake_request(method, url, *, params=None, data=None):
            captured.update(method=method, url=url, data=data)
            return _FakeResponse(200, {"code": 0, "data": {
                "uid": 1796852, "username": "air85",
                "key": "17868153820ea0ab6de2839102327bd3"}})

        client = ct.MalodyClient.__new__(ct.MalodyClient)
        ct._BaseClient.__init__(
            client, {"Referer": "http://m.mugzone.net", "Accept": "*/*",
                     "MaVersion": str(ct.MalodyClient.MAVERSION)})
        client._device_id = "3665630189535747679_0"
        client._name, client._password = "air85", "ceshi123"
        client.key = client.uid = None
        if "user-agent" in client._http.headers:
            del client._http.headers["user-agent"]
        monkeypatch.setattr(client, "_request", fake_request)
        client._do_login()
        # 无盐 md5（与真实抓包逐位一致的已知向量）
        assert captured["data"]["psw"] == "059d38a8c888d5109fa33a9815866013"
        assert captured["data"]["v"] == 262919
        assert captured["data"]["h"] == "3665630189535747679_0"
        assert client.key == "17868153820ea0ab6de2839102327bd3"
        assert client.uid == 1796852
        # 指纹：无 UA、带 MaVersion
        assert client._http.headers.get("user-agent") is None
        assert client._http.headers.get("maVERSION") == "262919"

    def test_cgi_requires_credentials(self):
        with pytest.raises(ct.AuthError):
            ct.MalodyClient(name="u", password=None)  # noqa: 无凭证组合

    def test_auto_relogin_on_code_minus_1(self, monkeypatch):
        client = ct.MalodyClient(name="air85", password="ceshi123", throttle=None)
        calls = {"cgi": 0}

        def fake_do_login():
            client.key, client.uid = "newkey" + "0" * 26, 42

        def fake_request(method, url, *, params=None, data=None):
            if url.endswith("/cgi/login"):
                return _FakeResponse(200, {"code": 0, "data": {
                    "uid": 42, "key": "newkey" + "0" * 26}})
            calls["cgi"] += 1
            return _FakeResponse(200, {"code": -1} if calls["cgi"] == 1
                                 else {"code": 0, "data": []})

        monkeypatch.setattr(client, "_request", fake_request)
        assert client._cgi("/cgi/list", {"type": 1}) == []
        assert client.key == "newkey" + "0" * 26


# ---------------------------------------------------------------------------
# Malody 整链路（离线）：manifest -> 并发下载 -> md5 校验 -> 音乐/曲绘定位
# ---------------------------------------------------------------------------

def manifest_items(contents: dict[str, bytes], uid: int = 1) -> list[dict]:
    """按实测规则构造 manifest 条目：.mc 的 hash=包内 JSON 的 md5
    （CDN 现场打 zip，外层字节不稳定），其余=原始字节 md5；file=hash[:12]。"""
    items = []
    for name, blob in contents.items():
        target = blob
        if name.lower().endswith(".mc"):
            zf = zipfile.ZipFile(io.BytesIO(blob))
            target = zf.read(
                [n for n in zf.namelist() if n.lower().endswith(".mc")][0])
        digest = hashlib.md5(target).hexdigest()
        items.append({"file": digest[:12], "hash": digest, "name": name, "uid": uid})
    return items


class TestMalodyBundle:
    def test_download_bundle_offline(self, malody, monkeypatch):
        """复刻线上行为：.mc 外层 zip 字节与 hash 无关，校验对象是包内 JSON。"""
        mc_raw = (ROOT / "1649685000.mc").read_bytes()
        contents = {
            "1649685000.mc": make_zip({"1649685000.mc": mc_raw}),
            "08-axion.jpg": b"\xff\xd8\xff\xe0" + b"J" * 64,
            "1605271703.ogg": b"OggS" + b"M" * 128,
        }
        items = manifest_items(contents, uid=250541)
        # 外层 zip 的 md5 必然对不上 hash（gui 踩坑现场复现）
        zip_md5 = hashlib.md5(contents["1649685000.mc"]).hexdigest()
        mc_hash = next(i["hash"] for i in items if i["name"].endswith(".mc"))
        assert zip_md5 != mc_hash
        manifest = {"cid": 115257, "sid": 536, "dsid": 536, "uid": 0, "list": items}
        monkeypatch.setattr(malody, "_manifest", lambda chart: manifest)

        def fake_get(url, *, params=None):
            fid = url.rsplit("/", 1)[-1]
            for it in items:
                if it["file"] == fid:
                    return contents[it["name"]]
            raise AssertionError(f"意外的下载 URL: {url}")

        monkeypatch.setattr(malody, "_get_bytes", fake_get)
        chart = ct.ChartInfo(platform="malody", chart_id=115257,
                              title="AXION", artist="Sakuzyo")
        bundle = malody.download_bundle(chart)

        assert bundle.music.format == "ogg"
        assert bundle.music.filename == "AXION.ogg"
        assert bundle.music.data.startswith(b"OggS")   # .mc 的 sound 字段定位成功
        assert bundle.cover is not None
        assert bundle.cover.format == "jpg" and bundle.cover.data[:3] == b"\xff\xd8\xff"
        assert bundle.chart_file.filename == "1649685000.mc"

    def test_md5_mismatch_raises(self, malody, monkeypatch):
        # .mc：包内 JSON 与 hash 不符 -> 报错
        contents = {"x.mc": make_zip({"x.mc": b"{}"})}
        items = manifest_items(contents)
        items[0]["hash"] = "f" * 32                    # 篡改 hash
        monkeypatch.setattr(malody, "_manifest",
                            lambda chart: {"sid": 1, "list": items})
        monkeypatch.setattr(malody, "_get_bytes",
                            lambda url, *, params=None: contents["x.mc"])
        chart = ct.ChartInfo(platform="malody", chart_id=1)
        with pytest.raises(ct.NetworkError, match="校验失败"):
            malody.download_bundle(chart)

    def test_ogg_mismatch_raises(self, malody, monkeypatch):
        # ogg：原始字节 md5 校验，不符 -> 报错
        contents = {"a.mc": make_zip({"a.mc": b'{"meta":{}}'}),
                    "b.ogg": b"OggS-corrupted"}
        items = manifest_items(contents)
        monkeypatch.setattr(malody, "_manifest",
                            lambda chart: {"sid": 1, "list": items})
        monkeypatch.setattr(malody, "_get_bytes",
                            lambda url, *, params=None: b"tampered" * 5)
        chart = ct.ChartInfo(platform="malody", chart_id=1)
        with pytest.raises(ct.NetworkError, match="校验失败"):
            malody.download_bundle(chart)

    def test_fallback_audio_when_sound_missing(self, malody, monkeypatch):
        mc = json.loads((ROOT / "1649685000.mc").read_text(encoding="utf-8"))
        for note in mc["note"]:                    # 抹掉 sound 引用，逼出兜底逻辑
            note.pop("sound", None)
        blob = make_zip({"a.mc": json.dumps(mc).encode()})
        ogg = b"OggS" + b"x" * 32
        contents = {"a.mc": blob, "b.ogg": ogg}
        items = manifest_items(contents)
        monkeypatch.setattr(malody, "_manifest", lambda chart: {"sid": 1, "list": items})
        monkeypatch.setattr(malody, "_get_bytes",
                            lambda url, *, params=None: contents[next(
                                i["name"] for i in items
                                if i["file"] == url.rsplit("/", 1)[-1])])
        chart = ct.ChartInfo(platform="malody", chart_id=1, title="T")
        music = malody.download_music(chart)
        assert music.format == "ogg" and music.data == ogg

    def test_osu_ported_chart_bundle(self, malody, monkeypatch):
        """osu! 移植谱：manifest 无 .mc 有 .osu；URL 中段=manifest.uid（非 0）。"""
        osu_text = (
            b"osu file format v14\r\n\r\n[General]\r\n"
            b"AudioFilename: nine point eight.mp3\r\n"
            b"AudioLeadIn: 0\r\n\r\n[Events]\r\n"
            b'0,0,"maxresdefault.jpg",0,0\r\n'
        )
        contents = {
            "Mili - Nine Point Eight ([S a k u r a ]) [Hard].osu": osu_text,
            "nine point eight.mp3": b"\xff\xfb" + b"M" * 3000,
            "maxresdefault.jpg": b"\xff\xd8\xff" + b"B" * 500,
            "LR_Kick Hard Fast.wav": b"W" * 900,       # key 音，最大的干扰项
            "LR_Snare Clap High.wav": b"S" * 800,      # key 音
        }
        items = manifest_items(contents, uid=5)
        urls = []

        def fake_get(url, *, params=None):
            urls.append(url)
            return contents[next(i["name"] for i in items
                                 if i["file"] == url.rsplit("/", 1)[-1])]

        monkeypatch.setattr(malody, "_manifest",
                            lambda chart: {"sid": 495, "dsid": 0, "uid": 5,
                                           "list": items})
        monkeypatch.setattr(malody, "_get_bytes", fake_get)
        chart = ct.ChartInfo(platform="malody", chart_id=1157,
                              title="Nine Point Eight", artist="Mili")
        bundle = malody.download_bundle(chart)

        # URL 中段是 manifest.uid=5（cid=1157 线上 404 案的复刻）
        assert all("/495/5/" in u for u in urls)
        # 音乐按 .osu AudioFilename 定位 mp3，而非最大的 wav key 音
        assert bundle.music.format == "mp3"
        assert bundle.music.data.startswith(b"\xff\xfb") and len(bundle.music) == 3002
        assert bundle.music.filename == "Nine Point Eight.mp3"
        # 曲绘按 .osu Background 定位
        assert bundle.cover is not None
        assert bundle.cover.format == "jpg" and bundle.cover.data[:3] == b"\xff\xd8\xff"
        # 谱面文件落到 .osu
        assert bundle.chart_file.filename.endswith(".osu")

    def test_osu_ref_helpers(self):
        text = ('[General]\nAudioFilename: My Song.mp3\n\n'
                '[Events]\n0,0,"bg file.jpg",0,0\n')
        assert ct._osu_audio_ref(text) == "My Song.mp3"
        assert ct._osu_background_ref(text) == "bg file.jpg"
        assert ct._osu_audio_ref("[General]\n") is None
        assert ct._osu_background_ref("") is None


# ---------------------------------------------------------------------------
# osu 音乐定位
# ---------------------------------------------------------------------------

class TestOsuExtraction:
    def test_locate_by_osu_audiofilename(self):
        zf = zipfile.ZipFile(io.BytesIO(make_zip({
            "song (artist) - title.osu":
                b"[General]\r\nAudioFilename: title.mp3\r\nMode: 0\r\n",
            "title.mp3": b"\xff\xfb" + b"A" * 200,
            "normal-hitnormal.wav": b"B" * 50,   # key 音
            "soft-hitclap.wav": b"C" * 50,       # key 音
        })))
        chart = ct.ChartInfo(platform="osu", chart_id=1)
        hit = ct.OsuClient._locate_audio(zf, chart)
        assert hit == "title.mp3"

    def test_locate_with_subdir_and_case(self):
        zf = zipfile.ZipFile(io.BytesIO(make_zip({
            "a.osu": b"[General]\nAudioFilename: My Song.MP3\n",
            "sub/My Song.MP3": b"x" * 10,
        })))
        hit = ct.OsuClient._locate_audio(
            zf, ct.ChartInfo(platform="osu", chart_id=1))
        assert hit == "sub/My Song.MP3"

    def test_locate_fallback_largest_mp3(self):
        zf = zipfile.ZipFile(io.BytesIO(make_zip({
            "readme.txt": b"no osu here",
            "big.mp3": b"D" * 300,
            "small key.mp3": b"E" * 20,
        })))
        hit = ct.OsuClient._locate_audio(
            zf, ct.ChartInfo(platform="osu", chart_id=1))
        assert hit == "big.mp3"

    def test_locate_none_raises(self):
        zf = zipfile.ZipFile(io.BytesIO(make_zip({"x.txt": b"nothing"})))
        with pytest.raises(ct.ExtractionError):
            ct.OsuClient._locate_audio(
                zf, ct.ChartInfo(platform="osu", chart_id=1))

    def test_video_scan(self):
        osz = make_zip({
            "a.osu": b"[General]\nAudioFilename: a.mp3\n",
            "a.mp3": b"A",
            "title.avi": b"V" * 100,
            "clip.mp4": b"W" * 40,
        })
        client = ct.OsuClient(cookie="osu_session=x", throttle=None)
        chart = ct.ChartInfo(platform="osu", chart_id=1, title="T")
        video = client.download_video.__doc__  # noqa: F841（仅引用避免误删）
        # monkeypatch 下载层
        client.download_osz = lambda c: osz          # type: ignore[method-assign]
        got = client.download_video(chart)
        assert got.format == "avi" and len(got) == 100   # 取最大者
        empty = make_zip({"a.osu": b"[General]\nAudioFilename: a.mp3\n", "a.mp3": b"A"})
        client.download_osz = lambda c: empty         # type: ignore[method-assign]
        with pytest.raises(ct.NotFoundError):
            client.download_video(chart)

    def test_titled_filename_rules(self):
        # 随手命名 -> 改为曲名（gui 实况：audio.mp3 / world.execute(me);）
        assert ct._titled_filename("audio.mp3", "world.execute(me);", "mp3") \
            == "world.execute(me);.mp3"
        # 'Artist - Title' 含曲名 -> 保留
        assert ct._titled_filename("Mili - world.execute(me);.mp3",
                                    "world.execute(me);", "mp3") \
            == "Mili - world.execute(me);.mp3"
        # 词干与曲名一致 -> 保留
        assert ct._titled_filename("Ga1ahad.mp3", "Ga1ahad", "mp3") == "Ga1ahad.mp3"
        # 大小写/标点差异：归一化后一致 -> 保留
        assert ct._titled_filename("World Execute Me.mp3",
                                    "world.execute(me);", "mp3") \
            == "World Execute Me.mp3"
        # 曲名为空 -> 保留原名
        assert ct._titled_filename("audio.mp3", "", "mp3") == "audio.mp3"
        # 带子目录的成员 -> 只取文件名部分参与判断
        assert ct._titled_filename("sub/audio.mp3", "T", "mp3") == "T.mp3"
        # 曲名全是非法字符被清空 -> 保留原名
        assert ct._titled_filename("x.mp3", "???") == "x.mp3"
        # Unicode 曲名一致 -> 保留
        assert ct._titled_filename("AXION.ogg", "AXION", "ogg") == "AXION.ogg"

    def test_download_music_renames_unrelated_member(self):
        client = ct.OsuClient(cookie="osu_session=x", throttle=None)
        chart = ct.ChartInfo(platform="osu", chart_id=1,
                              title="world.execute(me);", artist="Mili")
        # 曲师随手命名的 audio.mp3
        client.download_osz = lambda c: make_zip({          # type: ignore[method-assign]
            "w.osu": b"[General]\nAudioFilename: audio.mp3\n",
            "audio.mp3": b"\xff\xfb" + b"A" * 100,
        })
        music = client.download_music(chart)
        assert music.filename == "world.execute(me);.mp3"
        assert music.format == "mp3" and len(music) == 102
        # 命名规范的包保持原名
        client.download_osz = lambda c: make_zip({          # type: ignore[method-assign]
            "w.osu": b"[General]\nAudioFilename: Mili - world.execute(me);.mp3\n",
            "Mili - world.execute(me);.mp3": b"\xff\xfb" + b"A" * 100,
        })
        music = client.download_music(chart)
        assert music.filename == "Mili - world.execute(me);.mp3"
        # 视频同样兜底
        client.download_osz = lambda c: make_zip({          # type: ignore[method-assign]
            "w.osu": b"[General]\nAudioFilename: audio.mp3\n",
            "audio.mp3": b"A",
            "video.mp4": b"V" * 50,
        })
        video = client.download_video(chart)
        assert video.filename == "world.execute(me);.mp4"


# ---------------------------------------------------------------------------
# 通用件
# ---------------------------------------------------------------------------

class TestCommon:
    def test_extracted_file_save(self, tmp_path):
        f = ct.ExtractedFile(filename="song.mp3", data=b"123", format="mp3")
        p1 = f.save(tmp_path / "dir")
        assert p1.name == "song.mp3" and p1.read_bytes() == b"123"
        p2 = f.save(tmp_path / "full/override.ogg")
        assert p2.name == "override.ogg" and p2.read_bytes() == b"123"

    def test_safe_filename(self):
        assert ct._safe_filename('a/b\\c:d*e?f"g<h>i|j') != ""
        assert ct._safe_filename("   ") == "untitled"
        assert ct._safe_filename("AXION") == "AXION"

    def test_unbound_objects_raise(self):
        with pytest.raises(ct.CharTunesError):
            ct.ChartInfo(platform="osu", chart_id=1).download_music()
        with pytest.raises(ct.CharTunesError):
            ct.SongInfo(platform="malody", song_id=1).charts()

    def test_osu_requires_cookie(self):
        with pytest.raises(ct.AuthError):
            ct.OsuClient(cookie="")

    def test_sniff_image_ext(self):
        assert ct._sniff_image_ext(b"\x89PNG\r\n\x1a\n....") == "png"
        assert ct._sniff_image_ext(b"\xff\xd8\xff....") == "jpg"
        assert ct._sniff_image_ext(b"RIFF____WEBP") == "webp"
        assert ct._sniff_image_ext(b"garbage") is None

    def test_injection_never_string_concat(self, monkeypatch):
        """三平台关键词均以字典值原样进入 params（由 httpx 负责编码）。"""
        clients = {
            "osu": ct.OsuClient(cookie="osu_session=x", throttle=None),
            "phira": ct.PhiraClient(throttle=None),
        }
        nasty = "x' & q=1 <script>alert(1)</script>"
        for name, client in clients.items():
            captured = {}

            def fake_request(method, url, *, params=None, data=None,
                             _cap=captured):
                _cap.update(url=url, params=params)
                body = (load_json("osu_search.json") if name == "osu"
                        else load_json("phira_chart.json"))
                return _FakeResponse(200, body)

            monkeypatch.setattr(client, "_request", fake_request)
            if name == "osu":
                client.search(nasty)
                assert captured["params"]["q"] == nasty
                assert "?" not in captured["url"]
            else:
                client.search(nasty)
                assert captured["params"]["search"] == nasty

        malody = ct.MalodyClient(key="k", uid=1, throttle=None)
        captured = {}

        def fake_cgi(path, params):
            captured.update(path=path, params=params)
            return []

        monkeypatch.setattr(malody, "_cgi", fake_cgi)
        malody.search(nasty)
        assert captured["params"]["word"] == nasty
