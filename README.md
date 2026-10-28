# iptv-aggregator

把多个 m3u 订阅源定时合并成一个固定地址，电视那边只订阅这一个 URL，内容自己滚动更新。

给 `mytv-android`（「我的电视」）这类支持 m3u 订阅的播放器用。

## 用起来

1. 在 GitHub 建一个仓库（public 或 private 都行），把本目录整个推上去
2. 打开 **Actions** 标签页 → `update-playlist` → **Run workflow** 手动跑一次
3. 跑完后订阅地址是：

   ```
   https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/out/playlist.m3u
   ```

4. 把这个地址填进电视上「我的电视」的订阅源（扫码/网页 `http://<电视IP>:10481` → 订阅源）

以后每天 UTC 03:17 / 15:17 自动重建，电视端按缓存时间自己重拉。

## 国内访问 raw.githubusercontent 慢

两个办法，任选：

- **jsDelivr CDN**（推荐，国内一般能通）：

  ```
  https://cdn.jsdelivr.net/gh/<你的用户名>/<仓库名>@main/out/playlist.m3u
  ```

- **GitHub Pages**：仓库 Settings → Pages 选 `main` / root，然后订阅
  `https://<你的用户名>.github.io/<仓库名>/out/playlist.m3u`

## 改上游

只改 `sources.txt`，一行一个 URL，`#` 开头是注释。推送后 workflow 会自动重跑。

想加自家运营商 IPTV 的源，把抓出来的 m3u 地址贴进去就行。

## 命令

```bash
python scripts/merge.py                # 只合并去重
python scripts/merge.py --probe        # 再探活，剔掉连不上的（慢，但有效）
python scripts/merge.py --probe --timeout 4
```

只用标准库，`requirements.txt` 都不用。

## 它怎么去重

按「频道名」做键：去掉 `[HD]`/`(4K)`/`多线路` 这类噪声，统一全角半角、去掉空格和连字符，再 casefold。
同名的只保留先到的那个 —— 所以 `sources.txt` 里**越靠前的源优先级越高**，把最稳的放前面。

## 说明

- 探活只是「能不能连上」，不代表真能播（有些源要特定 UA、要国内网络、要 IPv6）
- 公共清单里的频道大多是**非授权转播**，随时会失效，也有版权风险；`Free-TV/IPTV` 的收录标准是「在该国免费公共播出」，相对干净
- 想长期稳定，还是用**自家宽带运营商的 IPTV 源**或央视/CGTN 这类官方公开流
