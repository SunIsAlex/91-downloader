# 91cg HLS 下载器

从文章 HTML 的 DPlayer 配置中提取正片 HLS 地址，使用线程池并发下载分片，
再调用 `ffmpeg` 完成 AES-128 解密、拼接和无损封装为 MP4。程序只读取
`data-config.video.url`，会跳过 `video_ads_url`、`backend_video_ads_url` 等广告流。

## 环境

- Python 3.10+
- ffmpeg
- `pip install -r requirements.txt`

Termux 可用 `pkg install python ffmpeg`。

## 使用

```sh
python downloader.py 'https://www.91cg1.com/archives/110156/'
python downloader.py URL -o video.mp4
python downloader.py URL --list
python downloader.py URL --index 2 -o second.mp4
python downloader.py URL --overwrite
python downloader.py URL --workers 16 -o video.mp4
```

默认使用 8 个下载线程，可用 `-j/--workers` 设置为 1–64。单个分片默认重试
3 次，可用 `--retries` 调整。若遇到特殊播放列表，可用 `--direct` 恢复为
ffmpeg 直接下载模式。

地址中的鉴权参数有时效性，因此程序每次都重新请求文章，不应保存并复用旧的
m3u8 地址。下载失败时先重试；若站点启用了额外的 Cloudflare 验证，则普通 HTTP
客户端无法绕过，需要在有权访问的网络环境中处理。

仅下载你有权保存的内容，并遵守所在地法律和网站条款。
