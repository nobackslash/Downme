"""Downme como app desktop (vira Downme.exe com o PyInstaller).

Sobe o backend Flask numa porta livre só da sua máquina e abre a mesma
interface numa janela nativa (WebView2 do Windows) — sem navegador.
Os arquivos vão para Downloads\\Downme. A fila pendente fica salva em
%LOCALAPPDATA%\\Downme\\queue.json e é retomada na próxima abertura.
"""

import faulthandler
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

# Log em %LOCALAPPDATA%\Downme\downme.log — num .exe sem console é o único
# jeito de ver erros (stdout/stderr são None e quebrariam o Flask/yt-dlp).
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Downme"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "downme.log"
_log_stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stdout is None:
    sys.stdout = _log_stream
if sys.stderr is None:
    sys.stderr = _log_stream
faulthandler.enable(file=_log_stream)
logging.basicConfig(
    stream=_log_stream, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("downme")
# A interface consulta a fila a cada segundo; sem isso o log cresceria sem parar.
logging.getLogger("werkzeug").setLevel(logging.WARNING)
sys.excepthook = lambda t, v, tb: log.critical("erro não tratado", exc_info=(t, v, tb))
threading.excepthook = lambda a: log.critical(
    "erro em thread", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

import webview  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import app as backend  # noqa: E402
import updater  # noqa: E402


def main() -> None:
    log.info("Downme %s", updater.VERSION)
    updater.cleanup_old()
    log.info("iniciando (frozen=%s, ffmpeg=%s, js=%s)",
             backend.FROZEN, backend.FFMPEG, backend.BASE_OPTS["js_runtimes"])
    backend.configure(desktop=True, state_file=LOG_DIR / "queue.json")

    # Porta 0 = o sistema escolhe uma livre; escuta só em 127.0.0.1.
    server = make_server("127.0.0.1", 0, backend.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    window = webview.create_window(
        "Downme",
        f"http://127.0.0.1:{server.server_port}/",
        width=880,
        height=860,
        min_size=(460, 620),
        background_color="#0e0f13",
    )
    backend.CONFIG["on_restart"] = lambda: threading.Timer(0.3, window.destroy).start()
    log.info("servidor em 127.0.0.1:%s", server.server_port)
    updater.start_background_checks()
    webview.start()
    log.info("janela fechada, encerrando")
    server.shutdown()
    backend.persist()
    updater.apply_pending()  # instala atualização já baixada (e reabre, se pedido)


if __name__ == "__main__":
    main()
