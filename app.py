"""Downme — downloader de vídeos do YouTube (uso pessoal, roda local).

Backend Flask + yt-dlp, usado de dois jeitos:
  * site local:  `python app.py` -> abre em http://127.0.0.1:5000
  * app desktop: `desktop.py` (vira Downme.exe) chama configure(desktop=True)

Fluxo da API:
  1. POST /api/info      -> metadados + qualidades disponíveis
  2. POST /api/download  -> inicia um job em segundo plano, devolve job_id
  3. GET  /api/progress/<job_id> -> status/porcentagem
  4. GET  /api/file/<job_id>     -> entrega o arquivo (modo site)
     POST /api/open/<job_id>     -> abre arquivo/pasta (modo app)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp

FROZEN = getattr(sys, "frozen", False)
# Dentro do .exe os arquivos embutidos ficam em sys._MEIPASS.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
JOB_TTL_SECONDS = 60 * 60  # arquivos temporários ficam no máximo 1h

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}

CONFIG = {
    "desktop": False,
    "work_dir": BUNDLE_DIR / "downloads",
    "save_dir": None,  # modo app: pasta final dos arquivos
}


def configure(desktop: bool = False, save_dir: Path | None = None) -> None:
    CONFIG["desktop"] = desktop
    if desktop:
        CONFIG["work_dir"] = Path(tempfile.gettempdir()) / "downme"
        CONFIG["save_dir"] = Path(save_dir or Path.home() / "Downloads" / "Downme")


# ---------- binários externos (ffmpeg e runtime JS) ----------

def _bundled(name: str) -> str | None:
    p = BUNDLE_DIR / "bin" / name
    return str(p) if p.exists() else None


def find_ffmpeg() -> str | None:
    found = _bundled("ffmpeg.exe") or shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def js_runtimes() -> dict:
    """yt-dlp precisa de Deno ou Node para resolver os desafios do YouTube."""
    node = _bundled("node.exe") or shutil.which("node")
    runtimes = {}
    if shutil.which("deno"):
        runtimes["deno"] = {}
    if node:
        runtimes["node"] = {"path": node}
    return runtimes or {"deno": {}}


FFMPEG = find_ffmpeg()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "js_runtimes": js_runtimes(),
}
if FFMPEG:
    BASE_OPTS["ffmpeg_location"] = FFMPEG

app = Flask(__name__, static_folder=str(BUNDLE_DIR / "static"))
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------- utilidades ----------

def valid_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in ALLOWED_HOSTS


def cleanup_old_jobs() -> None:
    now = time.time()
    with jobs_lock:
        expired = [jid for jid, j in jobs.items() if now - j["created"] > JOB_TTL_SECONDS]
        for jid in expired:
            jobs.pop(jid, None)
    work = CONFIG["work_dir"]
    if work.exists():
        for folder in work.iterdir():
            if folder.is_dir() and now - folder.stat().st_mtime > JOB_TTL_SECONDS:
                shutil.rmtree(folder, ignore_errors=True)


def update_job(job_id: str, **fields) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if "Private video" in msg:
        return "Esse vídeo é privado."
    if "Sign in to confirm your age" in msg:
        return "Vídeo com restrição de idade — não dá pra baixar sem login."
    if "Video unavailable" in msg:
        return "Vídeo indisponível."
    if "not a bot" in msg:
        return "O YouTube bloqueou temporariamente. Tente de novo em alguns minutos."
    if "ffmpeg" in msg.lower():
        return "ffmpeg não encontrado — ele é necessário para juntar vídeo e áudio."
    return "Não consegui processar esse vídeo. Confira o link e tente de novo."


def to_mp3(src: Path, title: str | None, artist: str | None) -> Path:
    """Converte o áudio baixado para MP3 192 kbps (só precisa do ffmpeg)."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg não encontrado")
    dst = src.with_suffix(".mp3")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)
    src.unlink(missing_ok=True)
    return dst


# ---------- rotas ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    save = CONFIG["save_dir"]
    return jsonify(desktop=CONFIG["desktop"], save_dir=str(save) if save else None)


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_youtube_url(url):
        return jsonify(error="Cole um link válido do YouTube."), 400

    try:
        with yt_dlp.YoutubeDL({**BASE_OPTS, "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("info falhou: %s", exc)
        return jsonify(error=friendly_error(exc)), 422

    heights = sorted(
        {f["height"] for f in data.get("formats", [])
         if f.get("height") and f.get("vcodec") not in (None, "none")},
        reverse=True,
    )

    return jsonify(
        title=data.get("title"),
        channel=data.get("uploader") or data.get("channel"),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        qualities=[h for h in heights if h >= 144],
    )


@app.post("/api/download")
def download():
    cleanup_old_jobs()
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    kind = body.get("kind", "video")
    quality = body.get("quality")

    if not valid_youtube_url(url):
        return jsonify(error="Cole um link válido do YouTube."), 400
    if kind not in ("video", "audio"):
        return jsonify(error="Tipo inválido."), 400
    try:
        quality = int(quality) if quality is not None else None
    except (TypeError, ValueError):
        return jsonify(error="Qualidade inválida."), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued", "percent": 0, "phase": "Na fila",
            "file": None, "saved_name": None, "error": None, "created": time.time(),
        }

    threading.Thread(
        target=run_download, args=(job_id, url, kind, quality), daemon=True
    ).start()
    return jsonify(job_id=job_id)


def run_download(job_id: str, url: str, kind: str, quality: int | None) -> None:
    out_dir = CONFIG["work_dir"] / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = round(done / total * 100, 1) if total else 0
            fmt = d.get("info_dict", {})
            is_audio_only = fmt.get("vcodec") in (None, "none")
            phase = "Baixando áudio" if is_audio_only and kind == "video" else "Baixando"
            update_job(job_id, status="downloading", percent=pct, phase=phase,
                       speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            update_job(job_id, percent=100)

    def postprocessor_hook(d):
        if d["status"] == "started":
            update_job(job_id, status="processing", phase="Finalizando arquivo")

    opts = {
        **BASE_OPTS,
        "outtmpl": str(out_dir / "%(title).150B.%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "windowsfilenames": True,
    }

    if kind == "audio":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        # Maior resolução até a escolhida; entre iguais, prefere H.264 + AAC
        # (toca em qualquer player). Acima de 1080p o YouTube só tem VP9/AV1.
        sort = [f"res:{quality}"] if quality else ["res"]
        opts["format"] = "bv*+ba/b"
        opts["format_sort"] = sort + ["vcodec:h264", "acodec:m4a"]
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
        files = [p for p in out_dir.iterdir()
                 if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Arquivo final não encontrado")
        final = max(files, key=lambda p: p.stat().st_size)

        if kind == "audio":
            update_job(job_id, status="processing", phase="Convertendo para MP3")
            final = to_mp3(final, data.get("title"), data.get("uploader"))

        if CONFIG["desktop"]:
            save_dir = CONFIG["save_dir"]
            save_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(save_dir, final.name)
            shutil.move(str(final), target)
            shutil.rmtree(out_dir, ignore_errors=True)
            final = target

        update_job(job_id, status="done", percent=100, phase="Pronto",
                   file=str(final), saved_name=final.name)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("download falhou: %s", exc)
        update_job(job_id, status="error", error=friendly_error(exc))
        shutil.rmtree(out_dir, ignore_errors=True)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Job não encontrado."), 404
        return jsonify({k: v for k, v in job.items() if k not in ("file", "created")})


@app.get("/api/file/<job_id>")
def get_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return jsonify(error="Arquivo não disponível."), 404
    path = Path(job["file"])
    if not path.exists():
        return jsonify(error="Arquivo expirou."), 410
    return send_file(path, as_attachment=True, download_name=path.name)


@app.post("/api/open/<job_id>")
def open_result(job_id):
    """Modo app: abre o arquivo baixado ou mostra ele no Explorer."""
    if not CONFIG["desktop"]:
        return jsonify(error="Só disponível no app."), 404
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or not job.get("file"):
        return jsonify(error="Arquivo não disponível."), 404
    path = Path(job["file"])
    if not path.exists():
        return jsonify(error="O arquivo foi movido ou apagado."), 410

    what = (request.get_json(silent=True) or {}).get("what", "folder")
    if what == "file":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["explorer", "/select,", str(path)])
    return jsonify(ok=True)


if __name__ == "__main__":
    CONFIG["work_dir"].mkdir(parents=True, exist_ok=True)
    # Só escuta na sua máquina. Troque para 0.0.0.0 se quiser acessar pela rede local.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
