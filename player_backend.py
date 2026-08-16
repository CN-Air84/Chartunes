# -*- coding: utf-8 -*-
"""试听播放子进程（skid 自 YuanyueTTS 的 music_backend.py）。

由 player.MusicPlayer 通过 stdin/stdout 以单行 JSON 协议驱动::

    下行命令：{"action": "play", "path": ..., "start_ms": 0, "play_id": 1}
              {"action": "pause"} / "resume" / "stop" / "quit"
              {"action": "set_volume", "value": 0.0~1.0}
              {"action": "set_pos", "pos_ms": 0}
    上行事件：{"type": "status", "play_id", "pos", "playing", "paused"}
              {"type": "event", "event": "ended", "play_id"}
              {"type": "error", "msg"}
              {"type": "info", "msg", "play_id", "duration_ms"}

与原版的差异：play 直接播放本地文件路径（试听缓存），不做 URL 下载，
也不负责删除文件（缓存归 GUI 管理）；播放成功后附报 duration_ms。
"""
# coding=utf-8
import json
import os
import sys
import threading
import time

# 屏蔽 pygame 的欢迎信息
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
try:
    import pygame
except ImportError:
    print(json.dumps({"type": "error", "msg": "pygame not installed"}), flush=True)
    sys.exit(1)


def _probe_duration_ms(path: str) -> int:
    """探测音频总时长（毫秒）。优先 mutagen（轻量读元数据），
    退化用 pygame.mixer.Sound 整体解码（内存换时长），再不行返回 0。"""
    try:
        from mutagen import File as MutagenFile     # 可选依赖
        meta = MutagenFile(path)
        if meta is not None and meta.info is not None and meta.info.length > 0:
            return int(meta.info.length * 1000)
    except Exception:
        pass
    try:
        sound = pygame.mixer.Sound(path)
        length = int(sound.get_length() * 1000)
        del sound
        return length
    except Exception:
        return 0


class PlaybackBackend:
    REPORT_INTERVAL_SECONDS = 0.2
    END_DETECTION_GRACE_SECONDS = 0.4
    END_CONFIRM_SAMPLES = 2

    def __init__(self):
        pygame.mixer.pre_init(44100, -16, 2, 1024)
        pygame.mixer.init()
        self.is_playing = False
        self.is_paused = False
        self.current_file = None
        self.start_time_offset = 0  # 毫秒
        self.current_pos = 0
        self.volume = 1.0
        self.current_play_id = None
        self._fallback_play_id = 0
        self._play_started_at = None
        self._not_busy_samples = 0
        self._state_lock = threading.Lock()
        self._output_lock = threading.Lock()

        # 状态报告线程
        self.report_thread = threading.Thread(target=self._report_loop, daemon=True)
        self.report_thread.start()

    def _emit(self, payload):
        """以单行 JSON 协议向父进程发送消息。"""
        with self._output_lock:
            print(json.dumps(payload), flush=True)

    def _check_playback_end(self):
        """检测当前播放会话是否自然结束，结束时返回 play_id。"""
        with self._state_lock:
            if not self.is_playing or self.is_paused:
                self._not_busy_samples = 0
                return None
            play_id = self.current_play_id
            started_at = self._play_started_at

        try:
            is_busy = pygame.mixer.music.get_busy()
        except Exception:
            # 查询失败不应被当作自然结束，等待下一次状态轮询。
            return None

        now = time.monotonic()
        with self._state_lock:
            # 查询期间可能发生了停止、切歌或新播放，旧结果必须作废。
            if (
                not self.is_playing
                or self.is_paused
                or self.current_play_id != play_id
            ):
                self._not_busy_samples = 0
                return None

            if is_busy:
                self._not_busy_samples = 0
                return None

            # 刚调用 play() 时 SDL 可能短暂返回 not busy，增加启动宽限期和连续确认。
            if started_at is None or now - started_at < self.END_DETECTION_GRACE_SECONDS:
                return None

            self._not_busy_samples += 1
            if self._not_busy_samples < self.END_CONFIRM_SAMPLES:
                return None

            self.is_playing = False
            self.is_paused = False
            self._not_busy_samples = 0
            return play_id

    def _report_loop(self):
        """定期向父进程报告状态，并发送明确的自然结束事件。"""
        while True:
            ended_play_id = self._check_playback_end()

            with self._state_lock:
                is_playing = self.is_playing
                is_paused = self.is_paused
                play_id = self.current_play_id
                start_time_offset = self.start_time_offset
                current_pos = self.current_pos

            if is_playing:
                try:
                    raw_pos = pygame.mixer.music.get_pos()
                    if raw_pos >= 0:
                        current_pos = start_time_offset + raw_pos
                        with self._state_lock:
                            if self.current_play_id == play_id:
                                self.current_pos = current_pos
                except Exception:
                    pass

            if is_playing or ended_play_id is not None:
                self._emit({
                    "type": "status",
                    "pos": current_pos,
                    "playing": is_playing,
                    "paused": is_paused,
                    "play_id": play_id,
                })

            if ended_play_id is not None:
                self._emit({
                    "type": "event",
                    "event": "ended",
                    "play_id": ended_play_id,
                })

            time.sleep(self.REPORT_INTERVAL_SECONDS)

    def handle_command(self, cmd_json):
        try:
            data = json.loads(cmd_json)
            action = data.get("action")

            if action == "play":
                self._play(
                    data.get("path"),
                    data.get("start_ms", 0),
                    data.get("play_id"),
                )
            elif action == "pause":
                pygame.mixer.music.pause()
                with self._state_lock:
                    if self.is_playing:
                        self.is_paused = True
                    self._not_busy_samples = 0
            elif action == "resume":
                pygame.mixer.music.unpause()
                with self._state_lock:
                    if self.is_playing:
                        self.is_paused = False
                    self._not_busy_samples = 0
            elif action == "stop":
                self._stop()
            elif action == "set_volume":
                self.volume = data.get("value", 1.0)
                pygame.mixer.music.set_volume(self.volume)
            elif action == "set_pos":
                pos_ms = data.get("pos_ms", 0)
                # pygame 的 set_pos 在某些格式上不可靠，最稳妥的是重新播放
                with self._state_lock:
                    current_file = self.current_file
                if current_file:
                    pygame.mixer.music.play(start=pos_ms / 1000.0)
                    with self._state_lock:
                        self.start_time_offset = pos_ms
                        self.current_pos = pos_ms
                        self._play_started_at = time.monotonic()
                        self._not_busy_samples = 0
            elif action == "quit":
                sys.exit(0)

        except SystemExit:
            raise
        except Exception as e:
            self._emit({"type": "error", "msg": str(e)})

    def _play(self, path, start_ms, play_id=None):
        if not path or not os.path.exists(path):
            self._emit({"type": "error", "msg": f"file not found: {path}",
                        "play_id": play_id})
            return

        self._stop()     # 只停播不删文件（缓存文件归 GUI 管理）

        if play_id is None:
            self._fallback_play_id += 1
            play_id = self._fallback_play_id

        try:
            duration_ms = _probe_duration_ms(path)
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play(start=start_ms / 1000.0)
            with self._state_lock:
                self.current_file = path
                self.current_play_id = play_id
                self.start_time_offset = start_ms
                self.current_pos = start_ms
                self.is_playing = True
                self.is_paused = False
                self._play_started_at = time.monotonic()
                self._not_busy_samples = 0
            self._emit({"type": "info", "msg": f"Playing: {path}",
                        "play_id": play_id, "duration_ms": duration_ms})
        except Exception as e:
            self._stop()
            self._emit({"type": "error", "msg": f"Play failed: {e!s}",
                        "play_id": play_id})

    def _stop(self):
        # 先使当前会话失效，避免轮询线程把人为停止误报为自然结束。
        with self._state_lock:
            self.current_file = None
            self.current_play_id = None
            self.is_playing = False
            self.is_paused = False
            self.start_time_offset = 0
            self.current_pos = 0
            self._play_started_at = None
            self._not_busy_samples = 0

        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except Exception:
            pass


if __name__ == "__main__":
    backend = PlaybackBackend()
    for line in sys.stdin:
        backend.handle_command(line)
