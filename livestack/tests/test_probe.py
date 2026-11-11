import pytest

from probe import Alerter, StaleTracker, parse_master, parse_media

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=3196800,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
720p/index.m3u8

#EXT-X-STREAM-INF:BANDWIDTH=1636800,RESOLUTION=854x480
480p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=985600,RESOLUTION=640x360
360p/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:1762871000
#EXT-X-INDEPENDENT-SEGMENTS
#EXTINF:4.000000,
seg_1762871000.ts
#EXTINF:4.000000,
seg_1762871001.ts
#EXTINF:4.000000,
seg_1762871002.ts
"""


def test_parse_master_resolves_relative_urls():
    v = parse_master(MASTER, "https://live.example.com/hls/live/test/master.m3u8")
    assert [u for _, u in v] == [
        "https://live.example.com/hls/live/test/720p/index.m3u8",
        "https://live.example.com/hls/live/test/480p/index.m3u8",
        "https://live.example.com/hls/live/test/360p/index.m3u8",
    ]
    assert "RESOLUTION=1280x720" in v[0][0]


def test_parse_master_rejects_non_playlist():
    with pytest.raises(ValueError):
        parse_master("<html>502 Bad Gateway</html>", "https://x/master.m3u8")


def test_parse_media():
    pl = parse_media(MEDIA)
    assert pl.media_sequence == 1762871000
    assert pl.target_duration == 4
    assert len(pl.segments) == 3
    assert pl.last_index == 1762871002
    assert not pl.endlist
    assert parse_media(MEDIA + "#EXT-X-ENDLIST\n").endlist


def test_stale_tracker():
    t = StaleTracker(stale_after=20)
    assert t.update("720p", 10, now=0) == 0
    assert t.update("720p", 10, now=15) == 15
    assert not t.is_stale(15)
    age = t.update("720p", 10, now=25)
    assert t.is_stale(age)
    assert t.update("720p", 11, now=26) == 0          # advanced -> fresh again


def test_alerter_dedupes_renotifies_and_resolves():
    sent, clock = [], [0.0]
    a = Alerter(renotify=300, send=lambda m, lvl: sent.append((lvl, m)), clock=lambda: clock[0])
    a.fire("stale:720p", "stale")
    clock[0] = 100
    a.fire("stale:720p", "stale")                    # suppressed
    assert len(sent) == 1
    clock[0] = 301
    a.fire("stale:720p", "stale")                    # re-notify
    assert len(sent) == 2
    a.resolve("stale:720p", "ok")
    assert sent[-1] == ("info", "RESOLVED: ok")
    a.resolve("stale:720p", "ok")                    # no duplicate recovery
    assert len(sent) == 3
