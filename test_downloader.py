import unittest
from pathlib import Path

from downloader import (
    Video,
    default_output,
    extract_videos,
    ffmpeg_command,
    localize_playlist,
)


class DownloaderTests(unittest.TestCase):
    def test_extracts_content_video_and_ignores_ads(self):
        document = """<html><head><title>Page - 91吃瓜网</title></head><body>
        <div class="foo dplayer bar" data-video_id="42" data-video_title="A/B"
          data-config='{"video_ads_url":["https://ad/a.m3u8"],
          "video":{"url":"https://cdn/content.m3u8?token=x","type":"hls"}}'></div>
        </body></html>"""
        videos, title = extract_videos(document)
        self.assertEqual([v.url for v in videos], ["https://cdn/content.m3u8?token=x"])
        self.assertEqual(videos[0].video_id, "42")
        self.assertEqual(title, "Page - 91吃瓜网")
        self.assertEqual(default_output(videos[0], title), Path("A_B.mp4"))

    def test_ffmpeg_uses_stream_copy_and_page_headers(self):
        command = ffmpeg_command(
            "ffmpeg", Video("https://cdn/v.m3u8", "v", "1"),
            "https://example.com/post/1", Path("out.mp4"), False
        )
        self.assertIn("copy", command)
        self.assertIn("Referer: https://example.com/post/1", "\n".join(command))
        self.assertIn("-n", command)

    def test_localizes_segments_key_and_map(self):
        playlist = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x01
#EXT-X-MAP:URI='init.mp4'
#EXTINF:2,
segment-1.ts?token=x
#EXTINF:2,
segment-2.ts
#EXT-X-ENDLIST
"""
        localized, resources = localize_playlist(
            playlist, "https://cdn.example/path/index.m3u8"
        )
        self.assertNotIn("https://", localized)
        self.assertIn('URI="resource-000000.bin"', localized)
        self.assertIn('URI="resource-000001.mp4"', localized)
        self.assertIn("resource-000002.ts", localized)
        self.assertEqual(len(resources), 4)


if __name__ == "__main__":
    unittest.main()
