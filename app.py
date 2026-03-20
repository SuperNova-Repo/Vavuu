#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vavuu Web Player – HuggingFace Space
Vollständige FastAPI-App ohne Gradio-UI-Wrapper.
/ → eigene HTML-Seite mit Player + Dropdowns
/api/groups  → JSON Länderliste
/api/channels?group=... → JSON Senderliste
/api/resolve?group=...&channel=...&retry=N → JSON {url, total}
/proxy?url=... → CORS-Stream-Proxy
/hlsjs → hls.js same-origin
"""

import os, re, json, time, uuid, logging, urllib.parse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
import gradio as gr

# ═════════════════════════════════════════════════════════════════════════════
# KONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

REQUEST_TIMEOUT = 15
MAX_RETRIES     = 3
AUTH_CACHE_TTL  = 3600

AUTH_API_URL  = "https://www.vavoo.tv/api/app/ping"
HANDSHAKE_URL = "https://www.vavoo.to/mediahubmx.json"
CATALOG_URL   = "https://www.vavoo.to/mediahubmx-catalog.json"
RESOLVE_URL   = "https://www.vavoo.to/mediahubmx-resolve.json"
INDEX_URL     = "https://www2.vavoo.to/live2/index"

UA_ELECTRON = "electron-fetch/1.0 electron (+https://github.com/arantes555/electron-fetch)"
UA_MEDIAHUB = "MediaHubMX/2"
UA_STREAM   = "libmpv"

_space_host = os.environ.get("SPACE_HOST", "")
PROXY_BASE  = f"https://{_space_host}" if _space_host else ""

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)-8s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("vavuu")
logger.info(f"SPACE_HOST={_space_host!r}")

# ═════════════════════════════════════════════════════════════════════════════
# HTTP SESSION
# ═════════════════════════════════════════════════════════════════════════════

def _make_session():
    s = requests.Session()
    a = HTTPAdapter(max_retries=Retry(
        total=MAX_RETRIES, backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]))
    s.mount("https://", a); s.mount("http://", a)
    s.headers.update({"User-Agent": UA_ELECTRON, "Accept": "*/*",
                       "Accept-Language": "de", "Accept-Encoding": "gzip, deflate",
                       "Connection": "close"})
    return s

SESSION = _make_session()

# ═════════════════════════════════════════════════════════════════════════════
# AUTH
# ═════════════════════════════════════════════════════════════════════════════

_auth_cache     = {"signature": None, "expires": 0}
_handshake_done = False

def get_auth_signature():
    global _auth_cache
    if _auth_cache["signature"] and time.time() < _auth_cache["expires"]:
        return _auth_cache["signature"]
    logger.info("Hole Auth-Signatur ...")
    payload = {
        "token": (
            "8Us2TfjeOFrzqFFTEjL3E5KfdAWGa5PV3wQe60uK4BmzlkJRMYFu0ufaM_eeDXKS2U04XUuhbD"
            "TgGRJrJARUwzDyCcRToXhW5AcDekfFMfwNUjuieeQ1uzeDB9YWyBL2cn5Al3L3gTnF8Vk1t7rP"
            "wkBob0swvxA"
        ),
        "reason": "player.enter", "locale": "de", "theme": "dark",
        "metadata": {
            "device": {"type": "Desktop", "brand": "Unknown", "model": "Unknown",
                       "name": "Unknown", "uniqueId": uuid.uuid4().hex[:16]},
            "os":     {"name": "windows", "version": "10.0.22631",
                       "abis": [], "host": "electron"},
            "app":    {"platform": "electron", "version": "3.1.4",
                       "buildId": "288045000", "engine": "jsc",
                       "signatures": [], "installer": "unknown"},
            "version": {"package": "tv.vavoo.app", "binary": "3.1.4", "js": "3.1.4"},
        },
        "appFocusTime": 27229, "playerActive": True, "playDuration": 0,
        "devMode": False, "hasAddon": False, "castConnected": False,
        "package": "tv.vavoo.app", "version": "3.1.4", "process": "app",
        "firstAppStart": int(time.time() * 1000) - 86_400_000,
        "lastAppStart":  int(time.time() * 1000),
        "ipLocation": "", "adblockEnabled": False,
        "proxy": {"supported": ["ss"], "engine": "ss", "enabled": False,
                  "autoServer": True, "id": "ca-bhs"},
        "iap": {"supported": True},
    }
    try:
        r = SESSION.post(AUTH_API_URL, json=payload,
                         headers={"User-Agent": UA_ELECTRON, "Accept": "*/*",
                                  "content-type": "application/json; charset=utf-8",
                                  "Connection": "close"},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        sig = r.json().get("addonSig")
        if sig:
            _auth_cache.update({"signature": sig,
                                 "expires": time.time() + AUTH_CACHE_TTL})
            logger.info("Auth OK"); return sig
    except Exception as e:
        logger.error(f"Auth: {e}")
    return None

def _api_headers(sig):
    h = {"accept": "*/*", "user-agent": UA_MEDIAHUB,
         "Accept-Language": "de", "Accept-Encoding": "gzip, deflate",
         "content-type": "application/json; charset=utf-8",
         "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "no-cors",
         "Sec-Fetch-Dest": "empty", "Connection": "close"}
    if sig: h["mediahubmx-signature"] = sig
    return h

def do_handshake(sig):
    global _handshake_done
    if _handshake_done: return
    h = _api_headers(sig)
    for pl in [{"language": "de", "region": "AT", "clientVersion": "3.1.4"},
               {"language": "de", "clientVersion": "3.1.4"}]:
        try: SESSION.post(HANDSHAKE_URL, data=json.dumps(pl), headers=h, timeout=REQUEST_TIMEOUT)
        except Exception: pass
    _handshake_done = True

# ═════════════════════════════════════════════════════════════════════════════
# KANAL-LOGIK
# ═════════════════════════════════════════════════════════════════════════════

def norm(name):
    n = re.sub(r'\s+\.[a-zA-Z]+$', '', name.strip())
    return re.sub(r'\s+\(\d+\)$', '', n)

def fetch_index():
    try:
        r = SESSION.get(INDEX_URL, params={"output": "json"}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status(); d = r.json()
        return d if isinstance(d, list) else []
    except Exception as e:
        logger.error(f"Index: {e}"); return []

def count_per_group(index):
    c = {}
    for it in index:
        g = it.get("group"); n = norm((it.get("name") or "").strip())
        if g and n: c.setdefault(g, set()).add(n)
    return {g: len(s) for g, s in c.items()}

def fetch_catalog(group, sig):
    h = _api_headers(sig); ch = {}; cursor = 0; seen = set()
    for _ in range(100):
        pl = {"language": "de", "region": "AT", "catalogId": "vto-iptv",
              "id": "vto-iptv", "adult": False, "search": "", "sort": "name",
              "filter": {"group": group}, "cursor": cursor,
              "count": 9999, "clientVersion": "3.1.4"}
        try:
            r = SESSION.post(CATALOG_URL, data=json.dumps(pl), headers=h, timeout=REQUEST_TIMEOUT)
            r.raise_for_status(); result = r.json()
        except Exception as e:
            logger.warning(f"Catalog: {e}"); break
        items = result.get("items", [])
        if not items: break
        for it in items:
            raw = (it.get("name") or "").strip(); url = it.get("url")
            if raw and url:
                k = norm(raw); ch.setdefault(k, [])
                if url not in ch[k]: ch[k].append(url)
        nc = result.get("nextCursor")
        if not nc or str(nc) in seen: break
        seen.add(str(nc)); cursor = nc
    return ch

def supplement_index(group, ch, index):
    SP = {1: 0, 6: 1, 7: 2}; pending = {}
    for it in index:
        if it.get("group") != group: continue
        raw = (it.get("name") or "").strip(); url = it.get("url")
        if not raw or not url: continue
        m = re.search(r'\((\d+)\)$', raw)
        prio = SP.get(int(m.group(1)) if m else 99, 99)
        pending.setdefault(norm(raw), []).append((prio, url))
    for k, pu in pending.items():
        pu.sort(key=lambda x: x[0]); ch.setdefault(k, [])
        for _, url in pu:
            if url not in ch[k]: ch[k].append(url)

def get_channels(group, sig, index):
    do_handshake(sig)
    ch = fetch_catalog(group, sig)
    supplement_index(group, ch, index)
    return ch

# ═════════════════════════════════════════════════════════════════════════════
# RESOLVE
# ═════════════════════════════════════════════════════════════════════════════

def _is_interstitial(html):
    return any(m in html.lower() for m in
               ["lokke.app", "willst du kostenlos", "weiterschauen",
                "lade den", "browser herunter"])

def follow_url(url):
    sh = {"User-Agent": UA_STREAM, "Accept": "*/*",
          "Accept-Encoding": "gzip, deflate", "Connection": "close"}
    try:
        r = SESSION.get(url, headers=sh, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if any(s in ct.lower() for s in ["mpegurl", "video/", "octet-stream"]):
            return r.url
        if "text/html" in ct:
            for pat in [r'["\'](https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)["\']',
                        r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)']:
                hits = re.findall(pat, r.text)
                if hits: return hits[0]
            if _is_interstitial(r.text): return None
        return r.url
    except Exception as e:
        logger.debug(f"follow_url: {e}"); return url

def resolve_url(link, sig):
    do_handshake(sig)
    try:
        r = SESSION.post(RESOLVE_URL,
                         data=json.dumps({"language": "de", "region": "AT",
                                          "url": link, "clientVersion": "3.1.4"}),
                         headers=_api_headers(sig), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        resolved = r.json()[0]["url"]
        if any(d in resolved for d in ["lokke.app", "lokke.to"]): return None
        return follow_url(resolved)
    except Exception as e:
        logger.warning(f"Resolve: {e}"); return None

# ═════════════════════════════════════════════════════════════════════════════
# APP-STATE
# ═════════════════════════════════════════════════════════════════════════════

_state = {"index": [], "groups": [], "counts": {}, "signature": None}
_channels_cache = {}
_hlsjs_cache: bytes | None = None

def _init():
    sig = get_auth_signature()
    index = fetch_index()
    cnt = count_per_group(index)
    grps = sorted({it.get("group") for it in index if it.get("group")})
    _state.update({"index": index, "groups": grps, "counts": cnt, "signature": sig})
    logger.info(f"Init OK – {len(grps)} Gruppen")

_init()

# ═════════════════════════════════════════════════════════════════════════════
# FRONTEND HTML
# ═════════════════════════════════════════════════════════════════════════════

FRONTEND = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📺 Vavuu Web Player</title>
<style>
  :root {
    --bg: #0d0d0d; --surface: #161616; --border: #2a2a2a;
    --text: #e0e0e0; --muted: #666; --accent: #e05050;
    --green: #4caf50; --radius: 10px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:system-ui,sans-serif;
         min-height:100vh; display:flex; flex-direction:column; }
  header { padding:16px 24px; border-bottom:1px solid var(--border);
            display:flex; align-items:center; gap:10px; }
  header h1 { font-size:1.2em; font-weight:700; }
  header .sub { color:var(--muted); font-size:.82em; margin-left:auto; }

  #player-wrap {
    background:#000; position:relative;
    aspect-ratio:16/9; max-height:60vh;
    display:flex; align-items:center; justify-content:center;
  }
  #player-wrap video { width:100%; height:100%; display:block; }
  #overlay {
    position:absolute; inset:0; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:12px;
    color:var(--muted); font-size:1em; pointer-events:none;
    transition:opacity .3s;
  }
  #overlay .icon { font-size:2.5em; }
  #overlay.hidden { opacity:0; }

  #titlebar {
    background:var(--surface); padding:8px 16px; font-size:.85em;
    border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px;
    min-height:36px;
  }
  #titlebar .dot { color:var(--accent); }
  #titlebar #ch-name { font-weight:600; }
  #titlebar .live { margin-left:auto; color:var(--muted); font-size:.78em; }

  .controls { padding:16px 24px; display:flex; flex-direction:column; gap:12px; }

  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .field { flex:1; min-width:200px; display:flex; flex-direction:column; gap:4px; }
  label { font-size:.78em; color:var(--muted); text-transform:uppercase;
           letter-spacing:.04em; }
  select {
    background:var(--surface); color:var(--text); border:1px solid var(--border);
    border-radius:var(--radius); padding:10px 12px; font-size:.95em;
    cursor:pointer; outline:none; appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23666' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
    background-repeat:no-repeat; background-position:right 12px center;
    padding-right:32px;
  }
  select:focus { border-color:#555; }
  select:disabled { opacity:.4; cursor:not-allowed; }

  #status-bar {
    padding:6px 24px; font-size:.8em; color:var(--muted);
    border-top:1px solid var(--border); background:var(--surface);
    display:flex; align-items:center; gap:8px; min-height:32px;
  }
  #status-bar .spinner { display:none; width:14px; height:14px;
    border:2px solid #333; border-top-color:var(--accent);
    border-radius:50%; animation:spin .7s linear infinite; }
  #status-bar.loading .spinner { display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }

  #url-display {
    padding:8px 24px 16px; font-size:.75em; color:#4a9; font-family:monospace;
    word-break:break-all; line-height:1.4; display:none;
  }
  #url-display.visible { display:block; }

  @media(max-width:500px) {
    .row { flex-direction:column; }
    header { padding:12px 16px; }
    .controls { padding:12px 16px; }
  }
</style>
</head>
<body>

<header>
  <span style="font-size:1.4em;">📺</span>
  <h1>Vavuu Web Player</h1>
  <span class="sub">Live-TV</span>
</header>

<div id="titlebar">
  <span class="dot">●</span>
  <span id="ch-name">Sender wählen ...</span>
  <span class="live" id="live-badge"></span>
</div>

<div id="player-wrap">
  <video id="video" controls playsinline></video>
  <div id="overlay">
    <span class="icon">📺</span>
    <span id="overlay-msg">Land → Sender wählen</span>
  </div>
</div>

<div id="hls-log" style="display:none;background:#1a0a0a;color:#f88;
  font-family:monospace;font-size:.75em;padding:8px 16px;
  max-height:120px;overflow-y:auto;border-top:1px solid #400;"></div>
<div id="status-bar">
  <div class="spinner"></div>
  <span id="status-msg">Bereit.</span>
</div>

<div class="controls">
  <div class="row">
    <div class="field">
      <label>🌍 Land</label>
      <select id="group-dd"><option value="">– Land wählen –</option></select>
    </div>
    <div class="field">
      <label>📡 Sender</label>
      <select id="channel-dd" disabled><option value="">– zuerst Land wählen –</option></select>
    </div>
  </div>
</div>

<div id="url-display"></div>

<script src="/hlsjs"></script>
<script>
const groupDd   = document.getElementById('group-dd');
const channelDd = document.getElementById('channel-dd');
const video     = document.getElementById('video');
const overlay   = document.getElementById('overlay');
const overlayMsg= document.getElementById('overlay-msg');
const statusBar = document.getElementById('status-bar');
const statusMsg = document.getElementById('status-msg');
const chName    = document.getElementById('ch-name');
const liveBadge = document.getElementById('live-badge');
const urlDisplay= document.getElementById('url-display');
const hlsLog    = document.getElementById('hls-log');

// ── Fehlertypen die SOFORT zum nächsten URL führen (kein recoverMediaError) ──
// fragParsingError = Codec/Strukturproblem, recover hilft hier NIE
const FATAL_NO_RECOVER = new Set([
  'fragParsingError',
  'fragDecryptError',
]);

let currentHls     = null;
let _playStarted   = false;   // verhindert doppelten play()-Aufruf bei recovery
let totalUrls      = 0;       // vom Server übermittelte Gesamt-URL-Anzahl

function setStatus(msg, loading = false) {
  statusMsg.textContent = msg;
  statusBar.classList.toggle('loading', loading);
}
function showOverlay(msg) {
  overlayMsg.textContent = msg;
  overlay.classList.remove('hidden');
}
function hideOverlay() {
  overlay.classList.add('hidden');
}
function logHls(msg) {
  hlsLog.style.display = 'block';
  hlsLog.innerHTML += `<div>${msg}</div>`;
  hlsLog.scrollTop = hlsLog.scrollHeight;
}

// ── Gruppen laden ──────────────────────────────────────────────────────────
async function loadGroups() {
  setStatus('Lade Länder ...', true);
  try {
    const r    = await fetch('/api/groups');
    const data = await r.json();
    data.groups.forEach(g => {
      const opt = document.createElement('option');
      opt.value = g.name;
      opt.textContent = `${g.name}  (${g.count} Sender)`;
      groupDd.appendChild(opt);
    });
    setStatus(`${data.groups.length} Länder geladen.`);
  } catch (e) { setStatus('Fehler: ' + e); }
}

// ── Sender laden ───────────────────────────────────────────────────────────
groupDd.addEventListener('change', async function () {
  const group = this.value;
  channelDd.innerHTML = '<option value="">Lädt ...</option>';
  channelDd.disabled  = true;
  urlDisplay.classList.remove('visible');
  if (!group) return;

  setStatus(`Lade Sender für ${group} ...`, true);
  try {
    const r    = await fetch('/api/channels?group=' + encodeURIComponent(group));
    const data = await r.json();
    channelDd.innerHTML = '<option value="">– Sender wählen –</option>';
    data.channels.forEach(name => {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      channelDd.appendChild(opt);
    });
    channelDd.disabled = false;
    setStatus(`✅ ${data.channels.length} Sender für ${group} geladen.`);
  } catch (e) {
    setStatus('Fehler: ' + e);
    channelDd.innerHTML = '<option value="">Fehler</option>';
  }
});

// ── HLS aufräumen ──────────────────────────────────────────────────────────
function destroyHls() {
  if (currentHls) {
    try { currentHls.destroy(); } catch (_) {}
    currentHls = null;
  }
  video.src    = '';
  _playStarted = false;
}

// ── Video abspielen (einmalig, race-sicher) ────────────────────────────────
function safePlay(label) {
  if (_playStarted) return;   // verhindert doppelten Aufruf bei recoverMediaError
  _playStarted = true;
  const p = video.play();
  if (p !== undefined) {
    p.then(() => setStatus(`▶ ${label}`))
     .catch(err => setStatus(`▶ ${label} – Play drücken`));
  }
}

// ── Stream starten ─────────────────────────────────────────────────────────
// retry = Index in der URL-Liste des Senders (exakt eine URL pro Schritt)
async function startStream(group, channel, retry) {
  destroyHls();
  hlsLog.style.display = 'none';
  hlsLog.innerHTML     = '';

  showOverlay(retry > 0
    ? `Fallback ${retry}/${totalUrls - 1} ...`
    : 'Löse Stream auf ...');
  setStatus(
    retry > 0
      ? `▶ ${channel} – Fallback ${retry} ...`
      : `▶ ${channel} – Stream wird aufgelöst ...`,
    true
  );
  urlDisplay.classList.remove('visible');

  let data;
  try {
    const r = await fetch(
      '/api/resolve?group='  + encodeURIComponent(group) +
      '&channel='            + encodeURIComponent(channel) +
      '&retry='              + retry
    );
    data = await r.json();
  } catch (e) {
    showOverlay('Netzwerkfehler: ' + e);
    setStatus('Fehler: ' + e);
    return;
  }

  // Gesamtanzahl merken (für Fortschrittsanzeige)
  if (data.total !== undefined) totalUrls = data.total;

  if (!data.url) {
    showOverlay('Stream nicht verfügbar.');
    setStatus(`❌ ${channel}: alle ${totalUrls} URLs erschöpft (retry=${retry}).`);
    return;
  }

  urlDisplay.textContent = '🔗 ' + data.url;
  urlDisplay.classList.add('visible');

  const proxyUrl = '/proxy?url=' + encodeURIComponent(data.url);
  chName.textContent    = channel;
  liveBadge.textContent = 'LIVE';

  // ── Native HLS (Safari / iOS) ──────────────────────────────────────────
  if (!Hls.isSupported() && video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = proxyUrl;
    hideOverlay();
    video.play().catch(() => {});
    setStatus(`▶ ${channel}`);
    return;
  }
  if (!Hls.isSupported()) {
    showOverlay('HLS wird in diesem Browser nicht unterstützt.');
    setStatus('Browser unterstützt kein HLS.');
    return;
  }

  // ── hls.js ─────────────────────────────────────────────────────────────
  const hls = new Hls({
    enableWorker:             true,
    debug:                    false,
    manifestLoadingTimeOut:   10000,
    manifestLoadingMaxRetry:  2,
    levelLoadingTimeOut:      10000,
    levelLoadingMaxRetry:     3,
    fragLoadingTimeOut:       20000,
    fragLoadingMaxRetry:      3,
    maxBufferLength:          30,
    maxMaxBufferLength:       60,
    stretchShortVideoTrack:   true,
  });

  currentHls   = hls;
  _playStarted = false;

  hls.loadSource(proxyUrl);
  hls.attachMedia(video);

  // MANIFEST_PARSED → Play (race-sicher durch safePlay)
  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    hideOverlay();
    setStatus(`▶ ${channel} – klicke Play falls nötig`);
    safePlay(channel);
  });

  // Fehlerbehandlung
  hls.on(Hls.Events.ERROR, (e, d) => {
    logHls(`[${d.type}] ${d.details} fatal=${d.fatal}`);

    if (!d.fatal) return;

    // ── Parsing-/Codec-Fehler: SOFORT nächste URL, kein recover ──────────
    if (FATAL_NO_RECOVER.has(d.details)) {
      logHls(`${d.details} → nicht behebbar, wechsle URL ...`);
      _tryNextUrl(group, channel, retry, d.details);
      return;
    }

    // ── Netzwerkfehler: einmal startLoad() ───────────────────────────────
    if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
      logHls('Netzwerkfehler – startLoad() ...');
      hls.startLoad();
      return;
    }

    // ── Media-Decoder-Fehler: einmal recoverMediaError() ─────────────────
    if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
      logHls('MediaError – recoverMediaError() ...');
      // _playStarted zurücksetzen damit safePlay() nach Reattach klappt
      _playStarted = false;
      hls.recoverMediaError();
      // Wenn nach dem nächsten MANIFEST_PARSED wieder ein fataler Fehler
      // kommt, landet er erneut hier und geht dann in _tryNextUrl
      return;
    }

    // ── Sonstiger fataler Fehler ──────────────────────────────────────────
    logHls(`Fataler Fehler (${d.details}) → wechsle URL ...`);
    _tryNextUrl(group, channel, retry, d.details);
  });
}

// ── Nächste URL versuchen oder aufgeben ────────────────────────────────────
function _tryNextUrl(group, channel, currentRetry, reason) {
  const next = currentRetry + 1;
  if (totalUrls > 0 && next >= totalUrls) {
    showOverlay(`Stream nicht verfügbar (${reason}).`);
    setStatus(`❌ ${channel}: alle ${totalUrls} URLs erschöpft.`);
    return;
  }
  startStream(group, channel, next);
}

// ── Sender wählen ──────────────────────────────────────────────────────────
channelDd.addEventListener('change', function () {
  const channel = this.value;
  const group   = groupDd.value;
  if (!channel) return;

  totalUrls = 0;
  startStream(group, channel, 0);
});

loadGroups();
</script>
</body>
</html>"""

# ═════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═════════════════════════════════════════════════════════════════════════════

fastapi_app = FastAPI()

CORS_H = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control":                "no-cache",
}

@fastapi_app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=FRONTEND, headers={
        "Content-Security-Policy":
            "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;",
        "X-Frame-Options": "SAMEORIGIN",
    })

@fastapi_app.get("/api/groups")
async def api_groups():
    groups = [
        {"name": g, "count": _state["counts"].get(g, 0)}
        for g in _state["groups"]
    ]
    return JSONResponse({"groups": groups})

@fastapi_app.get("/api/channels")
async def api_channels(group: str):
    sig = _state["signature"] or get_auth_signature()
    if not sig:
        return JSONResponse({"error": "Auth fehlgeschlagen"}, status_code=500)
    if group not in _channels_cache:
        _channels_cache[group] = get_channels(group, sig, _state["index"])
    names = sorted(_channels_cache[group].keys())
    return JSONResponse({"channels": names})

@fastapi_app.get("/api/resolve")
async def api_resolve(group: str, channel: str, retry: int = 0):
    """
    retry = exakter Index in der URL-Liste.
    Gibt GENAU diese eine URL zurück (kein Durchiterieren ab Index N).
    Außerdem wird 'total' mitgeliefert, damit der Client weiß wann er aufhören soll.
    """
    sig = _state["signature"] or get_auth_signature()
    if not sig:
        return JSONResponse({"error": "Auth fehlgeschlagen"}, status_code=500)
    if group not in _channels_cache:
        _channels_cache[group] = get_channels(group, sig, _state["index"])

    urls  = _channels_cache[group].get(channel, [])
    total = len(urls)

    if not urls or retry >= total:
        return JSONResponse({"url": None, "total": total,
                              "error": "Kein URL gefunden"})

    # Nur die URL an Position 'retry' auflösen – kein weiteres Iterieren
    cdn_url = resolve_url(urls[retry], sig)

    logger.info(f"Resolve: {channel} [{retry}/{total-1}] → {(cdn_url or 'FEHLER')[:80]}")
    return JSONResponse({"url": cdn_url, "total": total})

@fastapi_app.get("/hlsjs")
async def serve_hlsjs():
    global _hlsjs_cache
    if _hlsjs_cache is None:
        for url in ["https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js",
                    "https://unpkg.com/hls.js@1.5.13/dist/hls.min.js"]:
            try:
                r = SESSION.get(url, timeout=15)
                if r.status_code == 200:
                    _hlsjs_cache = r.content
                    logger.info(f"hls.js geladen ({len(_hlsjs_cache)} Bytes)")
                    break
            except Exception as e:
                logger.warning(f"hls.js: {e}")
        if _hlsjs_cache is None:
            return Response("// hls.js nicht verfügbar",
                            media_type="application/javascript", status_code=503)
    return Response(_hlsjs_cache, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})

# ── M3U8-Rewriter ─────────────────────────────────────────────────────────────

def _rewrite_m3u8(text: str, base_url: str) -> str:
    base = base_url.rsplit("/", 1)[0] + "/"

    def to_proxy(u: str) -> str:
        if not u: return u
        abs_u = u if u.startswith("http") else base + u
        return "/proxy?url=" + urllib.parse.quote(abs_u, safe="")

    def rewrite_attr_uri(line: str) -> str:
        return re.sub(r'URI="([^"]+)"',
                      lambda m: f'URI="{to_proxy(m.group(1))}"', line)

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith(("#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-MEDIA")):
            out.append(rewrite_attr_uri(line))
        elif s.startswith("#"):
            out.append(line)
        else:
            out.append(to_proxy(s))
    return "\n".join(out)

# ── Stream-Proxy ──────────────────────────────────────────────────────────────

_PROXY_TIMEOUT = httpx.Timeout(connect=8.0, read=20.0, write=8.0, pool=5.0)
_PROXY_LIMITS  = httpx.Limits(max_keepalive_connections=30, max_connections=60)
_PROXY_HEADERS = {"User-Agent": UA_STREAM, "Accept": "*/*",
                   "Accept-Encoding": "identity"}

BUFFER_MAX_BYTES = 10 * 1024 * 1024

@fastapi_app.options("/proxy")
async def proxy_options():
    return Response(headers=CORS_H)

@fastapi_app.get("/proxy")
async def proxy_stream(url: str, request: Request):
    cdn_url = urllib.parse.unquote(url)
    logger.debug(f"Proxy: {cdn_url[:90]}")

    req_h = dict(_PROXY_HEADERS)
    if "range" in request.headers:
        req_h["Range"] = request.headers["range"]

    try:
        client = httpx.AsyncClient(
            timeout=_PROXY_TIMEOUT, limits=_PROXY_LIMITS,
            follow_redirects=True, headers=_PROXY_HEADERS,
        )
        resp = await client.send(
            client.build_request("GET", cdn_url, headers=req_h), stream=True)
        ct = resp.headers.get("content-type", "application/octet-stream")
        logger.info(f"Proxy upstream {resp.status_code} ct={ct[:60]}")

        is_m3u8 = (
            "mpegurl" in ct.lower()
            or cdn_url.split("?")[0].lower().endswith((".m3u8", ".m3u"))
        )
        if is_m3u8:
            try:
                raw = await resp.aread()
            finally:
                await client.aclose()
            text      = raw.decode("utf-8", errors="replace")
            rewritten = _rewrite_m3u8(text, cdn_url)
            logger.info(f"M3U8 rewritten ({len(raw)} B, "
                        f"{rewritten.count('/proxy?url=')} URLs)")
            return Response(rewritten,
                            media_type="application/vnd.apple.mpegurl",
                            headers=CORS_H)

        cl       = resp.headers.get("content-length")
        is_large = cl and int(cl) > BUFFER_MAX_BYTES

        if is_large:
            async def stream_chunks():
                try:
                    async for chunk in resp.aiter_bytes(32768):
                        yield chunk
                except Exception as e:
                    logger.warning(f"Proxy stream abgebrochen: {e}")
                finally:
                    await client.aclose()
            out_h = dict(CORS_H)
            out_h["Content-Type"] = ct
            if cl: out_h["Content-Length"] = cl
            if "content-range" in resp.headers:
                out_h["Content-Range"] = resp.headers["content-range"]
            return StreamingResponse(stream_chunks(),
                                     status_code=resp.status_code,
                                     media_type=ct, headers=out_h)
        else:
            try:
                body = await resp.aread()
            finally:
                await client.aclose()
            logger.debug(f"Proxy buffered {len(body)} B ct={ct[:40]}")
            out_h = dict(CORS_H)
            out_h["Content-Type"]   = ct
            out_h["Content-Length"] = str(len(body))
            if "content-range" in resp.headers:
                out_h["Content-Range"] = resp.headers["content-range"]
            return Response(body, status_code=resp.status_code,
                            media_type=ct, headers=out_h)

    except httpx.TimeoutException:
        logger.warning(f"Proxy Timeout: {cdn_url[:80]}")
        return Response("Upstream Timeout", status_code=504,
                        headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        logger.error(f"Proxy Fehler: {e}")
        return Response(f"Fehler: {e}", status_code=502,
                        headers={"Access-Control-Allow-Origin": "*"})

# ═════════════════════════════════════════════════════════════════════════════
# GRADIO – minimale Stub-UI damit HF Space den Typ erkennt
# ═════════════════════════════════════════════════════════════════════════════

with gr.Blocks() as demo:
    gr.HTML("""
    <div style="padding:24px;text-align:center;font-family:sans-serif;color:#888;">
      <p>Der Player läuft auf der Hauptseite.</p>
      <p><a href="/" style="color:#4af;">→ Zum Player</a></p>
    </div>
    """)

app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
