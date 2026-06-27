#!/usr/bin/env python3
"""Extract the real HLS stream from a 91cg article and remux it with ffmpeg."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
        result = subprocess.run(
            ffmpeg_command(ffmpeg, video, args.url, output, args.overwrite),
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
