"""Keep production addresses, paths, and fixed values in one place."""

import os
from dataclasses import dataclass
from pathlib import Path

MITM_PORT = 17895
UPSTREAM_PORT = 17896
SUB_SERVER_PORT = 17897
LOCAL_HOST = "127.0.0.1"
LIVE_DIR = Path(
    os.environ.get(
        "APPLETV_ZHSUBS_HOME",
        Path.home() / ".config" / "sing-box" / "mitm",
    )
)
SUBS_DIR = LIVE_DIR / "subs"
REGISTRY_PATH = SUBS_DIR / "registry.json"
CERT_PATH = SUBS_DIR / "leaf-chain.pem"
KEY_PATH = SUBS_DIR / "leaf.key"
LOG_PATH = Path("/tmp/appletv-mitm.log")
SERVER_LOG_PATH = Path("/tmp/appletv-sub-server.log")
MASTER_DUMP_DIR = Path("/tmp/apple-masters")
WORK_DIR = Path("/tmp")

REGION = "sg"
COUNTRY = "sg"
SG_STOREFRONT = "143464"
SUB_BASE = "https://127.0.0.1:17897/s"
UTS_HOST = "uts-api.itunes.apple.com"
PLAYEDGE_HOST = "play-edge.itunes.apple.com"
CONFIGURATION_PATH = "/uts/v3/configurations"
SELFTEST_PATH = "/inj-selftest/master.m3u8"
LANGUAGES = ("zh-Hans", "zh-Hant")
LANGUAGE_NAMES = {"zh-Hans": "简体中文（社区）", "zh-Hant": "繁體中文（社區）"}
# Apple players ignore NAME and render the system-localized name of the
# LANGUAGE tag instead, so provenance has to ride in the tag itself. The
# private-use subtag shows up as "Chinese, Simplified (Private-Use: comm)"
# (verified with AVFoundation), while plain zh-Hans stays indistinguishable
# from an official cmn-Hans track. URIs keep the plain ids.
LANGUAGE_TAGS = {"zh-Hans": "zh-Hans-x-comm", "zh-Hant": "zh-Hant-x-comm"}
HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"
VTT_CONTENT_TYPE = "text/vtt; charset=utf-8"
BIPBOP_VARIANT_URL = (
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
    "bipbop_4x3/gear1/prog_index.m3u8"
)

SUBHD_BASE = "https://subhd.cc"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
NETWORK_TIMEOUT = 20.0
PROCESS_TIMEOUT = 120.0
BSDTAR = "/usr/bin/bsdtar"
FFMPEG = "ffmpeg"
SUBTITLE_ENCODINGS = ("UTF-8", "GB18030")
MIN_PLAYLIST_DURATION = 14400
PLAYLIST_MARGIN = 3600

# Media-playlist pre-warming: play-edge builds each playlist slowly on first
# touch, so the addon completes one fetch per representative playlist through
# the egress before the player asks. See warmer.py for the full story.
WARM_TTL_SECONDS = 1200.0
WARM_TIMEOUT = 90.0
WARM_VIDEO_PER_PATHWAY = 2
WARM_MAX_URLS = 12
DEFAULT_FETCH_TITLE = "The Wolf of Wall Street"
DEFAULT_FETCH_YEAR = "2013"
DEFAULT_FETCH_UMC = "umc.cmc.2tmr7hr6xnw1buspmujtvb9ep"

TEST_MASTER = f"""#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",URI="a.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English CC",LANGUAGE="en",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="{SUB_BASE}/wolf2013/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="简体中文（社区）",LANGUAGE="zh-Hans-x-comm",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="{SUB_BASE}/wolf2013/zh-Hans.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="繁體中文（社區）",LANGUAGE="zh-Hant-x-comm",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="{SUB_BASE}/wolf2013/zh-Hant.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="aud",SUBTITLES="subs"
{BIPBOP_VARIANT_URL}
"""

# This master has no injected tracks. The normal response hook adds them.
SELFTEST_MASTER = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=5000000
{BIPBOP_VARIANT_URL}
"""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Set server resources explicitly; defaults use the live subtitle store."""

    host: str = LOCAL_HOST
    port: int = SUB_SERVER_PORT
    subs_dir: Path = SUBS_DIR
    registry_path: Path = REGISTRY_PATH
    cert_path: Path = CERT_PATH
    key_path: Path = KEY_PATH
    log_path: Path = SERVER_LOG_PATH
    request_timeout: float = NETWORK_TIMEOUT
