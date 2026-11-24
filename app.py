"""Downme — downloader de vídeos do YouTube (uso pessoal, roda local).

Backend Flask + yt-dlp, usado de dois jeitos:
  * site local:  `python app.py` -> abre em http://127.0.0.1:5000
  * app desktop: `desktop.py` (vira Downme.exe) chama configure(desktop=True)

Tudo passa por uma fila de downloads. Cada item tem formato e qualidade
próprios (editáveis enquanto espera), pode ser reordenado, segurado,
cancelado ou refeito. No app desktop a fila pendente sobrevive ao fechar.

API:
  GET    /api/queue                  -> itens + configurações da fila
  POST   /api/queue                  -> adiciona links {urls, kind, quality}
  PATCH  /api/queue/<id>             -> edita {kind, quality, hold}
  DELETE /api/queue/<id>             -> remove (cancela se estiver baixando)
  POST   /api/queue/<id>/move        -> {dir: up|down|top}
  POST   /api/queue/<id>/retry       -> recoloca erro/cancelado na fila
  POST   /api/queue/clear            -> tira concluídos/erros da lista
  POST   /api/settings               -> {concurrency, paused}
  GET    /api/file/<id>              -> entrega o arquivo (modo site)
  POST   /api/open/<id>              -> abre arquivo/pasta (modo app)
"""

import json
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
    "save_dir": None,    # modo app: pasta final dos arquivos
    "state_file": None,  # modo app: onde a fila pendente é salva
}


def configure(desktop: bool = False, save_dir: Path | None = None,
              state_file: Path | None = None) -> None:
    CONFIG["desktop"] = desktop
    if desktop:
        CONFIG["work_dir"] = Path(tempfile.gettempdir()) / "downme"
        CONFIG["save_dir"] = Path(save_dir or Path.home() / "Downloads" / "Downme")
        CONFIG["state_file"] = Path(state_file) if state_file else None
        load_state()


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
    "noprogress": True,  # o progresso vem pelos hooks; não suja o log
    "noplaylist": True,
    "js_runtimes": js_runtimes(),
}
if FFMPEG:
    BASE_OPTS["ffmpeg_location"] = FFMPEG

app = Flask(__name__, static_folder=str(BUNDLE_DIR / "static"))

# ---------- estado da fila ----------
# Tudo protegido por `lock`. `order` define a posição de cada item na fila.
lock = threading.RLock()
items: dict[str, dict] = {}
order: list[str] = []
SETTINGS = {"concurrency": 1, "paused": False}
MAX_CONCURRENCY = 4
MAX_URLS_PER_ADD = 200

ACTIVE = ("downloading", "processing")
FINISHED = ("done", "error", "canceled")
# Campos internos que não vão para o navegador.
PRIVATE = ("file", "cancel", "created")
# Limita quantas buscas de metadados rodam ao mesmo tempo (colar 50 links).
info_slots = threading.Semaphore(3)


class Canceled(Exception):
    pass


# ---------- utilidades ----------

def valid_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in ALLOWED_HOSTS


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




# ---------- fila ----------

def public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in PRIVATE}


def new_item(url: str, kind: str, quality: int | None) -> dict:
    return {
        "id": uuid.uuid4().hex, "url": url, "kind": kind, "quality": quality,
        "hold": False, "status": "fetching", "phase": "Buscando informações",
        "percent": 0, "speed": None, "eta": None, "error": None,
        "title": None, "channel": None, "duration": None, "thumbnail": None,
        "qualities": None, "saved_name": None,
        "file": None, "cancel": False, "created": time.time(),
    }


def persist() -> None:
    """Modo app: grava a fila pendente para retomar na próxima abertura."""
    path = CONFIG["state_file"]
    if not path:
        return
    with lock:
        pending = []
        for iid in order:
            it = items[iid]
            if it["status"] in FINISHED:
                continue
            keep = {k: it[k] for k in ("id", "url", "kind", "quality", "hold", "title",
                                         "channel", "duration", "thumbnail", "qualities")}
            pending.append(keep)
        data = {"settings": SETTINGS, "items": pending}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        app.logger.warning("não consegui salvar a fila: %s", exc)


def load_state() -> None:
    path = CONFIG["state_file"]
    if not path or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        app.logger.warning("fila salva ilegível, ignorando: %s", exc)
        return
    s = data.get("settings") or {}
    SETTINGS["concurrency"] = clamp_concurrency(s.get("concurrency", 1))
    SETTINGS["paused"] = bool(s.get("paused", False))
    to_fetch = []
    with lock:
        for saved in data.get("items") or []:
            url = saved.get("url", "")
            if not valid_youtube_url(url) or saved.get("kind") not in ("video", "audio"):
                continue
            it = new_item(url, saved["kind"], saved.get("quality"))
            for k in ("id", "hold", "title", "channel", "duration", "thumbnail", "qualities"):
                if saved.get(k) is not None:
                    it[k] = saved[k]
            if it["qualities"] is not None:
                it.update(status="queued", phase="Na fila")
            else:
                to_fetch.append(it["id"])
            items[it["id"]] = it
            order.append(it["id"])
    for iid in to_fetch:
        threading.Thread(target=fetch_info, args=(iid,), daemon=True).start()
    schedule()


def clamp_concurrency(value) -> int:
    try:
        return max(1, min(MAX_CONCURRENCY, int(value)))
    except (TypeError, ValueError):
        return 1


def parse_quality(value) -> int | None:
    """None = melhor disponível. Levanta ValueError se for lixo."""
    if value in (None, "", "best"):
        return None
    q = int(value)
    if not 100 <= q <= 10000:
        raise ValueError(q)
    return q


def fit_quality(wanted: int | None, available: list[int]) -> int | None:
    """Ajusta a qualidade pedida ao que o vídeo tem (maior <= pedida)."""
    if wanted is None or not available or wanted in available:
        return wanted
    lower = [h for h in available if h <= wanted]
    return max(lower) if lower else min(available)


def schedule() -> None:
    """Inicia downloads enquanto houver vaga, respeitando ordem, pausa e itens segurados."""
    with lock:
        if SETTINGS["paused"]:
            return
        running = sum(1 for it in items.values() if it["status"] in ACTIVE)
        for iid in order:
            if running >= SETTINGS["concurrency"]:
                break
            it = items[iid]
            if it["status"] == "queued" and not it["hold"]:
                it.update(status="downloading", phase="Iniciando", percent=0,
                          speed=None, eta=None, cancel=False)
                running += 1
                threading.Thread(target=run_download, args=(iid,), daemon=True).start()


def update_item(item_id: str, **fields) -> None:
    with lock:
        if item_id in items:
            items[item_id].update(fields)


def fetch_info(item_id: str) -> None:
    with info_slots:
        with lock:
            it = items.get(item_id)
            if not it or it["status"] != "fetching":
                return
            url = it["url"]
        try:
            with yt_dlp.YoutubeDL({**BASE_OPTS, "skip_download": True}) as ydl:
                data = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("info falhou: %s", exc)
            update_item(item_id, status="error", phase="Erro", error=friendly_error(exc))
            persist()
            return

    heights = sorted(
        {f["height"] for f in data.get("formats", [])
         if f.get("height") and f.get("vcodec") not in (None, "none") and f["height"] >= 144},
        reverse=True,
    )
    with lock:
        it = items.get(item_id)
        if not it or it["status"] != "fetching":
            return
        it.update(
            title=data.get("title"),
            channel=data.get("uploader") or data.get("channel"),
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail"),
            qualities=heights,
            quality=fit_quality(it["quality"], heights),
            status="queued", phase="Na fila",
        )
    persist()
    schedule()


def run_download(item_id: str) -> None:
    with lock:
        it = items[item_id]
        url, kind, quality = it["url"], it["kind"], it["quality"]
    out_dir = CONFIG["work_dir"] / item_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def canceled() -> bool:
        with lock:
            return items.get(item_id, {}).get("cancel", True)

    def progress_hook(d):
        if canceled():
            raise Canceled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = round(done / total * 100, 1) if total else 0
            fmt = d.get("info_dict", {})
            is_audio_only = fmt.get("vcodec") in (None, "none")
            phase = "Baixando áudio" if is_audio_only and kind == "video" else "Baixando"
            update_item(item_id, status="downloading", percent=pct, phase=phase,
                        speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            update_item(item_id, percent=100)

    def postprocessor_hook(d):
        if d["status"] == "started":
            update_item(item_id, status="processing", phase="Finalizando arquivo",
                        speed=None, eta=None)

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
        if canceled():
            raise Canceled()
        files = [p for p in out_dir.iterdir()
                 if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Arquivo final não encontrado")
        final = max(files, key=lambda p: p.stat().st_size)

        if kind == "audio":
            update_item(item_id, status="processing", phase="Convertendo para MP3")
            final = to_mp3(final, data.get("title"), data.get("uploader"))
        if canceled():
            raise Canceled()

        if CONFIG["desktop"]:
            save_dir = CONFIG["save_dir"]
            save_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(save_dir, final.name)
            shutil.move(str(final), target)
            shutil.rmtree(out_dir, ignore_errors=True)
            final = target

        update_item(item_id, status="done", percent=100, phase="Pronto",
                    speed=None, eta=None, file=str(final), saved_name=final.name,
                    finished=time.time())
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(out_dir, ignore_errors=True)
        if canceled():
            update_item(item_id, status="canceled", phase="Cancelado",
                        speed=None, eta=None, percent=0)
        else:
            app.logger.warning("download falhou: %s", exc)
            update_item(item_id, status="error", phase="Erro", error=friendly_error(exc),
                        speed=None, eta=None)
    finally:
        with lock:
            it = items.get(item_id)
            if it and it.get("remove"):
                items.pop(item_id, None)
                if item_id in order:
                    order.remove(item_id)
        persist()
        schedule()


def cleanup_old() -> None:
    """Modo site: arquivos temporários (e seus itens) expiram em 1h."""
    if CONFIG["desktop"]:
        return
    now = time.time()
    with lock:
        expired = [iid for iid, it in items.items()
                   if it["status"] == "done" and now - it.get("finished", now) > JOB_TTL_SECONDS]
        for iid in expired:
            items.pop(iid, None)
            order.remove(iid)
        busy = {iid for iid, it in items.items() if it["status"] in ACTIVE}
    work = CONFIG["work_dir"]
    if work.exists():
        for folder in work.iterdir():
            if (folder.is_dir() and folder.name not in busy
                    and now - folder.stat().st_mtime > JOB_TTL_SECONDS):
                shutil.rmtree(folder, ignore_errors=True)


def get_item_or_404(item_id: str):
    it = items.get(item_id)
    if not it:
        return None, (jsonify(error="Item não encontrado."), 404)
    return it, None


def queue_snapshot():
    with lock:
        return jsonify(items=[public(items[iid]) for iid in order], settings=SETTINGS)


# ---------- rotas ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    save = CONFIG["save_dir"]
    return jsonify(desktop=CONFIG["desktop"], save_dir=str(save) if save else None,
                   max_concurrency=MAX_CONCURRENCY)


@app.get("/api/queue")
def list_queue():
    return queue_snapshot()


@app.post("/api/queue")
def add_to_queue():
    cleanup_old()
    body = request.get_json(silent=True) or {}
    urls = body.get("urls")
    if isinstance(urls, str):
        urls = urls.split()
    if not isinstance(urls, list):
        return jsonify(error="Mande uma lista de links."), 400
    kind = body.get("kind", "video")
    if kind not in ("video", "audio"):
        return jsonify(error="Tipo inválido."), 400
    try:
        quality = parse_quality(body.get("quality"))
    except (TypeError, ValueError):
        return jsonify(error="Qualidade inválida."), 400

    cleaned, invalid, seen = [], [], set()
    for raw in urls[:MAX_URLS_PER_ADD]:
        url = str(raw).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        (cleaned if valid_youtube_url(url) else invalid).append(url)
    if not cleaned:
        return jsonify(error="Nenhum link válido do YouTube.", invalid=invalid), 400

    added, skipped = [], 0
    with lock:
        pending = {(it["url"], it["kind"]) for it in items.values()
                   if it["status"] not in FINISHED}
        for url in cleaned:
            if (url, kind) in pending:
                skipped += 1
                continue
            it = new_item(url, kind, quality)
            items[it["id"]] = it
            order.append(it["id"])
            added.append(it["id"])
    for iid in added:
        threading.Thread(target=fetch_info, args=(iid,), daemon=True).start()
    persist()
    return jsonify(added=added, invalid=invalid, skipped=skipped)


@app.patch("/api/queue/<item_id>")
def edit_item(item_id):
    body = request.get_json(silent=True) or {}
    with lock:
        it, err = get_item_or_404(item_id)
        if err:
            return err
        if it["status"] in ACTIVE or it["status"] == "done":
            return jsonify(error="Esse item já começou — não dá mais pra mudar."), 409
        if "kind" in body:
            if body["kind"] not in ("video", "audio"):
                return jsonify(error="Tipo inválido."), 400
            it["kind"] = body["kind"]
        if "quality" in body:
            try:
                it["quality"] = parse_quality(body["quality"])
            except (TypeError, ValueError):
                return jsonify(error="Qualidade inválida."), 400
        if "hold" in body:
            it["hold"] = bool(body["hold"])
    persist()
    schedule()
    return queue_snapshot()


@app.delete("/api/queue/<item_id>")
def remove_item(item_id):
    with lock:
        it, err = get_item_or_404(item_id)
        if err:
            return err
        if it["status"] in ACTIVE:
            # O worker termina, limpa os temporários e tira o item da lista.
            it.update(cancel=True, remove=True, phase="Cancelando")
        else:
            items.pop(item_id)
            order.remove(item_id)
    persist()
    return queue_snapshot()


@app.post("/api/queue/<item_id>/cancel")
def cancel_item(item_id):
    with lock:
        it, err = get_item_or_404(item_id)
        if err:
            return err
        if it["status"] in ACTIVE:
            it.update(cancel=True, phase="Cancelando")
    return queue_snapshot()


@app.post("/api/queue/<item_id>/move")
def move_item(item_id):
    direction = (request.get_json(silent=True) or {}).get("dir")
    with lock:
        if item_id not in items:
            return jsonify(error="Item não encontrado."), 404
        i = order.index(item_id)
        if direction == "up" and i > 0:
            order[i - 1], order[i] = order[i], order[i - 1]
        elif direction == "down" and i < len(order) - 1:
            order[i + 1], order[i] = order[i], order[i + 1]
        elif direction == "top":
            order.insert(0, order.pop(i))
        elif direction not in ("up", "down", "top"):
            return jsonify(error="Direção inválida."), 400
    persist()
    schedule()
    return queue_snapshot()


@app.post("/api/queue/<item_id>/retry")
def retry_item(item_id):
    with lock:
        it, err = get_item_or_404(item_id)
        if err:
            return err
        if it["status"] not in ("error", "canceled"):
            return jsonify(error="Só dá pra refazer itens com erro ou cancelados."), 409
        fetch = it["qualities"] is None
        it.update(status="fetching" if fetch else "queued",
                  phase="Buscando informações" if fetch else "Na fila",
                  error=None, percent=0, cancel=False)
    if fetch:
        threading.Thread(target=fetch_info, args=(item_id,), daemon=True).start()
    persist()
    schedule()
    return queue_snapshot()


@app.post("/api/queue/clear")
def clear_finished():
    with lock:
        for iid in [i for i in order if items[i]["status"] in FINISHED]:
            items.pop(iid)
            order.remove(iid)
    persist()
    return queue_snapshot()


@app.post("/api/settings")
def update_settings():
    body = request.get_json(silent=True) or {}
    with lock:
        if "concurrency" in body:
            SETTINGS["concurrency"] = clamp_concurrency(body["concurrency"])
        if "paused" in body:
            SETTINGS["paused"] = bool(body["paused"])
    persist()
    schedule()
    return queue_snapshot()


@app.get("/api/file/<item_id>")
def get_file(item_id):
    with lock:
        it = items.get(item_id)
        path = Path(it["file"]) if it and it["status"] == "done" and it["file"] else None
    if not path:
        return jsonify(error="Arquivo não disponível."), 404
    if not path.exists():
        return jsonify(error="Arquivo expirou."), 410
    return send_file(path, as_attachment=True, download_name=path.name)


@app.post("/api/open/<item_id>")
def open_result(item_id):
    """Modo app: abre o arquivo baixado ou mostra ele no Explorer."""
    if not CONFIG["desktop"]:
        return jsonify(error="Só disponível no app."), 404
    with lock:
        it = items.get(item_id)
        path = Path(it["file"]) if it and it.get("file") else None
    if not path:
        return jsonify(error="Arquivo não disponível."), 404
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
