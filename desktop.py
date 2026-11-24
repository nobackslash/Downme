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


class WindowApi:
    """Controles da moldura própria, chamados pelo JS via window.pywebview.api.

    A janela é criada sem moldura (frameless). Arrastar e redimensionar são
    entregues ao próprio Windows (WM_NCLBUTTONDOWN com o "hit test" certo),
    então o movimento é nativo: suave, com arrastar-para-desmaximizar e o
    tamanho mínimo respeitado.
    """

    # Códigos de hit test do Win32 para cada borda/canto e para a barra de título.
    _HIT = {"move": 2, "w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14,
            "s": 15, "sw": 16, "se": 17}

    def __init__(self) -> None:
        self._window = None

    def _attach(self, window) -> None:
        self._window = window

    def _form(self):
        from webview.platforms.winforms import BrowserView
        return BrowserView.instances.get(self._window.uid) if self._window else None

    def _ui(self, fn):
        """Roda fn na thread da interface (WinForms) e devolve o resultado."""
        from System import Func, Type
        form = self._form()
        if form is None:
            return None
        box = []
        form.Invoke(Func[Type](lambda: box.append(fn(form))))
        return box[0] if box else None

    @staticmethod
    def _fit_work_area(form) -> None:
        # Sem moldura, o WinForms maximiza por cima da barra de tarefas;
        # limitar à área de trabalho do monitor atual resolve.
        from System.Drawing import Rectangle
        from System.Windows.Forms import Screen
        scr = Screen.FromHandle(form.Handle)
        wa, b = scr.WorkingArea, scr.Bounds
        form.MaximizedBounds = Rectangle(wa.X - b.X, wa.Y - b.Y, wa.Width, wa.Height)

    def begin(self, where: str) -> None:
        hit = self._HIT.get(where)
        if hit is None:
            return
        if hit != 2 and self.is_maximized():
            return  # janela maximizada não redimensiona pelas bordas

        def go(form):
            import ctypes
            from ctypes import wintypes
            self._fit_work_area(form)
            pt = wintypes.POINT()
            user32 = ctypes.windll.user32
            user32.GetCursorPos(ctypes.byref(pt))
            lparam = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
            user32.ReleaseCapture()
            user32.PostMessageW(wintypes.HWND(form.Handle.ToInt64()), 0x00A1, hit, lparam)

        self._ui(go)

    def is_maximized(self) -> bool:
        from System.Windows.Forms import FormWindowState
        return bool(self._ui(lambda f: f.WindowState == FormWindowState.Maximized))

    def toggle_maximize(self) -> bool:
        from System.Windows.Forms import FormWindowState

        def go(form):
            if form.WindowState == FormWindowState.Maximized:
                form.WindowState = FormWindowState.Normal
                return False
            self._fit_work_area(form)
            form.WindowState = FormWindowState.Maximized
            return True

        return bool(self._ui(go))

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        self._window.destroy()


def _round_corners(window) -> None:
    """Cantos arredondados do Windows 11 (no 10 a chamada é só ignorada)."""
    try:
        import ctypes
        from webview.platforms.winforms import BrowserView
        form = BrowserView.instances[window.uid]
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(form.Handle.ToInt64()), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        log.exception("não deu para arredondar os cantos")


def main() -> None:
    log.info("Downme %s", updater.VERSION)
    updater.cleanup_old()
    log.info("iniciando (frozen=%s, ffmpeg=%s, js=%s)",
             backend.FROZEN, backend.FFMPEG, backend.BASE_OPTS["js_runtimes"])
    backend.configure(desktop=True, state_file=LOG_DIR / "queue.json")

    # Porta 0 = o sistema escolhe uma livre; escuta só em 127.0.0.1.
    server = make_server("127.0.0.1", 0, backend.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    chrome = WindowApi()
    window = webview.create_window(
        "Downme",
        f"http://127.0.0.1:{server.server_port}/",
        width=880,
        height=860,
        min_size=(460, 620),
        background_color="#ffffff",
        frameless=True,
        easy_drag=False,   # o arrasto é feito pela barra de título da página
        shadow=True,
        js_api=chrome,
    )
    chrome._attach(window)
    window.events.shown += lambda: _round_corners(window)
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
