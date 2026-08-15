#### Chartunes
基于Osu！/Malody/Phira自制谱的一站式音乐下载器，

或者正式一点来讲，

## 一套从官网自动**搜索 → 下载 → 解包 → 提取音乐**的 Python 工具链（模块 + GUI）。

---

### 核心亮点

- **一个关键词，三大平台**

    osu! / Phira / Malody 统一流水线与数据模型，调用方零平台知识。

    mugzone那帮人把逻辑搞那么复杂是要干什么我也不明白了。
  
- **识别准确率高**

    健全的文件识别方法，避免将key音识别为乐曲。

    glm说这个特性含金量很高。我都不明白含金量在哪，明明只需要判断下音频长度就行。总之先写上吧。
  
- **全套逆向的私有协议**

    通过逆向分析Malody官网，我们创造出一套完全脱离Malody客户端的谱面下载方法。

    或许大概可能似乎是全网首个第三方malody自制谱下载器。

- **凭证一次到位**

    支持输入Malody账号密码登录、支持登录状态持久化。

    数据完全本地存储，我没有服务器我想私自收集都收集不了。
  
- **工程化不凑合**

    全参数字典化防注入、仿真实客户端指纹（Malody 无 UA + `MaVersion` 头）+ 抖动节流的轻度防风控、

    34 例基于真实抓包的离线测试（不联网即可回归）、单文件模块 + GUI + 冒烟脚本三形态交付。

    你要是觉得这东西还是太玩具的话，我也没招。


### 使用方法

程序本体除了httpx模块外不调用任何第三方模块，我就不写requirement.txt了。

哦对了gui还依赖一个selenium，差点忘提了。

Python 3.10+

（下文基本上就都是glm写的了，不喜勿喷。）

---

**Phira（免登录，最短路径）**

```python
from chartunes import PhiraClient

with PhiraClient() as phira:
    page = phira.search(" ")                 # -> SearchPage
    music = phira.download_music(page.items[0])    # -> ExtractedFile
    cover = phira.download_cover(page.items[0])    # 曲绘
    music.save("./out")          # 目录 -> ./out/<曲名>.mp3；也可传完整文件路径
    # 翻页：phira.search(" ", page=2, page_size=28)
```

**Malody（两段式：歌曲 → 难度 → 整包）**

```python
from chartunes import MalodyClient

with MalodyClient.login("user", "password") as m:      # 或 MalodyClient(key=..., uid=...)
    songs = m.search(" ")          # -> list[SongInfo]（歌曲级）
    charts = songs[0].charts()           # -> list[ChartInfo]（难度级）
    bundle = m.download_bundle(charts[0])    # 三件套并发下载 + md5 校验
    bundle.music.save("./out")               # -> ./out/<曲名>.ogg
    if bundle.cover:                         # 曲绘（若包内附带）
        bundle.cover.save("./out")
    print(m.key, m.uid)                  # 凭证可持久缓存，下次直接 MalodyClient(key, uid)
```

**osu!（需登录 cookie）**

```python
from chartunes import OsuClient

osu_cookie="osu_session……"

#这个智慧过人glm要让用户在调用的时候现填四百多字的cookie。
#with OsuClient(cookie="四百字雷霆大字符串")，这谁教它的。

with OsuClient(cookie=osu_cookie) as osu:
    page = osu.search(" ", include_covers=True, include_preview=True)
    page.items[0].download_music().save("./out")
    osu.download_video(page.items[0])     # 仅显式请求；只扫 avi/mp4
    osu.download_cover(page.items[0])     # 需搜索时 include_covers=True
    osu.download_preview(page.items[0])   # 需搜索时 include_preview=True
    page2 = osu.search("Sakuzyo", cursor=page.cursor)   # cursor_string 翻页
```


---

**返回的数据模型**

| 对象 | 字段/方法 | 说明 |
|---|---|---|
| `SearchPage` | `.items / .total / .cursor` | 一页结果；osu 用 `.cursor` 翻页，Phira 用 `page=` 参数 |
| `SongInfo`（Malody） | `.charts() / .download_cover()` | 歌曲级；`extra["mode_mask"]==0` 为无谱死条目 |
| `ChartInfo` | `.download_music() / .download_cover()` | 谱面级；含曲名/曲师/难度/谱师等元数据 |
| `ExtractedFile` | `.filename / .data / .format / .save(path)` | 提取产物；`save()` 传目录按建议名落盘，传完整路径则原样写入 |
| `MalodyBundle` | `.music / .cover / .chart_file / .manifest` | Malody 整包产物（cover 可能为 `None`） |

所有客户端可调公共参数：`throttle=(0.3, 0.8)` 请求间隔抖动（`None` 关闭）、`timeout`、`max_retries`。

#### 图形界面

不想写代码直接跑 GUI（见下文[简易 GUI](#简易-gui)章节）：选渠道 → 输关键词搜索 →（Malody 双击歌曲行加载难度）→ 选中行点「下载选中」，产物自动保存到 `./downloads/<平台>/`。

这个glm5.3也跟gpt学坏了是吧，怎么也一个劲的说落盘。

#### 命令行冒烟

```bash
python scripts/smoke.py malody --query "Sakuzyo" --malody-user <账号> --malody-pwd <密码>
```

### 凭证获取

#### osu!（必须）

任意浏览器登录 [osu.ppy.sh](https://osu.ppy.sh) → F12 → Network → 随便刷新一个请求 →

Request Headers 里复制完整 `Cookie:` 值传入 `OsuClient(cookie=...)`。

> ⚠️ 未登录时搜索接口会返回“结构正确但内容错误”的结果，模块会尽量识别并抛 `AuthError`，
>
> 但无法百分百兜底——请务必使用已登录会话。

### Malody（必须，二选一）

- **账密自动登录**：`MalodyClient.login(name, password)`——psw 为无盐 md5 上送；

- **手动凭证**：游戏客户端抓包 `cgi/*` 请求 query 里的 `key` 与 `uid`，

  `MalodyClient(key=..., uid=...)`。key 可长期缓存（实测多次登录签发多个 key 且旧 key 不立即作废），
  
  失效时若持有账密会自动重登。

#### Phira

免登录。若遇风控收紧可传 cookie：`PhiraClient(cookie=...)`。

### 防风控（轻度，默认开启）

- 仿真实客户端指纹：osu/Phira 用浏览器 UA + Referer；Malody **无 User-Agent** +
  
  `MaVersion` 头 + `Referer: http://m.mugzone.net`；

- cookie 会话保持（Malody 的 ESA WAF cookie 自动携带）；
- 请求节流：默认每次请求间 0.3–0.8s 随机抖动（`throttle=None` 可关）；
- 超时 / 5xx 指数退避重试（≤3 次）；
- 所有关键词走字典参数编码，杜绝 URL 注入。

- 三渠道搜索与下载，产物保存在 `./downloads/<平台>/`；
- **登录方式**：osu! 用**粘贴 Cookie 对话框**——登录页有 Turnstile 人机验证，
  
  selenium呼出的浏览器过不去；在正常浏览器登录后 F12 抄 Cookie 头贴入即可
  
  （cookie 到手后 API 全程免检）。Phira 点按钮弹出浏览器（selenium 有头模式）

  收割 cookie（免登录也可用，纯可选）；Malody 用原生账密表单；
  
- 凭证缓存在 `~/.chartunes/state.json`（Malody 只存 key+uid，**不存密码**），下次启动免登录；
- 网络操作全部在后台线程，界面不卡顿；日志区实时输出。

## 已知边界 / 待验证

- Malody `h`（device_id）默认沿用抓包值，语义未验证（`device_id=` 参数可改）；
- Malody `MaVersion` 过旧是否被拒未验证（模块常量可改）；
- osz 内视频扩展名实际分布未全面统计（仅扫 avi/mp4）；
- Phira 曲绘真实格式以 magic bytes 判定，缺省按 png 保存。

仅用于个人学习与备份自己游玩的谱面音乐，请尊重各平台条款与版权。

该睡觉了，886。有事LD喊我，或者给我发邮件。

