# -*- coding: utf-8 -*-
"""CharTunes 简易 GUI（tkinter + selenium 有头登录）。

用法::

    python gui.py

- 三渠道（osu! / Phira / Malody）搜索与下载；
- 登录：osu! 用原生 tk 对话框粘贴浏览器里抄来的 Cookie
  （登录页有 Turnstile 人机验证，自动化浏览器过不去，但 cookie 到手后
  后续 API 全程免检）；Phira 弹出浏览器（selenium 有头模式）由用户登录后
  收割 cookie（可选）；Malody 用原生 tk 账密表单走 cgi/login；
- 凭证缓存在 ~/.chartunes/state.json（malody 只存 key+uid，不存密码），
  下次启动免登录；
- 下载产物落在 ./downloads/<平台>/。
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import chartunes as ct

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    HAVE_SELENIUM = True
except ImportError:            # GUI 仍可运行，仅浏览器登录不可用
    HAVE_SELENIUM = False

STATE_DIR = Path.home() / ".chartunes"
STATE_FILE = STATE_DIR / "state.json"
LEGACY_STATE_FILE = Path.home() / ".chart2music" / "state.json"   # 旧版目录
DOWNLOAD_DIR = Path("downloads")

PLATFORMS = ["phira", "osu", "malody"]
PLATFORM_CN = {"phira": "Phira", "osu": "osu!", "malody": "Malody"}
# 仅 Phira 走浏览器收割（osu 登录页有 Turnstile，自动化浏览器无法通过）
LOGIN_URL = {"phira": "https://phira.moe/"}


class GuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("CharTunes")
        root.geometry("860x600")
        root.minsize(720, 480)

        self.state = self._load_state()
        self.clients: dict[str, object | None] = {}
        self.registry: dict[str, tuple[str, object]] = {}   # iid -> ("song"|"chart", info)
        self.loaded_songs: set[str] = set()
        self.q: queue.Queue = queue.Queue()
        self._drivers: list = []

        self._build_ui()
        self._refresh_login_labels()
        root.after(80, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="渠道:").grid(row=0, column=0)
        self.platform_var = tk.StringVar(value="phira")
        self.platform_box = ttk.Combobox(
            top, textvariable=self.platform_var, state="readonly",
            values=PLATFORMS, width=8)
        self.platform_box.grid(row=0, column=1, padx=(4, 12))
        self.platform_box.bind("<<ComboboxSelected>>", lambda e: self._clear_results())

        ttk.Label(top, text="关键词:").grid(row=0, column=2)
        self.keyword_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.keyword_var, width=36)
        ent.grid(row=0, column=3, padx=4)
        ent.bind("<Return>", lambda e: self.on_search())

        ttk.Button(top, text="搜索", command=self.on_search).grid(row=0, column=4, padx=4)
        self.cover_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="同时保存曲绘/封面", variable=self.cover_var)\
            .grid(row=0, column=5, padx=8)

        # 登录行
        login = ttk.Frame(self.root, padding=(8, 0))
        login.pack(fill="x")
        ttk.Button(login, text="登录 osu!(粘贴cookie)", command=self.osu_cookie_dialog)\
            .pack(side="left")
        ttk.Button(login, text="登录 Phira(浏览器)", command=lambda: self.selenium_login("phira"))\
            .pack(side="left", padx=6)
        ttk.Button(login, text="Malody 登录", command=self.malody_login_dialog)\
            .pack(side="left")
        self.login_label = ttk.Label(login, text="")
        self.login_label.pack(side="left", padx=12)

        # 结果树
        mid = ttk.Frame(self.root, padding=8)
        mid.pack(fill="both", expand=True)
        cols = ("title", "artist", "extra")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for col, text, w in (("title", "标题", 300), ("artist", "作者", 170),
                             ("extra", "附加信息", 260)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        ttk.Label(mid, text="（Malody：双击歌曲行加载难度；再选中难度行下载）",
                  foreground="gray").pack(side="bottom", anchor="w")

        # 下载按钮
        act = ttk.Frame(self.root, padding=(8, 4))
        act.pack(fill="x")
        ttk.Button(act, text="下载选中", command=self.on_download).pack(side="left")
        ttk.Label(act, text=f"产物目录: {DOWNLOAD_DIR.resolve()}",
                  foreground="gray").pack(side="left", padx=12)

        # 日志
        self.log_box = scrolledtext.ScrolledText(self.root, height=9, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=False, padx=8, pady=(0, 8))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------- 状态存取
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:                                    # 兼容旧版 chart2music 的凭证
            return json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **updates) -> None:
        self.state.update(updates)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def _refresh_login_labels(self) -> None:
        bits = []
        bits.append("osu!: " + ("已登录" if self.state.get("osu_cookie") else "未登录"))
        bits.append("Phira: " + ("已登录(可选)" if self.state.get("phira_cookie") else "免登录"))
        if self.state.get("malody_key"):
            bits.append(f"Malody: 已登录(uid={self.state['malody_uid']})")
        else:
            bits.append("Malody: 未登录")
        self.login_label.config(text="  |  ".join(bits))

    def _platform(self) -> str:
        p = self.platform_var.get()
        return p if p in PLATFORMS else "phira"

    # ------------------------------------------------------------- 线程投递
    def log(self, msg: str, error: bool = False) -> None:
        self.q.put(("log", msg, error))

    def _worker(self, fn) -> None:
        def run():
            try:
                fn()
            except Exception as e:                       # noqa: BLE001 GUI 兜底
                self.log(f"出错：{e}", error=True)
        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", msg[1] + "\n")
                    if msg[2]:
                        self.log_box.tag_add("err", "end-2c linestart", "end-1c")
                        self.log_box.tag_config("err", foreground="#c0392b")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "songs":
                    self._fill_songs(msg[1])
                elif kind == "charts":
                    self._fill_charts(msg[1])
                elif kind == "song_charts":
                    self._append_song_charts(msg[1], msg[2])
                elif kind == "confirm":                  # (msg, holder, event)
                    _, text, holder, ev = msg
                    holder["ok"] = messagebox.askyesno("浏览器登录", text)
                    ev.set()
                elif kind == "state_changed":
                    self._refresh_login_labels()
                elif kind == "ml_ok":                    # malody 登录成功，关窗
                    msg[1].destroy()
                elif kind == "ml_failed":                # 登录失败，恢复按钮
                    msg[1].config(state="normal")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _confirm(self, text: str) -> bool:
        """工作线程向主线程请求模态确认，阻塞等待结果。"""
        holder: dict = {}
        ev = threading.Event()
        self.q.put(("confirm", text, holder, ev))
        ev.wait()
        return bool(holder.get("ok"))

    # ------------------------------------------------------------- 客户端
    def get_client(self, platform: str):
        c = self.clients.get(platform)
        if c is not None:
            return c
        if platform == "osu":
            cookie = self.state.get("osu_cookie")
            if not cookie:
                raise ct.AuthError("osu! 未登录：请点「登录 osu!(浏览器)」")
            c = ct.OsuClient(cookie=cookie)
        elif platform == "phira":
            c = ct.PhiraClient(cookie=self.state.get("phira_cookie") or None)
        else:
            key, uid = self.state.get("malody_key"), self.state.get("malody_uid")
            if not (key and uid):
                raise ct.AuthError("Malody 未登录：请点「Malody 登录」填账密")
            c = ct.MalodyClient(key=key, uid=int(uid))
        self.clients[platform] = c
        return c

    # ------------------------------------------------------------- 搜索
    def on_search(self) -> None:
        platform = self._platform()
        q = self.keyword_var.get().strip()
        if not q:
            return
        self._clear_results()
        self.log(f"[{PLATFORM_CN[platform]}] 搜索：{q}")

        def work():
            client = self.get_client(platform)            # 可能抛 AuthError
            if platform == "malody":
                songs = client.search(q)
                self.q.put(("songs", songs))
                self.log(f"命中 {len(songs)} 首歌（双击行加载难度）")
            elif platform == "osu":
                page = client.search(q, include_covers=True)
                self.q.put(("charts", page.items))
                self.log(f"命中 {len(page.items)} 个谱面集（total={page.total}）")
            else:
                page = client.search(q)
                self.q.put(("charts", page.items))
                self.log(f"命中 {len(page.items)} 条（total={page.total}）")

        self._worker(work)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.registry.clear()
        self.loaded_songs.clear()

    def _fill_songs(self, songs) -> None:
        for s in songs:
            dead = s.extra.get("mode_mask") == 0    # 无谱死条目（服务器侧无任何难度）
            iid = self.tree.insert("", "end", values=(
                s.title + ("　[无谱]" if dead else ""), s.artist,
                f"sid={s.song_id}  bpm={s.bpm}  {s.duration or '?'}s"))
            self.registry[iid] = ("song", s)

    def _fill_charts(self, charts) -> None:
        for c in charts:
            extra = f"id={c.chart_id}"
            if c.platform == "phira" and c.extra.get("level"):
                extra += f"  {c.extra['level']}"
            iid = self.tree.insert("", "end", values=(c.title, c.artist, extra))
            self.registry[iid] = ("chart", c)

    def on_double_click(self, _event) -> None:
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        iid = sel[0]
        entry = self.registry.get(iid)
        if not (entry and entry[0] == "song" and iid not in self.loaded_songs):
            return
        self.loaded_songs.add(iid)
        song = entry[1]
        if song.extra.get("mode_mask") == 0:
            self.log(f"[Malody] {song.title}：服务器侧无任何难度（mode_mask=0 的"
                     f"死条目，uptime=0），换一首试试")
            return
        self.log(f"[Malody] 加载难度：{song.title} (sid={song.song_id})")
        client = None

        def work():
            nonlocal client
            client = self.get_client("malody")
            charts = client.charts(song)
            self.q.put(("song_charts", iid, charts))
            self.log(f"该曲 {len(charts)} 个难度已列出" if charts
                     else f"该曲 0 个难度（服务器侧无谱，换一首试试）")

        self._worker(work)

    def _append_song_charts(self, song_iid: str, charts) -> None:
        if not self.tree.exists(song_iid):
            return
        for c in charts:
            cid = self.tree.insert(song_iid, "end", values=(
                c.difficulty or c.title, c.charter or "",
                f"cid={c.chart_id}  pc={c.extra.get('pc')}"))
            self.registry[cid] = ("chart", c)
        self.tree.item(song_iid, open=True)

    # ------------------------------------------------------------- 下载
    def on_download(self) -> None:
        sel = [self.registry[i] for i in self.tree.selection()
               if i in self.registry]
        charts = [info for kind, info in sel if kind == "chart"]
        songs = [info for kind, info in sel if kind == "song"]
        if songs:
            self.log("提示：Malody 歌曲行需先双击加载难度，请选中难度行下载", error=True)
        if not charts:
            self.log("没有可下载的谱面行", error=True)
            return
        want_cover = self.cover_var.get()

        def work():
            for chart in charts:
                client = self.get_client(chart.platform)
                tag = f"[{PLATFORM_CN[chart.platform]}] {chart.title}"
                if chart.platform == "malody":
                    self.log(f"{tag} 下载整包（cid={chart.chart_id}）…")
                    bundle = client.download_bundle(chart)
                    out = DOWNLOAD_DIR / chart.platform
                    self.log(f"{tag} 音乐 -> {bundle.music.save(out)}")
                    if want_cover and bundle.cover:
                        self.log(f"{tag} 曲绘 -> {bundle.cover.save(out)}")
                else:
                    self.log(f"{tag} 下载中（id={chart.chart_id}）…")
                    music = client.download_music(chart)
                    out = DOWNLOAD_DIR / chart.platform
                    self.log(f"{tag} 音乐 -> {music.save(out)}")
                    if want_cover:
                        try:
                            cover = client.download_cover(chart)
                            self.log(f"{tag} 封面 -> {cover.save(out)}")
                        except ct.CharTunesError as e:
                            self.log(f"{tag} 封面跳过：{e}")
            self.log("全部下载完成。")

        self._worker(work)

    # ------------------------------------------------------------- 登录
    def osu_cookie_dialog(self) -> None:
        """osu! 粘贴 cookie 登录（登录页有 Turnstile，自动化浏览器过不去，
        由用户在正常浏览器登录后抄 Cookie 头贴进来）。"""
        win = tk.Toplevel(self.root)
        win.title("osu! 登录（粘贴 Cookie）")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack()
        ttk.Label(frm, text=(
            "1. 用浏览器打开 https://osu.ppy.sh 并登录（Turnstile 在正常浏览器里可过）\n"
            "2. F12 → Network → 随便点一个请求 → Request Headers\n"
            "3. 复制整行 Cookie: 的值，粘贴到下面："
        ), justify="left").grid(row=0, column=0, sticky="w", pady=(0, 8))
        cookie_var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=cookie_var, width=72)
        entry.grid(row=1, column=0, sticky="we")
        entry.focus_set()
        btn = ttk.Button(frm, text="保存")

        def submit():
            cookie = cookie_var.get().strip()
            # 容错：用户可能把 "Cookie:" 前缀一起抄了
            if cookie.lower().startswith("cookie:"):
                cookie = cookie[7:].strip()
            if "=" not in cookie:
                messagebox.showwarning(
                    "格式不对", "看着不像 Cookie（应形如 name1=value1; name2=value2）",
                    parent=win)
                return
            self._save_state(osu_cookie=cookie)
            self.clients["osu"] = None
            self._refresh_login_labels()
            self.log("[osu!] Cookie 已保存（缓存于 ~/.chartunes/state.json）")
            win.destroy()

        btn.config(command=submit)
        btn.grid(row=2, column=0, pady=(10, 0), sticky="e")
        win.bind("<Return>", lambda e: submit())

    def selenium_login(self, platform: str) -> None:
        if not HAVE_SELENIUM:
            messagebox.showerror(
                "缺少依赖", "未安装 selenium：pip install 'chartunes[gui]'\n"
                "（或 pip install selenium，并确保本机装有 Chrome）")
            return

        def work():
            try:
                driver = webdriver.Chrome()
            except WebDriverException as e:
                self.log(f"启动浏览器失败（需要本机安装 Chrome）：{e}", error=True)
                return
            self._drivers.append(driver)
            try:
                driver.get(LOGIN_URL[platform])
                self.log(f"[{PLATFORM_CN[platform]}] 已打开浏览器，请在页面中完成登录…")
                if not self._confirm(f"请在打开的浏览器中登录 {LOGIN_URL[platform]}，"
                                     "完成后点【是】收割 cookie；点【否】放弃。"):
                    self.log("已取消 Phira 登录")
                    return
                cookies = driver.get_cookies()
                header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                if not header:
                    self.log("未收割到任何 cookie", error=True)
                    return
                key = f"{platform}_cookie"
                self._save_state(**{key: header})
                self.clients[platform] = None
                self.q.put(("state_changed",))
                self.log(f"[{PLATFORM_CN[platform]}] 登录完成，cookie 已保存"
                         f"（{len(cookies)} 项，缓存于 {STATE_FILE}）")
            finally:
                try:
                    driver.quit()
                except WebDriverException:
                    pass
                if driver in self._drivers:
                    self._drivers.remove(driver)

        self._worker(work)

    def malody_login_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Malody 登录")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack()
        ttk.Label(frm, text="用户名:").grid(row=0, column=0, sticky="e", pady=3)
        name_var = tk.StringVar()
        pwd_var = tk.StringVar()
        ttk.Entry(frm, textvariable=name_var, width=24).grid(row=0, column=1, pady=3)
        ttk.Label(frm, text="密码:").grid(row=1, column=0, sticky="e", pady=3)
        ttk.Entry(frm, textvariable=pwd_var, width=24, show="*").grid(row=1, column=1, pady=3)
        btn = ttk.Button(frm, text="登录")

        def submit():
            name, pwd = name_var.get().strip(), pwd_var.get()
            if not (name and pwd):
                return
            btn.config(state="disabled")
            self.log(f"[Malody] 登录中：{name}")

            def work():
                try:
                    client = ct.MalodyClient.login(name, pwd)
                except Exception as e:                       # noqa: BLE001
                    self.log(f"Malody 登录失败：{e}", error=True)
                    self.q.put(("ml_failed", btn))
                    return
                self._save_state(malody_key=client.key, malody_uid=client.uid)
                self.clients["malody"] = client              # 保留账密以便自动重登
                self.q.put(("state_changed",))
                self.q.put(("ml_ok", win))
                self.log(f"[Malody] 登录成功 uid={client.uid}（key 已缓存，不存密码）")

            self._worker(work)

        btn.config(command=submit)
        btn.grid(row=2, column=0, columnspan=2, pady=(10, 0))
        win.bind("<Return>", lambda e: submit())

    # ------------------------------------------------------------- 退出
    def on_close(self) -> None:
        for d in list(self._drivers):
            try:
                d.quit()
            except Exception:                                # noqa: BLE001
                pass
        for c in self.clients.values():
            close = getattr(c, "close", None)
            if close:
                try:
                    close()
                except Exception:                            # noqa: BLE001
                    pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    GuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
