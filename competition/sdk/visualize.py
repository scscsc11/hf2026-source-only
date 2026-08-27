"""Optional 3D visualization launcher.

When a player passes ``--visualize``, the runner starts:
  1. the Redis↔WebSocket **bridge**, which relays ``sim:state``/``sim:events``
     to browser clients on WS :8080;
  2. a tiny static HTTP server over ``visualization/dist`` (the prebuilt
     frontend), so the player just opens a URL — no webpack/npm needed;
  3. opens the browser to that URL.

The bridge is TS source (``visualization/src/bridge``). Running it via
``npx ts-node`` hangs on Node 24 + ts-node 10.x (the process never reaches
``main()``), so we instead **compile it once to plain JS** (via
``npx tsc -p tsconfig.bridge.json`` → ``visualization/dist-bridge``) and run
that with ``node``. The compiled artifact is cached; only the first run pays
the ~30 s compile. If compilation fails we fall back to the ts-node path and
emit a warning. The bridge's stderr is tee'd to ``visualization/bridge.stderr.log``
so a silent start failure leaves a diagnostic trail (it used to be discarded
to DEVNULL, which made "frontend says disconnected" undiagnosable).

Both subprocesses are terminated when the run ends.

The visualization is a **bystander**: it only reads Redis, never participates
in the control loop. A player can run with or without it; scores are
identical.

Layout assumptions (release): the engine repo root is resolved as the parent
of ``competition/``. The visualization lives under ``<root>/visualization``.
For a minimal release that ships only ``competition/`` + the binary, copy the
``visualization/`` folder in too (or set ``--viz-dir``).
"""
from __future__ import annotations

import http.server
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional


# repo root = parent of competition/  (this file is competition/sdk/visualize.py)
_REPO_ROOT = Path(__file__).resolve().parents[2]

WS_PORT_DEFAULT = 8080          # bridge WebSocket
VIZ_HTTP_PORT_DEFAULT = 3000    # static frontend server


def _resolve_viz_dir(viz_dir: Optional[str]) -> Optional[Path]:
    d = Path(viz_dir) if viz_dir else _REPO_ROOT / "visualization"
    return d if (d / "dist" / "index.html").exists() else None


def _resolve_bridge_script(viz_dir: Path) -> Optional[Path]:
    """The TS source of the bridge entry point, or None if absent."""
    ts = viz_dir / "src" / "bridge" / "index.ts"
    return ts if ts.exists() else None


def _compiled_bridge_entry(viz_dir: Path) -> Optional[Path]:
    """The compiled JS entry of the bridge, if it has been built."""
    js = viz_dir / "dist-bridge" / "bridge" / "index.js"
    return js if js.exists() else None


def _ensure_bridge_compiled(viz_dir: Path, log=print) -> Optional[Path]:
    """Compile the bridge TS → JS once (cached), return the JS entry path.

    Running the bridge via ``npx ts-node`` hangs on Node 24 + ts-node 10.x,
    so we compile to plain JS (``npx tsc -p tsconfig.bridge.json``) and run
    that with ``node``. The artifact lands in ``dist-bridge/`` and is reused
    on subsequent runs; only the first run pays the ~30 s compile. Returns
    None if the TS source or tsc is unavailable, in which case the caller
    falls back to the ts-node path.
    """
    entry = _compiled_bridge_entry(viz_dir)
    if entry is not None:
        return entry
    ts = viz_dir / "src" / "bridge" / "index.ts"
    tsconfig = viz_dir / "tsconfig.bridge.json"
    if not ts.exists() or not tsconfig.exists():
        return None
    log("[viz] compiling bridge TS → JS (one-time, ~30 s)...")
    try:
        subprocess.run(
            ["npx", "tsc", "-p", str(tsconfig)],
            cwd=str(viz_dir),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=(sys.platform == "win32"),   # npx.cmd shim on Windows
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as ex:
        log(f"[viz] bridge compile failed ({ex}); falling back to ts-node")
        return None
    return _compiled_bridge_entry(viz_dir)


class _StaticServer(threading.Thread):
    """Serves the prebuilt 3D frontend on a background thread.

    The frontend bundle (index.html, bundle.js) lives in ``visualization/dist``;
    it also fetches ``/heightmap.json`` (and other assets) from
    ``visualization/public`` at runtime. We serve a single merged view: dist
    as-is, with any public/ asset not already present in dist copied in
    (cheap — public is tiny). This avoids a two-directory request fallback.
    """

    def __init__(self, viz_dir: Path, port: int):
        super().__init__(daemon=True)
        self.viz_dir = viz_dir
        self.port = port
        self.httpd: Optional[socketserver.TCPServer] = None
        self._serve_dir = self._materialize()

    def _materialize(self) -> Path:
        """Return a dir to serve = dist + public/ assets merged in.

        Copies public/ files into dist/ if missing (idempotent; public is
        ~3 MB). Uses dist in place so bundle.js etc. are served as built.
        """
        dist = self.viz_dir / "dist"
        public = self.viz_dir / "public"
        if dist.exists() and public.exists():
            import shutil
            for p in public.iterdir():
                dst = dist / p.name
                if p.is_file() and not dst.exists():
                    try:
                        shutil.copy2(p, dst)
                    except Exception:
                        pass  # non-fatal — best effort
        return dist if dist.exists() else public

    def run(self) -> None:
        serve = self._serve_dir
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
            *a, directory=str(serve), **kw)
        try:
            self.httpd = socketserver.TCPServer(
                ("127.0.0.1", self.port), handler)
            self.httpd.serve_forever()
        except OSError:
            # port in use — assume something else is already serving
            pass


class VisualizationSession:
    """Manages the bridge + static-server subprocesses for one run.

    Use as a context manager around the run loop::

        with VisualizationSession.start(...) as viz:
            ...  # run the scenario
        # bridge + server auto-stopped on exit
    """

    def __init__(self, *, bridge_proc, static: _StaticServer,
                 url: str, log=print, stderr_log=None):
        self._bridge = bridge_proc
        self._static = static
        self.url = url
        self._log = log
        self._stderr_log = stderr_log

    @classmethod
    def start(cls, *, viz_dir: Optional[str] = None,
              ws_port: int = WS_PORT_DEFAULT,
              http_port: int = VIZ_HTTP_PORT_DEFAULT,
              redis_host: str = "127.0.0.1", redis_port: int = 6379,
              open_browser: bool = True, log=print) -> Optional["VisualizationSession"]:
        viz = _resolve_viz_dir(viz_dir)
        if viz is None:
            log("[viz] visualization/dist not found — pass --viz-dir or copy "
                "the visualization/ folder in. Running without 3D view.")
            return None
        bridge_script = _resolve_bridge_script(viz)
        if bridge_script is None:
            log("[viz] visualization/src/bridge/index.ts not found — cannot "
                "start the bridge. Running without 3D view.")
            return None

        # Decide HOW to run the bridge. Prefer the compiled JS (run via node),
        # because npx ts-node hangs on Node 24 + ts-node 10.x. Compile once on
        # demand; fall back to ts-node if tsc is unavailable.
        compiled = _ensure_bridge_compiled(viz, log=log)
        if compiled is not None:
            cmd = ["node", str(compiled)]
            via = f"node {compiled.name}"
        else:
            cmd = ["npx", "ts-node", str(bridge_script)]
            via = f"ts-node {bridge_script.name}"

        # Inherit the parent environment and layer the bridge's env on top.
        # Node.js needs SYSTEMROOT/TEMP/etc. on Windows (its CSPRNG init
        # crashes with "Assertion failed: ncrypto::CSPRNG" if those are gone),
        # so we must NOT replace the env wholesale — only override the keys
        # the bridge cares about.
        import os
        env = dict(os.environ)
        env.update({
            "WS_PORT": str(ws_port),
            "REDIS_HOST": redis_host, "REDIS_PORT": str(redis_port),
        })
        # Keep the bridge's stderr for post-mortem (it used to be DEVNULL,
        # which made a silent start failure undiagnosable). stdout is still
        # suppressed; stderr is tee'd to a log file under the viz dir.
        stderr_log = open(viz / "bridge.stderr.log", "ab", buffering=0)
        log(f"[viz] starting bridge ({via}, ws=:{ws_port})...")
        popen_kwargs = dict(
            cwd=str(viz), env=env,
            stdout=subprocess.DEVNULL, stderr=stderr_log,
        )
        try:
            if sys.platform == "win32" and cmd[0] == "npx":
                # npx is a .cmd shim on Windows → needs shell=True to resolve.
                bridge_proc = subprocess.Popen(
                    " ".join(cmd), shell=True, **popen_kwargs)
            else:
                bridge_proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            stderr_log.close()
            log("[viz] node/npx not found on PATH — install Node.js to use "
                "--visualize. Running without 3D view.")
            return None

        # 2) static frontend server (daemon thread) — dist + public merged
        static = _StaticServer(viz, http_port)
        static.start()
        time.sleep(0.4)

        url = f"http://127.0.0.1:{http_port}/"
        log(f"[viz] 3D view ready at {url}  (bridge ws=:{ws_port})")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return cls(bridge_proc=bridge_proc, static=static, url=url, log=log,
                   stderr_log=stderr_log)

    def stop(self) -> None:
        if self._bridge is not None and self._bridge.poll() is None:
            self._bridge.terminate()
            try:
                self._bridge.wait(timeout=3)
            except Exception:
                self._bridge.kill()
        if self._static is not None and self._static.httpd is not None:
            try:
                self._static.httpd.shutdown()
            except Exception:
                pass
        if self._stderr_log is not None:
            try:
                self._stderr_log.close()
            except Exception:
                pass
        self._log("[viz] stopped")

    def __enter__(self) -> "VisualizationSession":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
