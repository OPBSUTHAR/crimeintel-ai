#!/usr/bin/env python3
"""
CrimeIntel AI - Unified Launcher (Windows / macOS / Linux)

    python start.py                  # backend + frontend + browser
    python start.py --no-browser     # don't open browser
    python start.py --backend-only   # only backend (http://localhost:8000)
    python start.py --frontend-only  # only frontend (http://localhost:5175)
    python start.py --port 5175 --backend-port 8000

Starts:
  - FastAPI backend  -> http://localhost:8000  (health: /api/v1/health, docs: /api/v1/docs)
  - Vite frontend    -> http://localhost:5175  (proxies /api/v1 + /storage to backend)

Features:
  - Kills stale ports (8000, 5173, 5174, 5175, 3000)
  - Auto-creates backend/.env from .env.example if missing
  - Ensures ALLOWED_ORIGINS includes frontend port
  - Auto-checks backend venv + uvicorn, frontend node_modules
  - Streams logs with [BACKEND]/[FRONTEND] prefixes (threaded)
  - Health-checks before opening browser
  - Ctrl+C cleanly kills both process trees
"""

import argparse
import os
import sys
import subprocess
import time
import threading
import webbrowser
import urllib.request
import urllib.error
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"

DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5175


def log(msg: str, prefix: str = ""):
    print(f"{prefix}{msg}", flush=True)


def find_venv_python() -> str:
    """Return venv python if present, else sys.executable."""
    candidates = [
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",  # Windows venv
        BACKEND_DIR / ".venv" / "bin" / "python",          # Linux/macOS venv
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable


def find_npm() -> str:
    npm = shutil.which("npm")
    if npm:
        return npm
    # fallback for Windows
    for cand in ["npm.cmd", "npm.exe"]:
        found = shutil.which(cand)
        if found:
            return found
    return "npm"


def kill_port(port: int):
    """Best-effort kill of whatever is listening on port."""
    # 1) npx kill-port (fast, cross-platform)
    try:
        subprocess.run(
            ["npx", "--yes", "kill-port", str(port)],
            capture_output=True, timeout=8,
            cwd=str(ROOT),
        )
    except Exception:
        pass
    # 2) Windows fallback: netstat + taskkill
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in out.stdout.splitlines():
                if f":{port} " in line or f":{port}\t" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    if pid.isdigit() and pid != "0":
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=5
                        )
        except Exception:
            pass


def kill_stale_ports(backend_port: int, frontend_port: int):
    for p in [backend_port, 3000, 5173, 5174, frontend_port]:
        kill_port(p)


def wait_for_http(url: str, timeout: int = 40, label: str = "service") -> bool:
    log(f"Waiting for {label} {url} ...", "[WAIT] ")
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if 200 <= r.status < 400:
                    log(f"{label} ready ({r.status})", "[OK] ")
                    return True
        except Exception:
            pass
        time.sleep(1)
    log(f"{label} not ready after {timeout}s - continuing anyway", "[WARN] ")
    return False


def ensure_backend_env(frontend_port: int):
    """Create backend/.env from .env.example if missing, and patch ALLOWED_ORIGINS."""
    env_path = BACKEND_DIR / ".env"
    example_path = BACKEND_DIR / ".env.example"
    needed_origin = f"http://localhost:{frontend_port}"

    if not env_path.exists() and example_path.exists():
        shutil.copy(example_path, env_path)
        log(f"Created backend/.env from .env.example", "[CFG] ")

    if not env_path.exists():
        return

    text = env_path.read_text(encoding="utf-8", errors="ignore")
    orig = text

    if needed_origin not in text:
        if "ALLOWED_ORIGINS=" in text:
            # expand existing value
            lines = []
            for line in text.splitlines():
                if line.startswith("ALLOWED_ORIGINS="):
                    val = line.split("=", 1)[1].strip()
                    origins = [o.strip() for o in val.split(",") if o.strip()]
                    if needed_origin not in origins:
                        origins.insert(0, needed_origin)
                    # also ensure 5173 present for compat
                    if "http://localhost:5173" not in origins:
                        origins.append("http://localhost:5173")
                    line = "ALLOWED_ORIGINS=" + ",".join(origins)
                lines.append(line)
            text = "\n".join(lines) + ("\n" if orig.endswith("\n") else "")
        else:
            text = text.rstrip() + f"\nALLOWED_ORIGINS={needed_origin},http://localhost:5173\n"

        if text != orig:
            env_path.write_text(text, encoding="utf-8")
            log(f"Patched backend/.env ALLOWED_ORIGINS to include {needed_origin}", "[CFG] ")


def ensure_storage():
    storage = BACKEND_DIR / "storage"
    storage.mkdir(parents=True, exist_ok=True)


def ensure_backend_deps(venv_python: str):
    """Heal common missing deps (fpdf2) that break backend import."""
    try:
        r = subprocess.run([venv_python, "-c", "import fpdf"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return
    except Exception:
        pass
    log("Missing backend deps detected - installing fpdf2 ...", "[CFG] ")
    try:
        subprocess.run([venv_python, "-m", "pip", "install", "fpdf2", "--no-deps"], timeout=60)
    except Exception as e:
        log(f"Auto-install fpdf2 failed: {e} - run: pip install -r backend/requirements.txt", "[WARN] ")


def ensure_db_integrity(venv_python: str):
    """Auto-heal 'database disk image is malformed' - WAL corruption from forced kill."""
    db_path = BACKEND_DIR / "data" / "crimeintel.db"
    backup_path = BACKEND_DIR / "data" / "crimeintel.db.backup"
    if not db_path.exists():
        return
    try:
        # Use venv python to avoid needing sqlite3 CLI
        r = subprocess.run(
            [venv_python, "-c", "import sqlite3; conn=sqlite3.connect(r'backend/data/crimeintel.db'); cur=conn.cursor(); print(list(cur.execute('PRAGMA integrity_check'))); conn.close()"],
            capture_output=True, text=True, timeout=10, cwd=str(ROOT),
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "ok" in out.lower() and r.returncode == 0:
            return
        if "malformed" in out.lower() or r.returncode != 0:
            raise RuntimeError(out.strip() or "integrity_check failed")
    except Exception as e:
        log(f"DB integrity issue detected: {e}", "[WARN] ")
        # 1) try removing WAL/SHM (most common cause - abrupt taskkill)
        for suffix in ["-wal", "-shm"]:
            p = Path(str(db_path) + suffix)
            if p.exists():
                try:
                    p.unlink()
                    log(f"Removed stale {p.name}", "[CFG] ")
                except Exception:
                    pass
        # re-check
        try:
            r2 = subprocess.run(
                [venv_python, "-c", "import sqlite3; conn=sqlite3.connect(r'backend/data/crimeintel.db'); cur=conn.cursor(); print(list(cur.execute('PRAGMA integrity_check'))); conn.close()"],
                capture_output=True, text=True, timeout=10, cwd=str(ROOT),
            )
            if "ok" in (r2.stdout or "").lower() and r2.returncode == 0:
                log("DB recovered after removing WAL/SHM", "[OK] ")
                return
        except Exception:
            pass
        # 2) restore from backup if available
        if backup_path.exists():
            try:
                shutil.copy(str(backup_path), str(db_path))
                # clean WAL again after restore
                for suffix in ["-wal", "-shm"]:
                    p = Path(str(db_path) + suffix)
                    if p.exists():
                        try: p.unlink()
                        except: pass
                log(f"Restored DB from {backup_path.name} - recent registrations may be lost, but integrity is ok", "[CFG] ")
                # also ensure admin hash is valid (run ensure_admin equivalent inline)
                try:
                    subprocess.run([venv_python, "backend/ensure_admin.py"], capture_output=True, timeout=10, cwd=str(ROOT))
                except: pass
                return
            except Exception as e2:
                log(f"Failed to restore backup: {e2}", "[ERR] ")
        log("DB still malformed - manual fix needed: delete backend/data/crimeintel.db-wal and restart, or restore backup", "[ERR] ")


def stream_logs(name: str, proc: subprocess.Popen):
    """Stream proc stdout/stderr with prefix in a daemon thread."""
    def _reader():
        try:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                # strip trailing \r\n but keep content
                print(f"[{name}] {line.rstrip()}", flush=True)
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def start_backend(venv_python: str, backend_port: int) -> subprocess.Popen:
    log("=" * 60)
    log("Starting Backend (FastAPI) ...")
    log("=" * 60)

    # detect entrypoint
    if (BACKEND_DIR / "main.py").exists():
        entry = "main:app"
    elif (BACKEND_DIR / "app" / "main.py").exists():
        entry = "app.main:app"
    else:
        log("backend/main.py not found!", "[ERR] ")
        sys.exit(1)

    cmd = [venv_python, "-m", "uvicorn", entry, "--host", "0.0.0.0", "--port", str(backend_port)]
    log(f"$ {' '.join(cmd)}  (cwd=backend)", "[BACKEND] ")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    log(f"Backend PID {proc.pid} -> http://localhost:{backend_port}  docs: http://localhost:{backend_port}/api/v1/docs", "[BACKEND] ")
    stream_logs("BACKEND", proc)
    return proc


def start_frontend(npm: str, frontend_port: int) -> subprocess.Popen:
    log("=" * 60)
    log("Starting Frontend (Vite) ...")
    log("=" * 60)

    # npm run dev -- --port 5175 --host 0.0.0.0 --strictPort
    cmd = [npm, "run", "dev", "--", "--port", str(frontend_port), "--host", "0.0.0.0", "--strictPort"]
    log(f"$ {' '.join(cmd)}  (cwd=frontend)", "[FRONTEND] ")

    env = os.environ.copy()
    # force vite port via CLI; no need to patch file, but ensure config is 5175-compatible (vite respects CLI)
    proc = subprocess.Popen(
        cmd,
        cwd=str(FRONTEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        shell=False,
    )
    log(f"Frontend PID {proc.pid} -> http://localhost:{frontend_port}", "[FRONTEND] ")
    stream_logs("FRONTEND", proc)
    return proc


def stop(proc: subprocess.Popen | None):
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=8)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="CrimeIntel AI launcher - runs backend + frontend")
    parser.add_argument("--port", type=int, default=DEFAULT_FRONTEND_PORT, help="Frontend port (default 5175)")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT, help="Backend port (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--backend-only", action="store_true", help="Only start backend")
    parser.add_argument("--frontend-only", action="store_true", help="Only start frontend")
    args = parser.parse_args()

    frontend_port = args.port
    backend_port = args.backend_port
    frontend_url = f"http://localhost:{frontend_port}"
    backend_url = f"http://localhost:{backend_port}/api/v1/health"
    backend_docs = f"http://localhost:{backend_port}/api/v1/docs"

    print("\n" + "=" * 60)
    print(" CrimeIntel AI - Unified Launcher")
    print("=" * 60)
    print(f" Frontend : {frontend_url}  (strictPort)")
    print(f" Backend  : http://localhost:{backend_port}  -> {backend_url}")
    print(f" Docs     : {backend_docs}")
    print("=" * 60 + "\n")

    venv_python = find_venv_python()
    npm = find_npm()

    # --- preflight checks ---
    if not args.frontend_only:
        if not BACKEND_DIR.exists():
            log("backend/ directory not found!", "[ERR] "); sys.exit(1)
        # check uvicorn available in venv
        try:
            subprocess.run([venv_python, "-m", "uvicorn", "--help"], capture_output=True, timeout=5)
        except Exception:
            log(f"uvicorn not found in {venv_python} - run: pip install -r backend/requirements.txt", "[ERR] ")
            sys.exit(1)
        ensure_backend_env(frontend_port)
        ensure_storage()
        ensure_backend_deps(venv_python)
        ensure_db_integrity(venv_python)

    if not args.backend_only:
        if not (FRONTEND_DIR / "package.json").exists():
            log("frontend/package.json not found!", "[ERR] "); sys.exit(1)
        if not (FRONTEND_DIR / "node_modules").exists():
            log("frontend/node_modules missing - running npm install ...", "[WARN] ")
            subprocess.run([npm, "install"], cwd=str(FRONTEND_DIR))
        if not shutil.which("node"):
            log("node not found in PATH", "[ERR] "); sys.exit(1)

    # --- kill stale ---
    log(f"Killing stale ports {backend_port}, 5173, 5174, {frontend_port}, 3000 ...")
    kill_stale_ports(backend_port, frontend_port)
    time.sleep(1.2)

    backend_proc = None
    frontend_proc = None

    try:
        if not args.frontend_only:
            backend_proc = start_backend(venv_python, backend_port)
            wait_for_http(backend_url, timeout=30, label="backend")

        if not args.backend_only:
            frontend_proc = start_frontend(npm, frontend_port)
            wait_for_http(frontend_url, timeout=30, label="frontend")

        print("\n" + "=" * 60)
        if not args.no_browser and not args.backend_only:
            log(f"Opening {frontend_url} ...")
            try:
                webbrowser.open(frontend_url)
            except Exception:
                pass
        print(" CrimeIntel AI is running!")
        print(f"   Backend : {backend_docs}")
        print(f"   Frontend: {frontend_url}")
        print("   Press Ctrl+C to stop both servers")
        print("=" * 60 + "\n")
        log("Streaming logs [BACKEND]/[FRONTEND] - Ctrl+C to stop", "[INFO] ")

        # Keep alive + auto-restart on crash (keep other alive)
        backend_restarts = 0
        frontend_restarts = 0
        while True:
            time.sleep(1)

            if backend_proc and backend_proc.poll() is not None:
                code = backend_proc.returncode
                if code != 0:
                    log(f"Backend exited with code {code} - auto-restarting (attempt {backend_restarts+1})", "[WARN] ")
                    # drain possible error output already streamed
                    time.sleep(2)
                    if backend_restarts >= 5:
                        log("Backend restarted 5 times - giving up, keeping frontend alive", "[ERR] ")
                        backend_proc = None
                        continue
                    try:
                        backend_proc = start_backend(venv_python, backend_port)
                        wait_for_http(backend_url, timeout=15, label="backend (restart)")
                        backend_restarts += 1
                    except Exception as e:
                        log(f"Failed to restart backend: {e}", "[ERR] ")
                        backend_proc = None
                else:
                    log(f"Backend exited cleanly (code {code})", "[INFO] ")
                    break

            if frontend_proc and frontend_proc.poll() is not None:
                code = frontend_proc.returncode
                if code != 0:
                    log(f"Frontend exited with code {code} - auto-restarting (attempt {frontend_restarts+1})", "[WARN] ")
                    time.sleep(2)
                    if frontend_restarts >= 5:
                        log("Frontend restarted 5 times - giving up, keeping backend alive", "[ERR] ")
                        frontend_proc = None
                        continue
                    try:
                        frontend_proc = start_frontend(npm, frontend_port)
                        wait_for_http(frontend_url, timeout=15, label="frontend (restart)")
                        frontend_restarts += 1
                    except Exception as e:
                        log(f"Failed to restart frontend: {e}", "[ERR] ")
                        frontend_proc = None
                else:
                    log(f"Frontend exited cleanly (code {code})", "[INFO] ")
                    break

            if not backend_proc and not frontend_proc:
                log("Both processes stopped - waiting for Ctrl+C", "[INFO] ")
                time.sleep(1)

    except KeyboardInterrupt:
        log("\nCtrl+C received", "[STOP] ")
    finally:
        log("Stopping servers ...", "[STOP] ")
        if frontend_proc:
            stop(frontend_proc)
        if backend_proc:
            stop(backend_proc)
        kill_stale_ports(backend_port, frontend_port)
        log("All servers stopped. Bye!", "[STOP] ")


if __name__ == "__main__":
    main()
