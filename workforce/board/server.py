"""Board HTTP server bind + serve."""

from typing import Optional
from http.server import ThreadingHTTPServer

from .._utils import engine_port
from .handler import _Handler
from .paths import _refuse_html_escape
from ..api.roster import _BRAND_TITLE


def make_server(port: Optional[int] = None, local_root: str = "local",
                daemon: Optional[object] = None) -> ThreadingHTTPServer:
    if port is None:
        port = engine_port()  # live WORKFORCE_PORT; not import snapshot
    _refuse_html_escape()
    _Handler.local_root = local_root
    _Handler.daemon = daemon  # None = read-only board (standalone)
    # ThreadingHTTPServer so LIVE-C SSE tails do not block /api/scene.
    return ThreadingHTTPServer(("127.0.0.1", port), _Handler)


def serve(port: Optional[int] = None, local_root: str = "local") -> None:
    httpd = make_server(port, local_root)
    print("%s: engine API at http://127.0.0.1:%d (API only)" % (_BRAND_TITLE, httpd.server_address[1]))
    httpd.serve_forever()

