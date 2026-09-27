# appletv-zhsubs

Add Simplified and Traditional Chinese subtitle tracks to Apple TV+ playback —
in the official app, with no jailbreak and no player modification.

Apple TV+ decides which subtitle languages a title offers from the storefront
region of the requesting network. Many titles ship without any Chinese track
outside China/HK/TW storefronts. This project works at the local network layer:
it pins the region hints, injects two extra subtitle renditions
(`简体中文` / `繁體中文`) into the HLS master playlist, and serves community
subtitles from a small local HTTPS service — with a per-title timing offset
when a release is not frame-aligned with Apple's edit.

One Mac (or any always-on host) on the LAN serves every device.

## Features

- **Native picker entries, clearly marked.** Injects `zh-Hans` and `zh-Hant`
  renditions (`EXT-X-MEDIA`) into Apple TV HLS masters, so they show up in
  the stock subtitle menu next to the official tracks. Apple players ignore
  the `NAME` attribute and render the system-localized name of the
  `LANGUAGE` tag instead (verified against AVFoundation), so provenance
  rides in the tag itself: injected tracks carry `zh-Hans-x-comm` /
  `zh-Hant-x-comm`, which the menu shows as "Chinese, Simplified
  (Private-Use: comm)" — always distinguishable from an official
  `cmn-Hans` track. `NAME` still carries （社区）/（社區） for players
  that honor it. Note: the effect of a private-use subtag on the player's
  *automatic* preferred-language matching has not been verified; manual
  selection is unaffected.
- **Official tracks win.** Injection is idempotent and skips any language the
  master already carries — Apple's own `cmn-Hans`/`cmn-Hant` renditions
  included — so you never see duplicate or conflicting menu entries.
- **Series aware.** Episode pages are read from `smartEpisode`
  (`showTitle` + `seasonNumber` + `episodeNumber`), the SubHD search leads
  with the exact `SxxEyy` tag, and a release naming a different episode is
  penalized. One title's tracks are never reused for another title or
  episode.
- **Automatic subtitle acquisition.** On the first playback of a title, the
  addon searches SubHD, ranks releases against the title year, downloads the
  archive, extracts it, and converts SRT/ASS to WebVTT with `ffmpeg`. Results
  are cached and keyed by Apple's UMC content id.
- **Live timing offset.** A per-title `offset_seconds` value shifts cues at
  serve time. Fix sync without touching the converted files.
- **Region pinning.** Rewrites the configuration API response so the player
  accepts the injected tracks (storefront `143464`, SG, by default).
- **Media-playlist pre-warming.** play-edge builds each variant/audio media
  playlist slowly on first touch (~20 kB/s cold), and AVPlayer abandons the
  feature before it starts while the warm-CDN interstitial still plays —
  the "ads play, movie does not" failure. The addon completes one fetch per
  representative playlist (per CDN pathway: opening video rungs + default
  audio) through the egress as soon as a master passes by; Apple's cache is
  keyed by asset, not by signed token, so one warm pass speeds up every
  later session.
- **Observable.** Audit log for every rewrite, `/inj-selftest/` endpoint to
  verify injection end to end, and 119 unit tests for the pipeline.

## How it works

```
        Apple TV app
             │  HTTPS: uts-api / play-edge (.itunes.apple.com)
             ▼
   transparent router (sing-box TUN)
             │  route rule: those two hosts → local HTTP proxy
             ▼
   mitmdump :17895  ── appletv_zhsubs.addon ──┐
   · rewrite /uts/v3/configurations          │  on new title:
     (storefront → SG 143464)                │  SubHDFetcher
   · inject zh renditions into              │  search → download →
     play-edge master.m3u8,                  │  extract → ffmpeg → VTT
     URI → https://127.0.0.1:17897/s/…       │
             │                               ▼
             ▼                         subs/ store + registry.json
   upstream proxy :17896 (egress selection unchanged)

   subtitle server :17897 (HTTPS, cert signed by the mitm CA)
   · /s/<key>/<lang>.m3u8 → VTT playlist sized to the feature
   · /s/<key>/<lang>.vtt  → cues, shifted by offset_seconds
```

The player sees ordinary HTTPS responses from Apple hosts (terminated by
mitmproxy) and ordinary subtitle HTTPS from `127.0.0.1` (trusted because the
leaf certificate chains to the same mitmproxy CA the device already trusts).

## Requirements

- macOS host (the extractor calls `/usr/bin/bsdtar`; other Unix needs a small
  change there)
- Python 3.14+
- [mitmproxy](https://mitmproxy.org/) 10+
- `ffmpeg` on `PATH`
- A transparent router that can send two Apple hosts to a local HTTP proxy —
  this deployment uses sing-box TUN, but any equivalent setup works

## Repository layout

| Path | Purpose |
| --- | --- |
| `appletv-zh-subs.py` | Thin loader handed to `mitmdump -s` |
| `appletv_zhsubs/addon.py` | mitmproxy hooks: region rewrite + track injection |
| `appletv_zhsubs/server.py` | Local HTTPS subtitle service (playlists, VTT, offsets) |
| `appletv_zhsubs/fetcher.py` | SubHD search/download/extract/convert pipeline |
| `appletv_zhsubs/hls.py` | HLS master rewriting and VTT playlist generation |
| `appletv_zhsubs/vtt.py` | WebVTT parsing, duration, cue shifting |
| `appletv_zhsubs/registry.py` | UMC → subtitle-set registry with offsets |
| `appletv_zhsubs/config.py` | All ports, paths, and fixed values in one place |
| `tests/` | 119 unit tests, no network, no subprocess |

## Deployment

Example layout uses `~/.config/sing-box/mitm` as the working home; set
`APPLETV_ZHSUBS_HOME` to choose another. Subtitles, the registry, and the TLS
material live under that directory.

### 1. Trust the mitmproxy CA

Run mitmproxy once to create its CA (`~/.mitmproxy` by default), then install
and trust `mitmproxy-ca-cert.pem` on the client device and on the host. This
is the standard mitmproxy step; the subtitle service reuses the same CA, so no
second root is needed.

### 2. Issue the subtitle-server leaf certificate

The subtitle service listens on `https://127.0.0.1:17897`. Sign a leaf for it
with the mitmproxy CA:

```sh
MITM_HOME=~/.config/sing-box/mitm
mkdir -p "$MITM_HOME/subs"
openssl req -new -newkey rsa:2048 -nodes \
  -keyout "$MITM_HOME/subs/leaf.key" -out /tmp/leaf.csr -subj "/CN=localhost"
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\n' > /tmp/leaf.ext
openssl x509 -req -in /tmp/leaf.csr \
  -CA "$MITM_HOME/conf/mitmproxy-ca.pem" \
  -CAkey "$MITM_HOME/conf/mitmproxy-ca.pem" -CAcreateserial \
  -days 825 -extfile /tmp/leaf.ext -out "$MITM_HOME/subs/leaf.crt"
cat "$MITM_HOME/subs/leaf.crt" "$MITM_HOME/conf/mitmproxy-ca.pem" \
  > "$MITM_HOME/subs/leaf-chain.pem"
```

`mitmproxy-ca.pem` contains both the CA certificate and its key. Copy the
`conf/` directory from `~/.mitmproxy` if you keep a dedicated confdir, as in
step 3.

### 3. Run the mitmproxy addon

```sh
mitmdump --listen-host 127.0.0.1 --listen-port 17895 \
  --mode upstream:http://127.0.0.1:17896 \
  --set confdir="$MITM_HOME/conf" \
  --set connection_strategy=lazy \
  -s "$MITM_HOME/appletv-zh-subs.py"
```

The upstream address is optional; without `--mode upstream`, mitmdump connects
directly. Keeping it lets your router retain egress selection for Apple
traffic.

### 4. Run the subtitle server

```sh
PYTHONPATH="$MITM_HOME" python3 -m appletv_zhsubs.server
```

### 5. Route the two Apple hosts through the addon

With sing-box, add an HTTP outbound pointing at the addon, route the two API
hosts to it, and (optionally) give the addon's upstream its own inbound so the
traffic re-enters normal rule evaluation:

```jsonc
{
  "inbounds": [
    { "type": "mixed", "tag": "appletv-mitm-upstream",
      "listen": "127.0.0.1", "listen_port": 17896 }
  ],
  "outbounds": [
    { "type": "http", "tag": "MitM-AppleTV",
      "server": "127.0.0.1", "server_port": 17895 }
  ],
  "route": {
    "rules": [
      { "inbound": "appletv-mitm-upstream", "outbound": "AppleTV-API" },
      { "domain_suffix": ["uts-api.itunes.apple.com",
                          "play-edge.itunes.apple.com"],
        "outbound": "MitM-AppleTV" }
    ]
  }
}
```

`AppleTV-API` here is any egress that presents a region matching the pinned
storefront (Singapore in the default configuration). Media CDN hosts
(`hls*.itunes.apple.com`, `np-edge`, `play.itunes.apple.com`) are **not**
proxied — only the two API hosts pass through mitmproxy.

Both long-running processes are good launchd citizens (`KeepAlive`), but any
process supervisor works.

## Usage

### Verify the injection

```sh
curl --proxy http://127.0.0.1:17895 \
  --cacert "$MITM_HOME/conf/mitmproxy-ca-cert.pem" \
  https://play-edge.itunes.apple.com/inj-selftest/master.m3u8
```

The addon answers this synthetic request itself and runs the result through
the normal injection hook, so the response is a master playlist with the two
Chinese renditions present — proof that rewriting works end to end.

### Fix subtitle timing

Edit `subs/registry.json` and add `"offset_seconds": 1.5` (positive shifts
cues later) to the title's entry. The next VTT request picks it up — no
restart, no file edits.

### Fetch a title manually

```sh
python3 -m appletv_zhsubs.fetcher "Title" 2013 umc.cmc.xxxx
```

## Development

```sh
python3 -m unittest discover -s tests   # 119 tests, offline, loopback only
ruff check .
```

Tests stub mitmproxy before importing the addon and never contact Apple or
SubHD.

## Disclaimer

For personal, non-commercial use on your own network, with a valid Apple TV+
subscription — nothing here grants access to content. Subtitles come from
community uploads on SubHD; their copyright belongs to the respective owners —
make sure you are entitled to use them, and note that automated access to
SubHD may be subject to that site's terms of service. This software does not
decrypt or circumvent any DRM: encrypted media segments never pass through
it; only plain-text API responses and playlist documents are rewritten. This
project is not affiliated with Apple, and "Apple TV" is a trademark of Apple
Inc. You are responsible for complying with the terms of the services you
access.

## License

[MIT](LICENSE)
