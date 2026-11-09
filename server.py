import asyncio
import hashlib
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, Any, List, Optional
import httpx
import yt_dlp
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

app = FastAPI(title="MiniCine Kids iOS 9 Server")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
JELLYFIN_DIRS = ["/media/peliculas", "/media/series"]
HLS_ROOT = "/tmp/minicine_hls"
os.makedirs(HLS_ROOT, exist_ok=True)

# Cachés en memoria para que en los iPads vaya instantáneo
SEARCH_CACHE: Dict[str, Dict[str, Any]] = {}
STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
LOCAL_FILES_MAP: Dict[str, str] = {}
HLS_PROCESSES: Dict[str, subprocess.Popen] = {}

SEARCH_TTL = 3600 * 6   # 6 horas
STREAM_TTL = 3600 * 2   # 2 horas
ACCESS_PIN = os.getenv("ACCESS_PIN", "1234")
HOST_LAN_IP = os.getenv("HOST_LAN_IP", "192.168.1.100")
DUCKDNS_DOMAIN = os.getenv("DUCKDNS_DOMAIN", "").strip()
DUCKDNS_TOKEN = os.getenv("DUCKDNS_TOKEN", "").strip()


def verify_pin(request: Request, pin: Optional[str] = None):
    cookie_pin = request.cookies.get("minicine_pin")
    query_pin = pin or request.query_params.get("pin")
    if cookie_pin == ACCESS_PIN or query_pin == ACCESS_PIN:
        return True
    raise HTTPException(status_code=401, detail="PIN requerido")


@app.get("/api/auth")
async def api_auth(pin: str):
    if pin != ACCESS_PIN:
        raise HTTPException(status_code=401, detail="PIN incorrecto")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="minicine_pin",
        value=ACCESS_PIN,
        max_age=315360000,
        httponly=False,
        samesite="lax",
    )
    return resp

# Estado del Mando a Distancia para los Padres (/padres)
REMOTE_STATE: Dict[str, Dict[str, Any]] = {
    "ipad1": {"cmd_id": 1, "item": None},
    "ipad2": {"cmd_id": 1, "item": None},
}
NEXT_CMD_ID = 2

# Canales 24h / Maratones en directo para la pestaña "TV Directo"
LIVE_TV_QUERIES = [
    ("🐶 Canal Bluey 24h (Maratón Castellano)", "Bluey Español Canal Oficial capitulos completos maraton castellano"),
    ("🐾 Canal Patrulla Canina 24h (Nick Jr Español)", "Patrulla Canina Nick Jr Español capitulos completos maraton"),
    ("🐭 Canal Disney Junior (Mickey y Amigos)", "Disney Junior España La Casa de Mickey Mouse capitulos completos español"),
    ("🐷 Canal Peppa Pig 24h (Castellano)", "Peppa Pig Español Oficial capitulos completos 1 hora castellano"),
    ("🕷️ Canal Spidey y su Superequipo", "Spidey y su superequipo Disney Junior español castellano recopilacion"),
    ("🐱 Canal La Casa de Muñecas de Gabby", "La casa de muñecas de Gabby Español capitulos completos maraton"),
    ("💙 Canal Pocoyó 24h (Especial 3 Años)", "Pocoyo Español capitulos completos 1 hora castellano"),
    ("❄️ Canal Frozen y Princesas Disney", "Disney España Frozen Vaiana cuentos canciones recopilacion castellano"),
]


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "▶️ Vídeo"
    sec = int(seconds)
    mins, s = divmod(sec, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {mins:02d}m"
    return f"{mins}:{s:02d}"


def run_yt_search(query: str, max_results: int = 14) -> List[Dict[str, Any]]:
    now = time.time()
    cache_key = f"{query}_{max_results}"
    if cache_key in SEARCH_CACHE:
        cached = SEARCH_CACHE[cache_key]
        if now - cached["ts"] < SEARCH_TTL:
            return cached["items"]

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    items: List[Dict[str, Any]] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        res = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        for entry in res.get("entries", []) or []:
            if not entry:
                continue
            vid_id = entry.get("id")
            title = entry.get("title")
            duration = entry.get("duration")
            if not vid_id or not title:
                continue
            if duration and duration < 60:
                continue
            items.append({
                "id": vid_id,
                "title": title,
                "duration": format_duration(duration),
                "thumb": f"/api/thumb?id={vid_id}",
                "download_url": f"/api/download?id={vid_id}",
            })

    SEARCH_CACHE[cache_key] = {"ts": now, "items": items}
    return items


def resolve_mp4_url(video_id: str) -> Dict[str, Any]:
    now = time.time()
    if video_id in STREAM_CACHE:
        cached = STREAM_CACHE[video_id]
        if now - cached["ts"] < STREAM_TTL:
            return cached

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"deno": {}, "node": {}},
        "format": "22/18/b[ext=mp4][vcodec^=avc1]/b[ext=mp4]",
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        direct_url = info.get("url")
        http_headers = info.get("http_headers", {})
        if not direct_url:
            raise HTTPException(status_code=404, detail="No se encontró stream MP4 compatible")

    data = {
        "ts": now,
        "url": direct_url,
        "headers": http_headers,
    }
    STREAM_CACHE[video_id] = data
    return data


def scan_jellyfin_files() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    valid_exts = (".mkv", ".mp4", ".m4v", ".avi", ".mov")
    for base in JELLYFIN_DIRS:
        if not os.path.exists(base):
            continue
        for root, _, files in os.walk(base):
            for fname in sorted(files):
                if not fname.lower().endswith(valid_exts):
                    continue
                full_path = os.path.join(root, fname)
                file_id = hashlib.md5(full_path.encode("utf-8")).hexdigest()[:12]
                LOCAL_FILES_MAP[file_id] = full_path
                clean_title = os.path.splitext(fname)[0].replace(".", " ")
                size_mb = int(os.path.getsize(full_path) / (1024 * 1024))
                items.append({
                    "id": f"local_{file_id}",
                    "title": f"🎬 {clean_title}",
                    "duration": f"{size_mb} MB",
                    "thumb": "/api/local/poster",
                    "stream_url": f"/api/local/hls/{file_id}/index.m3u8",
                })
    return items


def stop_all_hls_except(keep_id: str):
    for fid, proc in list(HLS_PROCESSES.items()):
        if fid != keep_id:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            HLS_PROCESSES.pop(fid, None)
            shutil.rmtree(os.path.join(HLS_ROOT, fid), ignore_errors=True)


@app.get("/")
async def index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/padres")
async def padres():
    return FileResponse(
        os.path.join(STATIC_DIR, "padres.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/search")
async def api_search(q: str, request: Request):
    verify_pin(request)
    items = run_yt_search(q, max_results=14)
    return JSONResponse({"items": items})


@app.get("/api/livetv")
async def api_livetv(request: Request):
    verify_pin(request)
    items: List[Dict[str, Any]] = []
    for channel_name, query in LIVE_TV_QUERIES:
        results = run_yt_search(query, max_results=3)
        if results:
            first = dict(results[0])
            first["title"] = f"{channel_name} — {first['title']}"
            first["duration"] = "🔴 EN EMISIÓN"
            items.append(first)
    return JSONResponse({"items": items})


@app.get("/api/jellyfin")
async def api_jellyfin(request: Request):
    verify_pin(request)
    items = scan_jellyfin_files()
    return JSONResponse({"items": items})


@app.get("/api/local/poster")
async def api_local_poster():
    # SVG ligero compatible con iOS 9 para películas locales de Jellyfin
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">'
        '<rect width="480" height="270" fill="#1e293b"/>'
        '<circle cx="240" cy="115" r="46" fill="#00a4dc"/>'
        '<polygon points="228,92 228,138 266,115" fill="#ffffff"/>'
        '<text x="240" y="205" font-family="Helvetica, Arial" font-size="24" font-weight="bold" fill="#ffffff" text-anchor="middle">Biblioteca Jellyfin</text>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/local/hls/{file_id}/index.m3u8")
async def api_local_hls_playlist(file_id: str, request: Request):
    verify_pin(request)
    if not LOCAL_FILES_MAP:
        scan_jellyfin_files()
    full_path = LOCAL_FILES_MAP.get(file_id)
    if not full_path or not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Archivo local no encontrado")

    stop_all_hls_except(file_id)
    out_dir = os.path.join(HLS_ROOT, file_id)
    os.makedirs(out_dir, exist_ok=True)
    m3u8_path = os.path.join(out_dir, "index.m3u8")

    proc = HLS_PROCESSES.get(file_id)
    if proc is None or proc.poll() is not None:
        # Limpiar segmentos previos
        for f in os.listdir(out_dir):
            try:
                os.remove(os.path.join(out_dir, f))
            except Exception:
                pass
        cmd = [
            "ffmpeg", "-y",
            "-i", full_path,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-profile:v", "main",
            "-level", "3.1",
            "-vf", "scale=-2:720",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ac", "2",
            "-b:a", "128k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_segment_filename", os.path.join(out_dir, "seg_%04d.ts"),
            m3u8_path,
        ]
        HLS_PROCESSES[file_id] = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    # Esperar hasta que ffmpeg genere el primer segmento y el archivo index.m3u8
    for _ in range(40):
        if os.path.exists(m3u8_path) and os.path.getsize(m3u8_path) > 20:
            break
        await asyncio.sleep(0.25)

    if not os.path.exists(m3u8_path):
        raise HTTPException(status_code=500, detail="Error preparando el vídeo de Jellyfin")

    return FileResponse(
        m3u8_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/local/hls/{file_id}/{segment_name}")
async def api_local_hls_segment(file_id: str, segment_name: str):
    if ".." in segment_name or not segment_name.endswith(".ts"):
        raise HTTPException(status_code=400, detail="Segmento inválido")
    seg_path = os.path.join(HLS_ROOT, file_id, segment_name)
    for _ in range(20):
        if os.path.exists(seg_path):
            break
        await asyncio.sleep(0.2)
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="Segmento no encontrado")
    return FileResponse(seg_path, media_type="video/MP2T")


@app.get("/api/thumb")
async def api_thumb(id: str):
    thumb_url = f"https://i.ytimg.com/vi/{id}/hqdefault.jpg"
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        r = await client.get(thumb_url)
        if r.status_code != 200:
            raise HTTPException(status_code=404, detail="Miniatura no encontrada")
        return Response(
            content=r.content,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )


@app.get("/api/stream")
async def api_stream(id: str, request: Request):
    verify_pin(request)
    stream_info = resolve_mp4_url(id)
    upstream_url = stream_info["url"]
    base_headers = dict(stream_info.get("headers") or {})

    range_header = request.headers.get("range")
    if range_header:
        base_headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    req = client.build_request("GET", upstream_url, headers=base_headers)
    resp = await client.send(req, stream=True)

    if resp.status_code in (401, 403):
        await resp.aclose()
        await client.aclose()
        STREAM_CACHE.pop(id, None)
        stream_info = resolve_mp4_url(id)
        upstream_url = stream_info["url"]
        base_headers = dict(stream_info.get("headers") or {})
        if range_header:
            base_headers["Range"] = range_header
        client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
        req = client.build_request("GET", upstream_url, headers=base_headers)
        resp = await client.send(req, stream=True)

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": resp.headers.get("content-type", "video/mp4"),
    }
    for h_name in ("content-length", "content-range"):
        if h_name in resp.headers:
            response_headers[h_name.title()] = resp.headers[h_name]

    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_generator(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=response_headers["Content-Type"],
    )


@app.get("/api/download")
async def api_download(id: str, request: Request):
    verify_pin(request)
    stream_info = resolve_mp4_url(id)
    upstream_url = stream_info["url"]
    base_headers = dict(stream_info.get("headers") or {})

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0))
    req = client.build_request("GET", upstream_url, headers=base_headers)
    resp = await client.send(req, stream=True)

    response_headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": f'attachment; filename="minicine_{id}.mp4"',
    }
    if "content-length" in resp.headers:
        response_headers["Content-Length"] = resp.headers["content-length"]

    async def download_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        download_generator(),
        status_code=200,
        headers=response_headers,
        media_type="video/mp4",
    )


class RemoteCommand(BaseModel):
    target: str
    action: str = "play"
    item: Optional[Dict[str, Any]] = None


@app.post("/api/remote/send")
async def api_remote_send(cmd: RemoteCommand, request: Request):
    verify_pin(request)
    global NEXT_CMD_ID
    cid = NEXT_CMD_ID
    NEXT_CMD_ID += 1
    payload = {"cmd_id": cid, "action": cmd.action, "item": cmd.item}
    if cmd.target in ("both", "ipad1"):
        REMOTE_STATE["ipad1"] = payload
    if cmd.target in ("both", "ipad2"):
        REMOTE_STATE["ipad2"] = payload
    return JSONResponse({"ok": True, "cmd_id": cid})


@app.get("/api/remote/poll")
async def api_remote_poll(request: Request, device: str = "ipad1"):
    verify_pin(request)
    state = REMOTE_STATE.get(device, REMOTE_STATE["ipad1"])
    return JSONResponse(state)


class OpenAnyAuthorizer(DummyAuthorizer):
    def validate_authentication(self, username, password, handler):
        if not self.has_user(username):
            self.add_user(username, password or "", "/media", perm="elr")
        return True


def start_ftp_server():
    os.makedirs("/media/peliculas", exist_ok=True)
    os.makedirs("/media/series", exist_ok=True)
    authorizer = OpenAnyAuthorizer()
    authorizer.add_anonymous("/media", perm="elr")
    handler = FTPHandler
    handler.authorizer = authorizer
    handler.masquerade_address = HOST_LAN_IP
    handler.passive_ports = range(30000, 30010)
    server = FTPServer(("0.0.0.0", 2121), handler)
    server.serve_forever()


def start_duckdns_updater():
    if not DUCKDNS_DOMAIN or not DUCKDNS_TOKEN:
        return
    while True:
        try:
            url = f"https://www.duckdns.org/update?domains={DUCKDNS_DOMAIN}&token={DUCKDNS_TOKEN}&ip="
            httpx.get(url, timeout=10.0)
        except Exception:
            pass
        time.sleep(300)


@app.on_event("startup")
async def on_startup():
    t = threading.Thread(target=start_ftp_server, daemon=True)
    t.start()
    if DUCKDNS_DOMAIN and DUCKDNS_TOKEN:
        t_ddns = threading.Thread(target=start_duckdns_updater, daemon=True)
        t_ddns.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
