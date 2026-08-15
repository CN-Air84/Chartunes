# -*- coding: utf-8 -*-
"""CharTunes 真网冒烟脚本（默认不跑任何请求，需显式指定平台与凭证）。

用法示例::

    # Phira（免登录）
    python scripts/smoke.py phira --query "DEADMAN"

    # Malody（账密自动登录；或 --malody-key/--malody-uid）
    python scripts/smoke.py malody --query "Sakuzyo" \
        --malody-user air85 --malody-pwd ceshi123

    # osu!（F12 抄 Cookie 头）
    python scripts/smoke.py osu --query "Sakuzyo" --osu-cookie "osu_session=..."

凭证也可用环境变量：MALODY_USER / MALODY_PWD / MALODY_KEY / MALODY_UID /
OSU_COOKIE / PHIRA_COOKIE。产物默认落在 ./smoke_out/。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chartunes as ct  # noqa: E402

OUT = Path("./smoke_out")


def smoke_phira(query: str, cookie: str | None) -> None:
    with ct.PhiraClient(cookie=cookie) as c:
        page = c.search(query)
        print(f"[phira] 命中 {len(page.items)} 条（total={page.total}）")
        if not page.items:
            return
        first = page.items[0]
        print(f"[phira] 试提取：{first.artist} - {first.title} (id={first.chart_id})")
        p = first.download_music().save(OUT)
        print(f"[phira] 音乐已保存 -> {p}")
        q = c.download_cover(first).save(OUT)
        print(f"[phira] 曲绘已保存 -> {q}")


def smoke_malody(query: str, user: str | None, pwd: str | None,
                 key: str | None, uid: int | None) -> None:
    if key and uid:
        c = ct.MalodyClient(key=key, uid=uid)
    elif user and pwd:
        c = ct.MalodyClient.login(user, pwd)
    else:
        raise SystemExit("malody 需要 --malody-user/--malody-pwd 或 --malody-key/--malody-uid")
    with c:
        print(f"[malody] 登录/凭证 OK：uid={c.uid}")
        songs = c.search(query)
        print(f"[malody] 命中 {len(songs)} 首歌")
        if not songs:
            return
        song = songs[0]
        print(f"[malody] 试提取：{song.artist} - {song.title} (sid={song.song_id})")
        charts = song.charts()
        print(f"[malody] 该曲 {len(charts)} 个难度，取第一条：{charts[0].difficulty}")
        bundle = c.download_bundle(charts[0])
        print(f"[malody] 音乐 {len(bundle.music)}B ({bundle.music.format}) | "
              f"曲绘 {'有' if bundle.cover else '无'}")
        print(f"[malody] 音乐已保存 -> {bundle.music.save(OUT)}")
        if bundle.cover:
            print(f"[malody] 曲绘已保存 -> {bundle.cover.save(OUT)}")


def smoke_osu(query: str, cookie: str) -> None:
    with ct.OsuClient(cookie=cookie) as c:
        page = c.search(query)
        print(f"[osu] 命中 {len(page.items)} 条（total={page.total}，"
              f"cursor={bool(page.cursor)}）")
        if not page.items:
            return
        first = page.items[0]
        print(f"[osu] 试提取：{first.artist} - {first.title} "
              f"(set={first.chart_id})")
        print(f"[osu] 音乐已保存 -> {first.download_music().save(OUT)}")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=["phira", "malody", "osu"])
    ap.add_argument("--query", required=True, help="搜索关键词")
    ap.add_argument("--osu-cookie", default=os.environ.get("OSU_COOKIE"))
    ap.add_argument("--phira-cookie", default=os.environ.get("PHIRA_COOKIE"))
    ap.add_argument("--malody-user", default=os.environ.get("MALODY_USER"))
    ap.add_argument("--malody-pwd", default=os.environ.get("MALODY_PWD"))
    ap.add_argument("--malody-key", default=os.environ.get("MALODY_KEY"))
    ap.add_argument("--malody-uid", type=int,
                    default=int(os.environ["MALODY_UID"])
                    if os.environ.get("MALODY_UID") else None)
    args = ap.parse_args()

    if args.platform == "phira":
        smoke_phira(args.query, args.phira_cookie)
    elif args.platform == "malody":
        smoke_malody(args.query, args.malody_user, args.malody_pwd,
                     args.malody_key, args.malody_uid)
    else:
        if not args.osu_cookie:
            raise SystemExit("osu 需要 --osu-cookie 或环境变量 OSU_COOKIE")
        smoke_osu(args.query, args.osu_cookie)


if __name__ == "__main__":
    main()
