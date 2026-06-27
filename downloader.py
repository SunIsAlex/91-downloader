#!/usr/bin/env python3
"""Extract the real HLS stream from a 91cg article and remux it with ffmpeg."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Video:
    url: str
    title: str
    video_id: str


class DPlayerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.videos: list[Video] = []
        self.page_title = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() != "div":
            return
        values = dict(attrs)
        if "dplayer" not in (values.get("class") or "").split():
            return
        raw_config = values.get("data-config")
        if not raw_config:
            return
        try:
            config: dict[str, Any] = json.loads(raw_config)
            video_url = config["video"]["url"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DownloadError(f"DPlayer 的 data-config 无法解析: {exc}") from exc
        if not isinstance(video_url, str) or ".m3u8" not in video_url.lower():
            return
        self.videos.append(
            Video(
                url=video_url,
                title=values.get("data-video_title") or "",
                video_id=values.get("data-video_id") or "",
            )
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            self.page_title = " ".join("".join(self._title_parts).split())

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def extract_videos(document: str) -> tuple[list[Video], str]:
    parser = DPlayerParser()
    parser.feed(document)
    return parser.videos, parser.page_title


def fetch_page(url: str, timeout: float) -> str:
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError(f"页面请求失败: {exc}") from exc
    return response.text


def request_headers(page_url: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": page_url,
        "Origin": origin(page_url),
    }


def fetch_playlist(url: str, page_url: str, timeout: float) -> tuple[str, str]:
    """Return the highest-bandwidth media playlist and its final URL."""
    for _ in range(4):
        try:
            response = requests.get(
                url, timeout=timeout, headers=request_headers(page_url)
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DownloadError(f"m3u8 请求失败: {exc}") from exc
        text = response.text.lstrip("\ufeff")
        if not text.startswith("#EXTM3U"):
            raise DownloadError("服务器返回的内容不是 m3u8")
        variants: list[tuple[int, str]] = []
        lines = text.splitlines()
        for index, line in enumerate(lines[:-1]):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            match = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line)
            next_line = lines[index + 1].strip()
            if next_line and not next_line.startswith("#"):
                variants.append((int(match.group(1)) if match else 0, next_line))
        if not variants:
            return text, response.url
        _, selected = max(variants, key=lambda item: item[0])
        url = urljoin(response.url, selected)
    raise DownloadError("m3u8 主播放列表嵌套过深")


URI_ATTRIBUTE = re.compile(r'URI=(?P<quote>["\'])(?P<url>.*?)(?P=quote)')


def localize_playlist(
    playlist: str, playlist_url: str
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite remote segment/key/map URIs to unique local file names."""
    resources: list[tuple[str, str]] = []
    names: dict[str, str] = {}

    def local_name(remote: str) -> str:
        absolute = urljoin(playlist_url, remote)
        if absolute not in names:
            suffix = Path(urlparse(absolute).path).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
                suffix = ".bin"
            names[absolute] = f"resource-{len(names):06d}{suffix}"
            resources.append((absolute, names[absolute]))
        return names[absolute]

    output: list[str] = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            output.append(local_name(stripped))
        elif "URI=" in line:
            output.append(
                URI_ATTRIBUTE.sub(
                    lambda match: f'URI="{local_name(match.group("url"))}"', line
                )
            )
        else:
            output.append(line)
    return "\n".join(output) + "\n", resources


def download_resources(
    resources: list[tuple[str, str]],
    directory: Path,
    page_url: str,
    workers: int,
    timeout: float,
    retries: int,
) -> None:
    completed = 0
    lock = threading.Lock()

    def download(item: tuple[str, str]) -> None:
        nonlocal completed
        url, filename = item
        error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with requests.get(
                    url,
                    timeout=timeout,
                    headers=request_headers(page_url),
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    with (directory / filename).open("wb") as target:
                        for chunk in response.iter_content(1024 * 256):
                            if chunk:
                                target.write(chunk)
                with lock:
                    completed += 1
                    print(
                        f"\r下载分片: {completed}/{len(resources)}",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                return
            except (requests.RequestException, OSError) as exc:
                error = exc
                (directory / filename).unlink(missing_ok=True)
                if attempt < retries:
                    continue
        raise DownloadError(f"资源下载失败: {url}: {error}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download, item) for item in resources]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except Exception:
            for future in futures:
                future.cancel()
            raise
    print(file=sys.stderr)


def safe_filename(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:120] or "video"


def default_output(video: Video, page_title: str) -> Path:
    title = video.title or re.sub(r"\s*[-–—]\s*91吃瓜网\s*$", "", page_title)
    return Path(f"{safe_filename(title)}.mp4")


def ffmpeg_command(
    ffmpeg: str, video: Video, page_url: str, output: Path, overwrite: bool
) -> list[str]:
    headers = f"Referer: {page_url}\r\nOrigin: {origin(page_url)}\r\n"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-y" if overwrite else "-n",
        "-user_agent",
        USER_AGENT,
        "-headers",
        headers,
        "-i",
        video.url,
        "-map",
        "0",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        str(output),
    ]


def local_ffmpeg_command(
    ffmpeg: str, playlist: Path, output: Path, overwrite: bool
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-y" if overwrite else "-n",
        "-protocol_whitelist",
        "file,crypto,data",
        "-allowed_extensions",
        "ALL",
        "-i",
        str(playlist),
        "-map",
        "0",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        str(output),
    ]


def origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="提取 91cg 文章中的正片 m3u8，并用 ffmpeg 下载、解密和拼接。"
    )
    parser.add_argument("url", help="文章 URL")
    parser.add_argument("-o", "--output", type=Path, help="输出文件（默认从标题生成）")
    parser.add_argument(
        "-i", "--index", type=int, default=1, help="下载第几个 DPlayer 正片（从 1 开始）"
    )
    parser.add_argument("--list", action="store_true", help="只列出发现的正片，不下载")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出文件")
    parser.add_argument("--timeout", type=float, default=30, help="页面请求超时秒数")
    parser.add_argument("-j", "--workers", type=int, default=8, help="并发下载线程数")
    parser.add_argument("--retries", type=int, default=3, help="每个分片的重试次数")
    parser.add_argument(
        "--direct", action="store_true", help="让 ffmpeg 直接下载（关闭 Python 多线程）"
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 命令或路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        document = fetch_page(args.url, args.timeout)
        videos, page_title = extract_videos(document)
        if not videos:
            raise DownloadError("页面中未找到 DPlayer 正片 m3u8")

        if args.list:
            for number, video in enumerate(videos, 1):
                print(f"[{number}] {video.title or video.video_id or '(无标题)'}")
                print(f"    {video.url}")
            return 0

        if args.index < 1 or args.index > len(videos):
            raise DownloadError(f"--index 应在 1 到 {len(videos)} 之间")
        if args.workers < 1 or args.workers > 64:
            raise DownloadError("--workers 应在 1 到 64 之间")
        if args.retries < 0:
            raise DownloadError("--retries 不能小于 0")
        video = videos[args.index - 1]
        output = args.output or default_output(video, page_title)
        if output.exists() and not args.overwrite:
            raise DownloadError(f"输出文件已存在: {output}（可使用 --overwrite）")
        output.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg = shutil.which(args.ffmpeg)
        if not ffmpeg:
            raise DownloadError("找不到 ffmpeg，请先安装或用 --ffmpeg 指定路径")
        print(f"正片: {video.title or video.video_id or video.url}", file=sys.stderr)
        print(f"输出: {output}", file=sys.stderr)
        if args.direct:
            command = ffmpeg_command(ffmpeg, video, args.url, output, args.overwrite)
            result = subprocess.run(command, check=False)
        else:
            playlist, playlist_url = fetch_playlist(
                video.url, args.url, args.timeout
            )
            localized, resources = localize_playlist(playlist, playlist_url)
            print(
                f"资源: {len(resources)} 个，{args.workers} 个下载线程",
                file=sys.stderr,
            )
            with tempfile.TemporaryDirectory(
                prefix=".91-downloader-", dir=output.parent
            ) as temporary:
                temporary_path = Path(temporary)
                playlist_path = temporary_path / "local.m3u8"
                playlist_path.write_text(localized, encoding="utf-8")
                download_resources(
                    resources,
                    temporary_path,
                    args.url,
                    args.workers,
                    args.timeout,
                    args.retries,
                )
                result = subprocess.run(
                    local_ffmpeg_command(
                        ffmpeg, playlist_path, output, args.overwrite
                    ),
                    check=False,
                )
        if result.returncode:
            output.unlink(missing_ok=True)
            raise DownloadError(f"ffmpeg 下载失败（退出码 {result.returncode}）")
        print(f"完成: {output}")
        return 0
    except (DownloadError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
