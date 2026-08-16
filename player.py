# -*- coding: utf-8 -*-
"""试听播放器管理（skid 自 YuanyueTTS music_NCM.py 的 MusicPlayer）。

拉起独立子进程 player_backend.py 播放本地缓存音频，父进程只经单行
JSON 协议通信；play_id 会话号用于丢弃陈旧/重复的"自然结束"事件，
防止误触发自动切歌。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading

log = logging.getLogger("chartunes.player")


class MusicPlayer:
    """本地试听播放器：管理 player_backend 子进程的完整生命周期。"""

    def __init__(self):
        self.is_playing = False
        self.is_paused = False
        self.volume = 1.0
        self.current_pos = 0        # 毫秒
        self.duration_ms = 0        # 当前曲目总时长（backend 上报；0 = 未知）
        self.backend_proc = None
        self._command_lock = threading.Lock()
        self._play_sequence = 0
        self.current_play_id = None
        self._last_ended_play_id = None
        self.auto_next_callback = lambda: None
        self._start_backend()

    # ------------------------------------------------------------ 子进程管理
    def _start_backend(self) -> None:
        """启动后台播放进程（隐藏 Windows 控制台窗口）。"""
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "player_backend.py")
            if not os.path.exists(script):
                log.error("找不到 player_backend.py：%s", script)
                return
            args = [sys.executable, script]

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            self.backend_proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                startupinfo=startupinfo,
            )
            threading.Thread(target=self._read_backend_output, daemon=True).start()
            log.info("试听播放后台已启动 (pid=%s)", self.backend_proc.pid)
        except Exception as e:                                  # noqa: BLE001
            log.error("启动播放后台失败：%s", e)

    def _read_backend_output(self) -> None:
        """后台线程：读取子进程 stdout 的 JSON 消息并更新本地状态。"""
        while self.backend_proc and self.backend_proc.poll() is None:
            line = self.backend_proc.stdout.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                message_type = data.get("type")

                if message_type == "status":
                    status_play_id = data.get("play_id")
                    # 丢弃旧播放会话延迟到达的状态帧
                    if status_play_id is not None and status_play_id != self.current_play_id:
                        continue
                    self.current_pos = data.get("pos", 0)
                    self.is_playing = data.get("playing", False)
                    self.is_paused = data.get("paused", False)
                elif message_type == "event" and data.get("event") == "ended":
                    self._handle_playback_ended(data.get("play_id"))
                elif message_type == "info":
                    duration = data.get("duration_ms")
                    if data.get("play_id") == self.current_play_id and duration:
                        self.duration_ms = int(duration)
                elif message_type == "error":
                    log.error("播放后台错误：%s", data.get("msg"))
            except Exception as e:                              # noqa: BLE001
                log.warning("忽略无法解析的后台消息：%s", e)

    def _handle_playback_ended(self, play_id) -> None:
        """自然结束事件只触发一次自动切歌。"""
        if play_id is None or play_id != self.current_play_id:
            return
        if self._last_ended_play_id == play_id:
            return

        self._last_ended_play_id = play_id
        self.is_playing = False
        self.is_paused = False
        # 延迟半秒再自动切歌，给"用户抢先点了下一首"留出作废窗口
        threading.Timer(0.5, self._run_auto_next, args=(play_id,)).start()

    def _run_auto_next(self, play_id) -> None:
        if play_id != self.current_play_id:
            return                      # 会话已切换，任务作废
        if self.is_playing or self.is_paused:
            return                      # 播放状态又被用户改变，取消自动切歌
        try:
            self.auto_next_callback()
        except Exception as e:                                  # noqa: BLE001
            log.error("自动切歌回调失败：%s", e)

    # ---------------------------------------------------------------- 命令
    def _send_cmd(self, action: str, **kwargs) -> bool:
        """向子进程发一条 JSON 命令（写入互斥，防止多行交错）。"""
        with self._command_lock:
            if not self.backend_proc or self.backend_proc.poll() is not None:
                log.warning("播放后台已退出，尝试重启…")
                self._start_backend()
            if not self.backend_proc or self.backend_proc.poll() is not None:
                log.error("播放后台不可用，命令未发送")
                return False

            cmd = {"action": action}
            cmd.update(kwargs)
            try:
                self.backend_proc.stdin.write(json.dumps(cmd) + "\n")
                self.backend_proc.stdin.flush()
                return True
            except Exception as e:                              # noqa: BLE001
                log.error("发送播放命令失败：%s", e)
                return False

    # -------------------------------------------------------------- 公共 API
    def play_path(self, path: str, start_ms: int = 0) -> int:
        """播放本地文件，返回本次播放的会话号 play_id。"""
        self._play_sequence += 1
        play_id = self._play_sequence
        self.current_play_id = play_id
        self._last_ended_play_id = None
        self.current_pos = start_ms
        self.duration_ms = 0
        self.is_playing = True
        self.is_paused = False
        if not self._send_cmd("play", path=path, start_ms=start_ms, play_id=play_id):
            self.is_playing = False
        return play_id

    def pause(self) -> None:
        self._send_cmd("pause")
        self.is_paused = True

    def resume(self) -> None:
        self._send_cmd("resume")
        self.is_paused = False

    def stop(self) -> None:
        # 先作废当前会话，再下发 stop，让排队中的 ended 事件自然失效
        self.current_play_id = None
        self._last_ended_play_id = None
        self.is_playing = False
        self.is_paused = False
        self.current_pos = 0
        self.duration_ms = 0
        self._send_cmd("stop")

    def set_volume(self, val: float) -> None:
        self.volume = max(0.0, min(1.0, val))
        self._send_cmd("set_volume", value=self.volume)

    def set_pos(self, pos_ms: int) -> None:
        self._send_cmd("set_pos", pos_ms=pos_ms)
        self.current_pos = pos_ms

    def get_pos(self) -> int:
        return self.current_pos

    def shutdown(self) -> None:
        """退出子进程（窗口关闭时调用）。"""
        if self.backend_proc:
            try:
                self._send_cmd("quit")
                self.backend_proc.terminate()
            except Exception:                                   # noqa: BLE001
                pass
            self.backend_proc = None
