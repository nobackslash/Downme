"""Atualização automática do Downme.exe via GitHub Releases.

Fluxo:
  1. Ao abrir (e a cada 6h) consulta a última release do repositório.
  2. Se a tag (ex.: v1.2.0) for maior que VERSION, baixa o asset Downme.exe
     para "Downme.exe.new" ao lado do executável e confere o SHA-256
     publicado pelo GitHub (campo `digest` do asset ou um Downme.exe.sha256).
  3. A troca acontece ao fechar o app ou no botão "Reiniciar agora":
     o .exe em uso é renomeado para "Downme.old.exe" (o Windows permite
     renomear, não apagar) e o novo assume o nome. O .old é apagado na
     próxima abertura.

Só roda no .exe (PyInstaller); rodando pelo Python não faz nada.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from version import REPO, VERSION

log = logging.getLogger("downme.update")

API_URL = os.environ.get(
    "DOWNME_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
ASSET_NAME = "Downme.exe"
CHECK_EVERY = 6 * 60 * 60
USER_AGENT = f"Downme/{VERSION} (+https://github.com/{REPO})"

ENABLED = getattr(sys, "frozen", False) and os.name == "nt"
EXE = Path(sys.executable)
NEW_EXE = EXE.with_name(EXE.stem + ".exe.new")
OLD_EXE = EXE.with_name(EXE.stem + ".old.exe")

_lock = threading.Lock()
STATE = {
    "current": VERSION,
    "status": "idle",   # idle | checking | latest | downloading | ready | error | disabled
    "latest": None,
    "notes": None,
    "url": None,        # página da release
    "percent": 0,
    "error": None,
    "checked_at": None,
}
_restart_requested = False


def snapshot() -> dict:
    with _lock:
        return dict(STATE)


def _set(**fields) -> None:
    with _lock:
        STATE.update(fields)


def parse_version(tag: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def _get(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return urllib.request.urlopen(req, timeout=30)  # noqa: S310 (URL fixa do GitHub)


def cleanup_old() -> None:
    """Apaga o executável antigo deixado pela última atualização."""
    if not ENABLED:
        return
    for leftover in (OLD_EXE, NEW_EXE.with_suffix(".part")):
        for _ in range(10):  # o processo antigo pode levar um instante para sair
            try:
                leftover.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)


def check_and_download() -> None:
    if not ENABLED:
        _set(status="disabled")
        return
    with _lock:
        if STATE["status"] in ("checking", "downloading", "ready"):
            return
        STATE.update(status="checking", error=None)
    try:
        with _get(API_URL) as res:
            release = json.load(res)
        tag = release.get("tag_name", "")
        _set(checked_at=time.time(), latest=tag.lstrip("v"),
             notes=(release.get("body") or "").strip()[:1500] or None,
             url=release.get("html_url"))
        if release.get("draft") or release.get("prerelease") \
                or parse_version(tag) <= parse_version(VERSION):
            _set(status="latest")
            return

        asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
        if not asset:
            raise RuntimeError(f"release {tag} sem {ASSET_NAME}")
        expected = _expected_sha256(release, asset)
        _download(asset["browser_download_url"], asset.get("size") or 0, expected)
        _set(status="ready", percent=100)
        log.info("atualização %s baixada e verificada", tag)
    except Exception as exc:  # noqa: BLE001
        log.warning("verificação de atualização falhou: %s", exc)
        _set(status="error", error="Não consegui verificar atualizações agora.")


def _expected_sha256(release: dict, asset: dict) -> str:
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    side = next((a for a in release.get("assets", [])
                 if a.get("name") == ASSET_NAME + ".sha256"), None)
    if side:
        with _get(side["browser_download_url"], "application/octet-stream") as res:
            text = res.read(4096).decode("utf-8", "replace")
        m = re.search(r"\b[0-9a-fA-F]{64}\b", text)
        if m:
            return m.group(0).lower()
    raise RuntimeError("release sem hash SHA-256 para conferir o download")


def _download(url: str, size: int, expected: str) -> None:
    part = NEW_EXE.with_suffix(".part")
    sha = hashlib.sha256()
    done = 0
    _set(status="downloading", percent=0)
    with _get(url, "application/octet-stream") as res, open(part, "wb") as fh:
        total = size or int(res.headers.get("Content-Length") or 0)
        while chunk := res.read(1024 * 256):
            fh.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if total:
                _set(percent=round(done / total * 100))
    if sha.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise RuntimeError("hash do download não confere — arquivo descartado")
    os.replace(part, NEW_EXE)


def start_background_checks() -> None:
    if not ENABLED:
        _set(status="disabled")
        return

    def loop():
        time.sleep(3)  # deixa a janela abrir primeiro
        while True:
            check_and_download()
            if snapshot()["status"] == "ready":
                return
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, daemon=True, name="updater").start()


def request_restart() -> bool:
    global _restart_requested
    if snapshot()["status"] != "ready":
        return False
    _restart_requested = True
    return True


def apply_pending() -> None:
    """Chamado ao fechar a janela: instala a versão baixada e, se pedido, reabre."""
    if not ENABLED or snapshot()["status"] != "ready" or not NEW_EXE.exists():
        return
    try:
        OLD_EXE.unlink(missing_ok=True)
        os.replace(EXE, OLD_EXE)
        try:
            os.replace(NEW_EXE, EXE)
        except OSError:
            os.replace(OLD_EXE, EXE)  # desfaz para não deixar o app sem executável
            raise
        log.info("atualização instalada em %s", EXE)
    except OSError as exc:
        log.warning("não consegui instalar a atualização: %s", exc)
        return
    if _restart_requested:
        env = dict(os.environ)
        # Sem isso o novo .exe (PyInstaller) herda a pasta temporária deste.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([str(EXE)], env=env, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
