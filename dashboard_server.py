"""
dashboard_server.py — Flask сервер with DB-бекендом

Все yesнные читаются/пишутся via db.py. Ниasих файлов кроме ghost_shell.db
(плюс payload_debug.json в профиле for C++ ядра).

Overпуск:
    python dashboard_server.py
    → http://127.0.0.1:5000
"""

import os
import sys
import json
import time
import queue
import logging
import threading
import subprocess
from datetime import datetime

try:
    from flask import Flask, request, jsonify, send_file, Response
    from flask_cors import CORS
except ImportError:
    print("pip install flask flask-cors")
    sys.exit(1)

from db import get_db
from platform_paths import popen_flags_no_console, terminate_process_tree


def _popen_no_console_flags():
    """Cross-platform Popen flags that (a) don't pop a console on Windows,
    (b) put the child in its own process group on Unix."""
    return popen_flags_no_console()


# ──────────────────────────────────────────────────────────────
# RUNNER STATE
# ──────────────────────────────────────────────────────────────

class RunnerState:
    def __init__(self):
        self.is_running       = False
        self.current_run_id   = None
        self.profile_name     = None          # which profile is active
        self.thread           = None
        self.process          = None          # subprocess handle (for stop)
        self.started_at       = None
        self.finished_at      = None
        self.last_exit_code   = None
        self.last_error       = None
        self.log_queue        = queue.Queue(maxsize=1000)

    def log(self, message: str, level: str = "info"):
        entry = {
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "level":    level,
            "message":  message,
        }
        try:
            self.log_queue.put_nowait(entry)
        except queue.Full:
            try:
                self.log_queue.get_nowait()
                self.log_queue.put_nowait(entry)
            except Exception:
                pass
        # Write to DB log table
        try:
            get_db().log_add(self.current_run_id, level, message)
        except Exception:
            pass


RUNNER = RunnerState()


def cleanup_stale_runs():
    """
    Mark all runs stuck in 'running' state as failed on dashboard startup.
    Happens when dashboard/main.py crashes without calling run_finish().
    """
    try:
        db = get_db()
        conn = db._get_conn()
        rows = conn.execute("""
            SELECT id FROM runs
            WHERE finished_at IS NULL AND exit_code IS NULL
        """).fetchall()
        for row in rows:
            conn.execute("""
                UPDATE runs
                SET finished_at = ?, exit_code = -99, error = ?
                WHERE id = ?
            """, (
                datetime.now().isoformat(timespec="seconds"),
                "stale: process not found on dashboard restart",
                row["id"],
            ))
        if rows:
            logging.info(f"[startup] Cleaned up {len(rows)} stale run(s)")
    except Exception as e:
        logging.error(f"[startup] Stale cleanup failed: {e}")


# ──────────────────────────────────────────────────────────────
# FLASK
# ──────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="dashboard", static_url_path="")
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.route("/")
def index():
    """Serve the dashboard SPA entry page."""
    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "dashboard", "index.html"
    )
    if not os.path.exists(html_path):
        return (
            "<h1>dashboard/index.html not found</h1>"
            "<p>Create the <code>dashboard/</code> folder next to dashboard_server.py</p>",
            404
        )
    return send_file(html_path)


# ──────────────────────────────────────────────────────────────
# API: CONFIG
# ──────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        return jsonify(get_db().config_get_all())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_set():
    try:
        data = request.get_json(force=True)
        get_db().config_set_all(data)
        return jsonify({"ok": True, "saved_at": datetime.now().isoformat(timespec="seconds")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: STATS
# ──────────────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def api_stats():
    db = get_db()
    total_summary = db.events_summary(hours=24 * 365)  # all события
    total_comp, unique_domains = db.competitors_count()
    profiles = db.profiles_list()

    # Post-click action counters — rolled up from action_events
    actions_24h = db.action_events_summary(hours=24)
    actions_all = db.action_events_summary(hours=24 * 365)

    # Build info — which Chromium / Chrome versions ship with this binary
    try:
        from device_templates import CHROMIUM_BUILD, CHROMIUM_BUILD_FULL, CHROME_VERSIONS
        stable_chrome = CHROME_VERSIONS[0]["major"] if CHROME_VERSIONS else "?"
        # User-configurable spoof range (major version bounds)
        spoof_min = db.config_get("browser.spoof_chrome_min") or None
        spoof_max = db.config_get("browser.spoof_chrome_max") or None
        build_info = {
            "chromium_build":      CHROMIUM_BUILD,
            "chromium_build_full": CHROMIUM_BUILD_FULL,
            "chrome_spoof":        stable_chrome,     # top of the pool
            "chrome_pool":         [v["major"] for v in CHROME_VERSIONS],
            "chrome_pool_full":    [v["full"]  for v in CHROME_VERSIONS],
            "spoof_min":           spoof_min,
            "spoof_max":           spoof_max,
        }
    except Exception:
        build_info = {}

    return jsonify({
        "total_profiles":    len(profiles),
        "total_searches":    total_summary.get("search_ok", 0),
        "total_captchas":    total_summary.get("captcha", 0),
        "total_blocks":      total_summary.get("blocked", 0),
        "total_competitors": total_comp,
        "unique_domains":    unique_domains,
        "daily":             db.daily_stats(days=14),
        "run_status":        get_run_status_dict(),
        # Post-click action stats (24h and all-time)
        "actions_24h":       actions_24h,
        "actions_total":     actions_all,
        "build_info":        build_info,
    })


# ──────────────────────────────────────────────────────────────
# API: PROFILES
# ──────────────────────────────────────────────────────────────

@app.route("/api/profiles", methods=["GET"])
def api_profiles():
    return jsonify(get_db().profiles_list())


@app.route("/api/profiles/<name>/selfcheck", methods=["GET"])
def api_profile_selfcheck(name: str):
    sc = get_db().selfcheck_latest(name)
    if not sc:
        return jsonify({"error": "Нет selfcheck. Overпусти мониторинг"}), 404
    return jsonify(sc)


@app.route("/api/profiles/<name>/selfcheck/history", methods=["GET"])
def api_profile_selfcheck_history(name: str):
    return jsonify(get_db().selfchecks_history(name, limit=20))


@app.route("/api/profiles/<name>/fingerprint", methods=["GET"])
def api_profile_fingerprint(name: str):
    fp = get_db().fingerprint_current(name)
    if not fp:
        return jsonify({"error": "Нет fingerprint"}), 404
    return jsonify(fp["payload"])


@app.route("/api/profiles/<name>/reset-health", methods=["POST"])
def api_profile_reset_health(name: str):
    """Reset consecutive blocks marker — use after fixing the root cause."""
    try:
        path = os.path.join("profiles", name)
        if not os.path.exists(path):
            return jsonify({"error": f"Profile {name} not found"}), 404
        from session_quality import SessionQualityMonitor
        sqm = SessionQualityMonitor(path)
        sqm.reset_consecutive_blocks()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>/clear-session-quality", methods=["POST"])
def api_profile_clear_session_quality(name: str):
    """Delete entire session_quality.json — fresh start."""
    try:
        path = os.path.join("profiles", name)
        if not os.path.exists(path):
            return jsonify({"error": f"Profile {name} not found"}), 404
        from session_quality import SessionQualityMonitor
        sqm = SessionQualityMonitor(path)
        sqm.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: COMPETITORS
# ──────────────────────────────────────────────────────────────

@app.route("/api/competitors", methods=["GET"])
def api_competitors():
    db = get_db()
    total, unique = db.competitors_count()
    by_domain = db.competitors_by_domain()

    # Merge in per-domain action counters (how many times did we click /
    # interact with an ad on this domain — not just how often we saw it)
    actions_by_domain = db.action_events_by_domain()
    for row in by_domain:
        stats = actions_by_domain.get(row["domain"], {})
        row["actions_ran"]     = stats.get("ran", 0)
        row["actions_skipped"] = stats.get("skipped", 0)
        row["actions_errored"] = stats.get("errored", 0)
        row["last_action_at"]  = stats.get("last_action_at")

    return jsonify({
        "total_records":  total,
        "unique_domains": unique,
        "by_domain":      by_domain,
        "recent":         db.competitors_recent(limit=100),
    })


# ──────────────────────────────────────────────────────────────
# API: IPs
# ──────────────────────────────────────────────────────────────

@app.route("/api/ips", methods=["GET"])
def api_ips():
    return jsonify(get_db().ip_stats())


# ──────────────────────────────────────────────────────────────
# API: RUNS
# ──────────────────────────────────────────────────────────────

@app.route("/api/runs", methods=["GET"])
def api_runs():
    return jsonify(get_db().runs_list(limit=50))


def get_run_status_dict():
    return {
        "is_running":     RUNNER.is_running,
        "current_run_id": RUNNER.current_run_id,
        "profile_name":   RUNNER.profile_name,
        "started_at":     RUNNER.started_at,
        "finished_at":    RUNNER.finished_at,
        "last_exit_code": RUNNER.last_exit_code,
        "last_error":     RUNNER.last_error,
    }


@app.route("/api/run/status", methods=["GET"])
def api_run_status():
    return jsonify(get_run_status_dict())


@app.route("/api/run", methods=["POST"])
def api_run():
    if RUNNER.is_running:
        return jsonify({"error": "Monitor already запущен"}), 409

    def run_thread():
        db = get_db()
        profile_name = db.config_get("browser.profile_name", "profile_01")
        proxy_url    = db.config_get("proxy.url", "")
        run_id = db.run_start(profile_name, proxy_url)

        RUNNER.is_running     = True
        RUNNER.current_run_id = run_id
        RUNNER.profile_name   = profile_name
        RUNNER.started_at     = datetime.now().isoformat(timespec="seconds")
        RUNNER.finished_at    = None
        RUNNER.last_exit_code = None
        RUNNER.last_error     = None

        RUNNER.log(f"Overпуск #{run_id} мониторинга (profile: {profile_name})...", "info")

        try:
            env = os.environ.copy()
            env["GHOST_SHELL_RUN_ID"] = str(run_id)

            proc = subprocess.Popen(
                [sys.executable, "-u", "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=env,
            )
            RUNNER.process = proc   # so /api/run/stop can terminate it

            # ── Monitor chrome process tree ─────────────────────────────
            # When the user closes the Chrome window manually, main.py can
            # take 30+ seconds to notice (it's blocked on selenium calls that
            # only error out on timeout). We proactively watch chrome.exe
            # under our subprocess and kill main.py instantly when Chrome
            # disappears — so the "Running" status reflects reality.
            chrome_ever_seen = {"value": False}
            monitor_stop     = threading.Event()

            def _chrome_monitor():
                try:
                    import psutil
                    ps_proc = psutil.Process(proc.pid)
                except Exception:
                    return
                no_chrome_seen_at = None
                while not monitor_stop.is_set():
                    if proc.poll() is not None:
                        return
                    try:
                        children = ps_proc.children(recursive=True)
                    except Exception:
                        return
                    chrome_kids = [c for c in children
                                   if c.name().lower() == "chrome.exe"]
                    if chrome_kids:
                        chrome_ever_seen["value"] = True
                        no_chrome_seen_at = None
                    elif chrome_ever_seen["value"]:
                        # Chrome was running, now it's gone — probably closed
                        # by the user. Give it ~3s grace in case it's mid-restart.
                        if no_chrome_seen_at is None:
                            no_chrome_seen_at = time.time()
                        elif time.time() - no_chrome_seen_at > 3:
                            RUNNER.log(
                                "Chrome window closed — stopping monitor",
                                "warning")
                            try:
                                for child in children:
                                    try:  child.terminate()
                                    except Exception: pass
                                proc.terminate()
                            except Exception:
                                pass
                            return
                    time.sleep(1)

            mon_thread = threading.Thread(target=_chrome_monitor, daemon=True)
            mon_thread.start()

            # ── stdout → UI log (blocking, but OK because we have monitor) ─
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    lvl = "info"
                    if "ERROR" in line:   lvl = "error"
                    elif "WARN" in line:  lvl = "warning"
                    RUNNER.log(line, lvl)
            except Exception as e:
                RUNNER.log(f"stdout reader: {e}", "debug")

            monitor_stop.set()
            proc.wait()
            RUNNER.last_exit_code = proc.returncode

            # Podведение итогов for runs
            total = db.events_summary(hours=24)  # грубо
            db.run_finish(
                run_id,
                exit_code    = proc.returncode,
                total_queries= total.get("search_ok", 0) + total.get("search_empty", 0),
                total_ads    = total.get("search_ok", 0),
                captchas     = total.get("captcha", 0),
            )
            RUNNER.log(f"Monitor #{run_id} finished (code {proc.returncode})",
                       "info" if proc.returncode == 0 else "error")

        except Exception as e:
            RUNNER.last_error = str(e)
            db.run_finish(run_id, exit_code=-1, error=str(e))
            RUNNER.log(f"Error запуска: {e}", "error")
        finally:
            RUNNER.is_running  = False
            RUNNER.finished_at = datetime.now().isoformat(timespec="seconds")

    t = threading.Thread(target=run_thread, daemon=True)
    RUNNER.thread = t
    t.start()

    return jsonify({"ok": True, "started_at": RUNNER.started_at})


@app.route("/api/run/stop", methods=["POST"])
def api_run_stop():
    """Forcefully terminate the running monitor subprocess + its children.

    Uses psutil to walk the process tree (python.exe spawned main.py which
    spawned chrome.exe — we need to kill them all, otherwise chrome stays
    orphaned and the run never actually stops).
    """
    if not RUNNER.is_running or not RUNNER.process:
        return jsonify({"error": "No run in progress"}), 409

    try:
        import psutil
        parent_pid = RUNNER.process.pid
        RUNNER.log(f"Stop requested — killing process tree of PID {parent_pid}", "warning")

        killed = []
        try:
            parent = psutil.Process(parent_pid)
            # Kill children first (chrome.exe, crashpad_handler.exe, ...)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                    killed.append(f"{child.name()}({child.pid})")
                except psutil.NoSuchProcess:
                    pass
            # Then the parent python
            parent.kill()
            killed.append(f"{parent.name()}({parent.pid})")
        except psutil.NoSuchProcess:
            pass

        # Wait briefly so the run-watcher thread notices and marks finished_at
        try:
            RUNNER.process.wait(timeout=5)
        except Exception:
            pass

        # Force-mark the run as failed in DB just in case the watcher didn't
        if RUNNER.current_run_id:
            try:
                get_db()._get_conn().execute("""
                    UPDATE runs
                    SET finished_at = ?, exit_code = -99,
                        error = COALESCE(error, 'stopped by user')
                    WHERE id = ? AND finished_at IS NULL
                """, (datetime.now().isoformat(timespec="seconds"),
                      RUNNER.current_run_id))
            except Exception as e:
                logging.error(f"mark-failed on stop: {e}")

        RUNNER.is_running  = False
        RUNNER.finished_at = datetime.now().isoformat(timespec="seconds")
        RUNNER.log(f"Killed: {', '.join(killed) if killed else '(nothing)'}", "warning")

        return jsonify({"ok": True, "killed": killed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/runs/<int:run_id>/mark-failed", methods=["POST"])
def api_run_mark_failed(run_id: int):
    """Manually mark a stuck run as failed (useful for cleanup)."""
    try:
        db = get_db()
        db._get_conn().execute("""
            UPDATE runs
            SET finished_at = ?, exit_code = -99, error = ?
            WHERE id = ? AND finished_at IS NULL
        """, (
            datetime.now().isoformat(timespec="seconds"),
            "manually marked as failed via dashboard",
            run_id,
        ))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>", methods=["DELETE"])
def api_profile_delete(name: str):
    """
    Delete an entire profile — removes the folder AND purges DB rows for it.
    """
    try:
        import shutil
        profile_dir = os.path.join("profiles", name)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)

        db = get_db()
        conn = db._get_conn()
        for table in ("events", "selfchecks", "fingerprints"):
            conn.execute(f"DELETE FROM {table} WHERE profile_name = ?", (name,))
        # Runs: don't delete (keep history), but note they belong to a deleted profile
        return jsonify({"ok": True, "deleted": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: SCHEDULER
# ──────────────────────────────────────────────────────────────

SCHEDULER_PID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".scheduler.pid"
)


def _scheduler_pid_alive() -> int:
    """Return PID of running scheduler, or 0."""
    if not os.path.exists(SCHEDULER_PID_FILE):
        return 0
    try:
        pid = int(open(SCHEDULER_PID_FILE).read().strip())
    except Exception:
        return 0
    try:
        import psutil
        if psutil.pid_exists(pid):
            p = psutil.Process(pid)
            if "python" in (p.name() or "").lower():
                return pid
    except Exception:
        pass
    try:
        os.remove(SCHEDULER_PID_FILE)
    except OSError:
        pass
    return 0


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    if _scheduler_pid_alive():
        return jsonify({"error": "Scheduler is already running"}), 409
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "scheduler.py"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_popen_no_console_flags(),
        )
        with open(SCHEDULER_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    pid = _scheduler_pid_alive()
    if not pid:
        return jsonify({"error": "Scheduler is not running"}), 409
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=10)
        except psutil.TimeoutExpired:
            p.kill()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        os.remove(SCHEDULER_PID_FILE)
    except OSError:
        pass
    try:
        get_db().config_set("scheduler.heartbeat_at", None)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/scheduler/status", methods=["GET"])
def api_scheduler_status():
    pid = _scheduler_pid_alive()
    db = get_db()

    today = datetime.now().strftime("%Y-%m-%d")
    row = db._get_conn().execute(
        "SELECT COUNT(*) AS n FROM runs WHERE started_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    runs_today = row["n"] if row else 0

    heartbeat_at = db.config_get("scheduler.heartbeat_at")
    is_alive_heartbeat = False
    if heartbeat_at:
        try:
            age = (datetime.now() - datetime.fromisoformat(heartbeat_at)).total_seconds()
            is_alive_heartbeat = age < 120
        except Exception:
            pass

    return jsonify({
        "is_running":         bool(pid) and is_alive_heartbeat,
        "pid":                pid,
        "started_at":         db.config_get("scheduler.started_at"),
        "heartbeat_at":       heartbeat_at,
        "next_run_at":        db.config_get("scheduler.next_run_at"),
        "last_run_profile":   db.config_get("scheduler.last_run_profile"),
        "runs_today":         runs_today,
        "target_runs_per_day": db.config_get("scheduler.target_runs_per_day") or 30,
    })


# ──────────────────────────────────────────────────────────────
# API: LOGS
# ──────────────────────────────────────────────────────────────

@app.route("/api/logs/live")
def api_logs_live():
    """SSE live logs stream"""
    def generate():
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    entry = RUNNER.log_queue.get(timeout=1)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    last_heartbeat = time.time()
                except queue.Empty:
                    # Heartbeat every 15s to keep connection alive
                    if time.time() - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.time()
                    continue
        except GeneratorExit:
            # Client disconnected — normal, not an error
            return

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control":      "no-cache",
        "X-Accel-Buffering":  "no",
        "Connection":         "keep-alive",
    })


@app.route("/api/logs/history", methods=["GET"])
def api_logs_history():
    run_id = request.args.get("run_id", type=int)
    limit  = request.args.get("limit", default=200, type=int)
    return jsonify(get_db().logs_list(run_id=run_id, limit=limit))


# ──────────────────────────────────────────────────────────────
# API: DB TOOLS
# ──────────────────────────────────────────────────────────────

@app.route("/api/db/migrate", methods=["POST"])
def api_db_migrate():
    """Ручной запуск миграции из файлов"""
    try:
        get_db().migrate_from_files(verbose=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/db/info", methods=["GET"])
def api_db_info():
    db = get_db()
    conn = db._get_conn()
    info = {}
    for table in ("runs", "events", "competitors", "ip_history",
                  "fingerprints", "selfchecks", "config_kv", "logs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        info[table] = n
    info["db_path"] = db.path
    return jsonify(info)


# ──────────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# PROFILE CREATION / FINGERPRINT MANAGEMENT
# ──────────────────────────────────────────────────────────────

@app.route("/api/profile-templates", methods=["GET"])
def api_profile_templates():
    """List available device templates for the create-profile UI.

    Returns enriched metadata the dropdown can preview (CPU cores, RAM,
    GPU model short-name, screen resolution, desktop vs laptop).
    """
    try:
        from device_templates import DEVICE_TEMPLATES
        import re

        def _extract_gpu(renderer: str) -> str:
            """Pull a short model from the ANGLE renderer string.
            Example: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 ...)'
                     → 'GeForce RTX 4060'"""
            if not renderer:
                return "Unknown GPU"
            # Strip ANGLE wrapper
            m = re.search(r"ANGLE \([^,]+,\s*([^,]+)", renderer)
            inner = m.group(1).strip() if m else renderer
            # Strip vendor prefix ("NVIDIA ", "Intel(R) ", "AMD ")
            inner = re.sub(r"^(NVIDIA|Intel\(R\)|Intel|AMD)\s+", "", inner)
            # Strip common suffixes ("Direct3D11 vs_5_0 ps_5_0", "Graphics" sometimes)
            inner = re.sub(r"\s*Direct3D.*$", "", inner)
            return inner.strip() or "Unknown GPU"

        out = []
        for t in DEVICE_TEMPLATES:
            cpu    = t.get("cpu")    or {}
            gpu    = t.get("gpu")    or {}
            screen = t.get("screen") or {}
            battery = t.get("battery")
            out.append({
                "name":        t.get("name"),
                "platform":    t.get("platform"),
                "description": t.get("description") or "",
                # Enriched fields for dropdown preview
                "cpu_cores":   cpu.get("concurrency"),
                "ram_gb":      cpu.get("memory"),
                "gpu_model":   _extract_gpu(gpu.get("gl_renderer", "")),
                "gpu_vendor":  gpu.get("webgpu_vendor"),
                "screen_w":    screen.get("width"),
                "screen_h":    screen.get("height"),
                "is_laptop":   battery is not None,
                "weight":      t.get("weight", 1),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fingerprint/preview", methods=["POST"])
def api_profile_preview_fingerprint():
    """
    Generate a deterministic fingerprint for (name, template, language)
    without writing anything to disk or DB. Lets the user 'preview' what
    this profile would look like before creating it.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    template = data.get("template") or None
    language = data.get("language") or "uk-UA"
    if not name:
        return jsonify({"error": "name is required"}), 400
    if template == "auto":
        template = None
    try:
        b = DeviceTemplateBuilder(
            profile_name       = name,
            preferred_language = language,
            force_template     = template,
        )
        payload = b.generate_payload_dict()
        # Return a compact summary, not the whole 7KB JSON
        hw   = payload.get("hardware")   or {}
        gfx  = payload.get("graphics")   or {}
        scr  = payload.get("screen")     or {}
        lang = payload.get("languages")  or {}
        tz   = payload.get("timezone")   or {}
        uam  = payload.get("ua_metadata") or {}
        return jsonify({
            "ok":             True,
            "template":       payload.get("template_name"),
            "chrome_version": uam.get("full_version"),
            "platform":       hw.get("platform"),
            "user_agent":     hw.get("user_agent"),
            "cpu_cores":      hw.get("hardware_concurrency"),
            "ram_gb":         hw.get("device_memory"),
            "screen":         f"{scr.get('width')}x{scr.get('height')}",
            "pixel_ratio":    scr.get("pixel_ratio"),
            "language":       lang.get("language"),
            "languages":      lang.get("languages"),
            "timezone":       tz.get("id"),
            "gpu_vendor":     gfx.get("gl_vendor"),
            "gpu_renderer":   gfx.get("gl_renderer"),
            "fonts_count":    len(payload.get("fonts") or []),
            "plugins_count":  len(payload.get("plugins") or []),
            "webgl_exts":     len(gfx.get("webgl_extensions") or []),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles", methods=["POST"])
def api_profile_create():
    """
    Create a new profile record. Only registers in DB and creates the
    user-data-dir placeholder. Actual fingerprint generation + chrome
    launch happens on first run.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    template = data.get("template") or None
    language = data.get("language") or "uk-UA"
    enrich   = bool(data.get("enrich", True))

    if not name or not re.match(r"^[A-Za-z0-9_\-]+$", name):
        return jsonify({"error": "invalid name (letters, digits, _ and - only)"}), 400

    db = get_db()
    existing = db.profile_get(name) if hasattr(db, "profile_get") else None
    if existing:
        return jsonify({"error": f"profile '{name}' already exists"}), 409

    try:
        # Generate & store fingerprint
        if template == "auto":
            template = None
        builder = DeviceTemplateBuilder(
            profile_name       = name,
            preferred_language = language,
            force_template     = template,
        )
        payload = builder.generate_payload_dict()
        db.fingerprint_save(name, payload)

        # Persist profile metadata
        if hasattr(db, "profile_save"):
            db.profile_save(name, {
                "template_name":      payload.get("template_name"),
                "preferred_language": language,
                "enrich_on_create":   enrich,
                "status":             "ready",
            })

        # Create user data dir on disk
        import os as _os
        prof_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "profiles", name)
        _os.makedirs(prof_dir, exist_ok=True)

        return jsonify({"ok": True, "name": name,
                        "template": payload.get("template_name")})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<n>/regenerate-fingerprint", methods=["POST"])
def api_profile_regenerate_fingerprint(n):
    """
    Re-roll the fingerprint for an existing profile. Useful if current
    fingerprint is getting flagged — new seed = new UA / screen / fonts.
    Accepts optional JSON body with seed/template/language overrides.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    template  = data.get("template") or None
    language  = data.get("language") or None
    new_seed  = data.get("seed_suffix")   # optional: appended to profile name

    db = get_db()
    if hasattr(db, "profile_get") and not db.profile_get(n):
        return jsonify({"error": "profile not found"}), 404

    if not language:
        prof = db.profile_get(n) if hasattr(db, "profile_get") else {}
        language = (prof or {}).get("preferred_language") or "uk-UA"

    if template == "auto":
        template = None

    try:
        # Use a variant of the profile name to get deterministic-but-different
        # fingerprint without losing the ability to re-roll deterministically.
        seed_name = n
        if new_seed:
            seed_name = f"{n}#{new_seed}"
        else:
            # Timestamp-based seed for one-shot "just give me something new"
            import time as _t
            seed_name = f"{n}#{int(_t.time())}"

        builder = DeviceTemplateBuilder(
            profile_name       = seed_name,
            preferred_language = language,
            force_template     = template,
        )
        payload = builder.generate_payload_dict()
        # Save under the ORIGINAL profile name so the new fingerprint
        # becomes the active one.
        payload["profile_name"] = n
        db.fingerprint_save(n, payload)

        # Clear cached health state — old 13/13 no longer applies
        if hasattr(db, "reset_profile_health"):
            db.reset_profile_health(n)

        return jsonify({
            "ok":            True,
            "template":      payload.get("template_name"),
            "chrome_version": (payload.get("ua_metadata") or {}).get("full_version"),
            "seed_used":     seed_name,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/profiles/<n>/clear-history", methods=["POST"])
def api_profile_clear_history_new(n):
    """
    Clear profile history. Scope selects what to clear:
      events | runs | logs | selfchecks | all
    """
    data = request.get_json(silent=True) or {}
    scope = data.get("scope", "events")
    if scope not in ("events", "runs", "logs", "selfchecks", "all"):
        return jsonify({"error": "invalid scope"}), 400

    db = get_db()
    try:
        result = db.clear_profile_history(n, scope=scope)
        return jsonify({"ok": True, "cleared": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/runs/clear", methods=["POST"])
def api_runs_clear():
    """
    Clear all run records. Accepts optional JSON body:
      { "older_than_days": 30 }   — only delete runs older than N days
    """
    data = request.get_json(silent=True) or {}
    older = data.get("older_than_days")
    if older is not None:
        try:
            older = int(older)
        except ValueError:
            return jsonify({"error": "older_than_days must be an integer"}), 400

    db = get_db()
    try:
        count = db.clear_all_runs(older_than_days=older)
        return jsonify({"ok": True, "deleted": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# PROXY DIAGNOSTICS API
# ──────────────────────────────────────────────────────────────

def _get_proxy_url() -> str | None:
    """Fetch proxy URL from DB. Normalizes to include scheme."""
    db = get_db()
    url = (db.config_get("proxy.url")
           or db.config_get("proxy.string")
           or None)
    if url and not url.startswith("http"):
        url = "http://" + url
    return url


def _fetch_exit_info(proxy_url: str, timeout: int = 12) -> dict:
    """Single exit-IP lookup via proxy. Tries ipapi.co → ipwho.is."""
    import requests
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get("https://ipapi.co/json/", proxies=proxies,
                         timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("ip"):
            return {
                "ok":           True,
                "ip":           d.get("ip"),
                "country":      d.get("country_name"),
                "country_code": d.get("country_code"),
                "city":         d.get("city"),
                "region":       d.get("region"),
                "timezone":     d.get("timezone"),
                "org":          d.get("org"),
                "asn":          d.get("asn"),
            }
    except Exception:
        pass
    try:
        r = requests.get("https://ipwho.is/", proxies=proxies, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("success", True) and d.get("ip"):
            return {
                "ok":           True,
                "ip":           d.get("ip"),
                "country":      d.get("country"),
                "country_code": d.get("country_code"),
                "city":         d.get("city"),
                "region":       d.get("region"),
                "timezone":     (d.get("timezone") or {}).get("id"),
                "org":          (d.get("connection") or {}).get("org"),
                "asn":          (d.get("connection") or {}).get("asn"),
            }
    except Exception:
        pass
    return {"ok": False, "error": "all geo services failed"}


@app.route("/api/proxy/current-ip", methods=["GET"])
def api_proxy_current_ip():
    """Fetch current exit IP through configured proxy."""
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400
    return jsonify(_fetch_exit_info(proxy_url))


@app.route("/api/proxy/rotate", methods=["POST"])
def api_proxy_rotate():
    """
    Force a rotation. For providers that rotate per-TCP-connection, we just
    open a fresh connection (which is what fetching exit info does). For
    providers with a rotation API, we call that first.
    """
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    db = get_db()
    provider = db.config_get("proxy.rotation_provider") or "none"
    api_url  = db.config_get("proxy.rotation_api_url")
    api_key  = db.config_get("proxy.rotation_api_key")
    method   = db.config_get("proxy.rotation_method") or "GET"

    rotation_called = False
    rotation_error  = None
    rotation_http   = None
    if provider != "none" and api_url:
        import requests
        try:
            headers = _build_rotation_headers(provider, api_key)
            if method.upper() == "POST":
                r = requests.post(api_url, headers=headers, timeout=10)
            else:
                r = requests.get(api_url, headers=headers, timeout=10)
            rotation_http   = r.status_code
            rotation_called = r.ok
            if not r.ok:
                rotation_error = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return jsonify({"ok": False,
                            "error": f"rotation API call failed: {e}"}), 500
        import time as _t
        _t.sleep(2)
    else:
        rotation_error = (
            "Rotation API not configured — provider="
            f"{provider}, url={'set' if api_url else 'missing'}"
        )

    info = _fetch_exit_info(proxy_url)
    info["rotation_called"] = rotation_called
    info["rotation_error"]  = rotation_error
    info["rotation_http"]   = rotation_http
    info["provider"]        = provider
    return jsonify(info)


def _build_rotation_headers(provider: str, api_key: str) -> dict:
    """
    Provider-specific header assembly. Matches the logic in
    rotating_proxy.py so the dashboard test and the runtime rotation
    behave identically.
    """
    headers = {}
    if not api_key:
        return headers
    if provider == "brightdata":
        headers["Authorization"] = f"Bearer {api_key}"
    elif provider == "asocks":
        # asocks auth is the ?apiKey=... query parameter embedded in
        # the URL — no header required, and adding one here can confuse
        # strict API validators.
        pass
    else:
        headers["X-API-Key"] = api_key
    return headers


@app.route("/api/proxy/asocks-port-list", methods=["POST"])
def api_asocks_port_list():
    """
    Fetches the user's port list from asocks using their apiKey. The
    Dashboard calls this while the user is filling in the rotation form
    so they can pick the right portId without having to dig through
    the asocks UI.

    Per https://docs.asocks.com/en/operations/941a4fb52e76050f13a0e886b08d3b6f.html
    the endpoint is GET /v2/proxy/ports?apiKey=<key>&per_page=50.

    IMPORTANT DISTINCTION for users:
      * "Port ID" in the asocks API = internal DB id (6-8 digit integer)
      * TCP port in host:port (e.g. 16720) is NOT the Port ID
    Users confuse these all the time — this endpoint disambiguates.
    """
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "API key is empty"}), 400

    import requests
    try:
        r = requests.get(
            "https://api.asocks.com/v2/proxy/ports",
            params={"apiKey": api_key, "per_page": 50},
            timeout=10,
        )
        if not r.ok:
            return jsonify({
                "ok":    False,
                "http":  r.status_code,
                "error": f"asocks returned HTTP {r.status_code}",
                "body":  r.text[:500],
            }), 200
        data = r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Network error: {e}"}), 200

    # asocks returns {success: true, message: {...}}. Inside message, the
    # port list may live under different keys depending on the endpoint
    # version — real shape (as of 2026) is:
    #   message.proxies: [{id, name, proxy: "host:port", login, password,
    #                      countryCode, cityName, refresh_link, ...}, ...]
    # Older versions used message.data (Laravel paginator) and even older
    # ones returned message as a flat array. Handle all three.
    items = None
    if isinstance(data, dict):
        envelope = data.get("message", data)
        if isinstance(envelope, list):
            items = envelope
        elif isinstance(envelope, dict):
            # Current shape: message.proxies
            # Laravel-paginator shape: message.data
            items = (
                envelope.get("proxies")
                or envelope.get("data")
                or envelope.get("items")
                or envelope.get("ports")
                or envelope.get("results")
            )
    else:
        items = data

    if not isinstance(items, list):
        return jsonify({
            "ok":    False,
            "error": "Unexpected response shape from asocks — no port list found",
            "body":  json.dumps(data)[:800] if isinstance(data, (dict, list)) else str(data)[:800],
        }), 200

    # Normalize each port. asocks gives us `proxy: "host:port"` as one string
    # — split it for the UI. Also pass through `refresh_link` so we can skip
    # URL assembly entirely (asocks literally hands us the full rotation URL).
    ports = []
    for p in items:
        if not isinstance(p, dict):
            continue

        # Split "109.236.84.23:16720" into host + port
        host = p.get("host") or p.get("server") or p.get("ip")
        port = p.get("port") or p.get("external_port")
        proxy_str = p.get("proxy")
        if proxy_str and (not host or not port):
            try:
                h, prt = proxy_str.rsplit(":", 1)
                host = host or h
                port = port or int(prt)
            except Exception:
                pass

        # Country: prefer full name, fall back to code
        country = None
        c = p.get("country")
        if isinstance(c, dict):
            country = c.get("name") or c.get("code")
        elif isinstance(c, str):
            country = c
        if not country:
            country = p.get("countryCode") or p.get("country_code")

        ports.append({
            "id":           p.get("id") or p.get("port_id") or p.get("portId"),
            "name":         p.get("name") or p.get("title"),
            "host":         host,
            "port":         port,
            "login":        p.get("login") or p.get("username") or p.get("user"),
            "country":      country,
            "city":         p.get("cityName") or p.get("city"),
            # asocks hands us a pre-signed rotation URL — pass it through
            # so the UI can use it verbatim instead of rebuilding from scratch.
            "refresh_link": p.get("refresh_link"),
            "active":       p.get("status", 1) == 1 if "status" in p else p.get("active", True),
        })
    return jsonify({"ok": True, "ports": ports, "count": len(ports)})


@app.route("/api/proxy/test-rotation-api", methods=["POST"])
def api_proxy_test_rotation_api():
    """
    Ping the configured rotation API URL without actually caring about
    the exit IP. Returns the HTTP status + response snippet so the user
    can verify their asocks/brightdata URL is live.
    """
    db = get_db()
    provider = db.config_get("proxy.rotation_provider") or "none"
    api_url  = db.config_get("proxy.rotation_api_url")
    api_key  = db.config_get("proxy.rotation_api_key")
    method   = (db.config_get("proxy.rotation_method") or "GET").upper()

    if provider == "none" or not api_url:
        return jsonify({
            "ok": False,
            "status": "unconfigured",
            "message": (
                f"Provider is {provider!r} and URL is "
                f"{'set' if api_url else 'empty'}. "
                "Pick a provider and paste the rotation URL first."
            ),
        })

    import requests
    try:
        headers = _build_rotation_headers(provider, api_key)
        kwargs = {"headers": headers, "timeout": 10}
        r = requests.post(api_url, **kwargs) if method == "POST" \
            else requests.get(api_url, **kwargs)
        return jsonify({
            "ok":       r.ok,
            "status":   "ok" if r.ok else "error",
            "http":     r.status_code,
            "provider": provider,
            "method":   method,
            "body":     r.text[:500],
            "message": (
                f"✓ HTTP {r.status_code} — rotation API is working"
                if r.ok else
                f"✗ HTTP {r.status_code} — check URL and credentials"
            ),
        })
    except Exception as e:
        return jsonify({
            "ok":       False,
            "status":   "network-error",
            "provider": provider,
            "message":  f"✗ Network error: {e}",
        })


@app.route("/api/proxy/test-rotation", methods=["POST"])
def api_proxy_test_rotation():
    """Run N rotation tests. Returns exit IPs + country summary."""
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    payload = request.get_json(silent=True) or {}
    n = int(payload.get("count", 10))
    n = max(1, min(30, n))

    import time as _t
    results = []
    for _ in range(n):
        results.append(_fetch_exit_info(proxy_url, timeout=15))
        _t.sleep(1.5)

    countries = {}
    unique_ips = set()
    for r in results:
        if r.get("ok"):
            unique_ips.add(r.get("ip"))
            c = r.get("country") or "?"
            countries[c] = countries.get(c, 0) + 1

    return jsonify({
        "ok":         True,
        "results":    results,
        "unique_ips": len(unique_ips),
        "countries":  countries,
        "total":      n,
    })


@app.route("/api/proxy/full-diagnostics", methods=["POST"])
def api_proxy_full_diagnostics():
    """
    Full proxy health report: IP info, geo match, timezone match,
    ASN-based reputation hint.
    """
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    db = get_db()
    expected_country  = db.config_get("browser.expected_country")  or "Ukraine"
    expected_timezone = db.config_get("browser.expected_timezone") or "Europe/Kyiv"

    info = _fetch_exit_info(proxy_url)
    if not info.get("ok"):
        return jsonify({"ok": False,
                        "error": info.get("error", "IP lookup failed")}), 502

    actual_country = (info.get("country") or "").strip()
    geo_match = True
    if expected_country and actual_country:
        exp_lc = expected_country.strip().lower()
        act_lc = actual_country.lower()
        geo_match = (exp_lc in act_lc or act_lc in exp_lc)

    actual_tz = info.get("timezone")
    tz_aliases = {"Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv"}
    normalized_actual   = tz_aliases.get(actual_tz, actual_tz)
    normalized_expected = tz_aliases.get(expected_timezone, expected_timezone)
    tz_match = (normalized_actual == normalized_expected)

    org = (info.get("org") or "").lower()
    datacenter_markers = [
        "amazon", "aws", "google cloud", "microsoft", "digitalocean", "ovh",
        "hetzner", "linode", "vultr", "datacamp", "datacenter", "hosting",
    ]
    mobile_markers = ["kyivstar", "lifecell", "vodafone", "mts", "mobile"]
    ip_type = "unknown"
    if any(m in org for m in datacenter_markers):
        ip_type = "datacenter"
    elif any(m in org for m in mobile_markers):
        ip_type = "mobile"
    elif org and ("llc" in org or "ltd" in org):
        ip_type = "residential"

    risk = {"datacenter": "high", "mobile": "low",
            "residential": "low", "unknown": "medium"}.get(ip_type, "medium")

    return jsonify({
        "ok":                True,
        "ip":                info,
        "expected_country":  expected_country,
        "actual_country":    actual_country,
        "geo_match":         geo_match,
        "expected_timezone": expected_timezone,
        "actual_timezone":   actual_tz,
        "tz_match":          tz_match,
        "ip_type":           ip_type,
        "detection_risk":    risk,
    })


# ──────────────────────────────────────────────────────────────
# ACTION PIPELINES API
# ──────────────────────────────────────────────────────────────

@app.route("/api/actions/catalog", methods=["GET"])
def api_actions_catalog():
    """List all supported action types with their params for UI builder."""
    try:
        from action_runner import action_catalog, action_common_params
        return jsonify({
            "types":         action_catalog(),
            "common_params": action_common_params(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/actions/pipelines", methods=["GET"])
def api_actions_pipelines_get():
    """
    Return both pipelines used by main.py:
      - post_ad_actions          (competitor ads)
      - on_target_domain_actions (your own brand's ads, if shown)

    Defensive read: if the value came back as a JSON string (can happen
    after certain legacy import paths), parse it. If parsing fails or
    the value isn't a list, return [] instead of choking the UI with
    `No parameters` ghost-steps.
    """
    db = get_db()

    def _as_list(key):
        raw = db.config_get(key)
        if raw is None:
            return []
        # Sometimes imports stash strings — unwrap one level
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                logging.warning(
                    f"[pipelines] {key} is an unparseable string, resetting"
                )
                return []
        if not isinstance(raw, list):
            logging.warning(
                f"[pipelines] {key} is not a list (type={type(raw).__name__}), resetting"
            )
            return []
        # Filter out malformed steps: missing `type` or not a dict
        clean = []
        for i, step in enumerate(raw):
            if not isinstance(step, dict):
                logging.warning(f"[pipelines] {key}[{i}] skipped (not a dict)")
                continue
            if not step.get("type"):
                logging.warning(f"[pipelines] {key}[{i}] skipped (no type field)")
                continue

            # Auto-migrate legacy `search_all_queries` → `loop` with
            # items_from="queries". Keeps existing configs working after
            # the Scripts refactor (Apr 2026).
            if step.get("type") == "search_all_queries":
                step = {
                    "type":       "loop",
                    "enabled":    step.get("enabled", True),
                    "items_from": "queries",
                    "item_var":   "query",
                    "shuffle":    step.get("shuffle", True),
                    "steps": [
                        {"type": "search_query", "query": "{query}"},
                    ],
                }
                logging.info(
                    f"[pipelines] {key}[{i}] migrated search_all_queries → loop"
                )

            clean.append(step)
        return clean

    return jsonify({
        "main_script":              _as_list("actions.main_script"),
        "post_ad_actions":          _as_list("actions.post_ad_actions"),
        "on_target_domain_actions": _as_list("actions.on_target_domain_actions"),
    })


@app.route("/api/actions/pipelines", methods=["POST"])
def api_actions_pipelines_save():
    """
    Save one, two, or all three pipelines. Body:
      { "main_script":              [...],
        "post_ad_actions":          [...],
        "on_target_domain_actions": [...] }
    Any key is optional; omitted keys are left untouched.
    """
    data = request.get_json(silent=True) or {}
    db = get_db()
    saved = {}

    for key in ("main_script", "post_ad_actions", "on_target_domain_actions"):
        if key in data:
            pipeline = data[key]
            if not isinstance(pipeline, list):
                return jsonify({"error": f"{key} must be a list"}), 400
            # Light validation — every item must have `type`
            for i, step in enumerate(pipeline):
                if not isinstance(step, dict) or "type" not in step:
                    return jsonify({
                        "error": f"{key}[{i}] must be a dict with 'type'"
                    }), 400
            db.config_set(f"actions.{key}", pipeline)
            saved[key] = len(pipeline)

    return jsonify({"ok": True, "saved": saved})


# ──────────────────────────────────────────────────────────────
# API: CONFIGURATION EXPORT / IMPORT
# ──────────────────────────────────────────────────────────────
#
# The export bundles every *configuration* table — stuff the user has
# tweaked via the dashboard — into one JSON file. The bundle EXCLUDES
# history tables (runs, events, logs, competitors, ip_history,
# action_events, selfchecks, fingerprints) since those are per-deployment
# and usually noise when copying a setup between machines.
#
# Format:
#   {
#     "format_version": 1,
#     "exported_at":    "2026-04-22T22:15:00",
#     "app_version":    "ghost-shell-1.0",
#     "config":         { "key": value, ... },            # config_kv
#     "profiles":       [ {...}, ... ],                   # profiles metadata
#     "action_pipelines": {
#         "post_ad_actions":         [ ... ],
#         "on_target_domain_actions": [ ... ],
#     }
#   }

EXPORT_FORMAT_VERSION = 1
# Keys stored in config_kv that are machine-specific and should NOT be
# moved between installations. Proxy credentials, for instance, are
# typically different on each host.
_EXPORT_SKIP_KEYS = {
    "proxy.total_rotations",
    "proxy.last_rotation_at",
    "system.first_run_at",
}


@app.route("/api/export-config", methods=["GET"])
def api_export_config():
    """Download the full dashboard configuration as JSON."""
    db = get_db()
    try:
        from datetime import datetime

        # ── CRITICAL: read FLAT keys directly from SQLite, NOT the
        # nested dict from config_get_all(). The nested form
        # ({"proxy": {"url": ...}, ...}) round-trips incorrectly on
        # import: iterating over the top-level `proxy` key and calling
        # config_set("proxy", {...}) would write the whole object under
        # a flat "proxy" key in config_kv, breaking lookups like
        # config_get("proxy.url").
        rows = db._get_conn().execute(
            "SELECT key, value FROM config_kv"
        ).fetchall()
        flat_config = {}
        for row in rows:
            key = row["key"]
            if key in _EXPORT_SKIP_KEYS:
                continue
            try:
                flat_config[key] = json.loads(row["value"])
            except Exception:
                flat_config[key] = row["value"]

        # Pull out the three action pipelines separately for clarity
        # (they stay duplicated in `config` too — exactly what config_kv has).
        action_pipelines = {
            "main_script":              flat_config.get("actions.main_script", []),
            "post_ad_actions":          flat_config.get("actions.post_ad_actions", []),
            "on_target_domain_actions": flat_config.get("actions.on_target_domain_actions", []),
        }

        # Profiles — just the on-disk config, no per-run data
        profiles = db.profiles_list()
        # profiles_list returns dicts with heavy fields — keep only the
        # deterministic ones. Anything with "last_", "total_", "session_"
        # prefix is runtime state.
        slim_profiles = []
        for p in profiles:
            slim_profiles.append({
                k: v for k, v in p.items()
                if not (k.startswith("last_") or k.startswith("total_")
                        or k.startswith("session_") or k.startswith("recent_"))
            })

        bundle = {
            "format_version":   EXPORT_FORMAT_VERSION,
            "exported_at":      datetime.now().isoformat(timespec="seconds"),
            "app_version":      "ghost-shell-1.0",
            # config is a FLAT dict of dotted keys — one-to-one with
            # SQLite's config_kv rows. Imports read this back as-is.
            "config":           flat_config,
            "profiles":         slim_profiles,
            "action_pipelines": action_pipelines,
        }

        # Content-Disposition makes the browser download it as a file
        from flask import Response
        filename = f"ghost-shell-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            json.dumps(bundle, indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except Exception as e:
        logging.error(f"export-config failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/import-config", methods=["POST"])
def api_import_config():
    """Replace current config with an uploaded bundle.

    Accepts JSON body `{ "bundle": {...}, "mode": "merge"|"replace" }`.
      - merge   (default): union config dicts, existing keys overwritten
                           by bundle values; profiles/pipelines upserted
      - replace: dangerous — wipes existing config_kv before importing

    Handles two `config` formats:
      (a) flat: {"proxy.url": "...", "search.queries": [...]} — new format
      (b) nested: {"proxy": {"url": "..."}, ...} — produced by the buggy
          v1 exporter that round-tripped through config_get_all(). We
          flatten it on the fly so old bundles still import correctly.
    """
    try:
        data = request.get_json(force=True) or {}
        bundle = data.get("bundle") or {}
        mode = data.get("mode", "merge")

        if not isinstance(bundle, dict):
            return jsonify({"error": "bundle must be an object"}), 400
        if bundle.get("format_version") != EXPORT_FORMAT_VERSION:
            return jsonify({
                "error": f"format_version mismatch: expected {EXPORT_FORMAT_VERSION}, "
                         f"got {bundle.get('format_version')!r}. "
                         f"This bundle was made by a different Ghost Shell version."
            }), 400

        db   = get_db()
        conn = db._get_conn()
        stats = {"config_keys": 0, "profiles": 0, "pipelines": 0}

        # Remove legacy "mush" keys before any writes — these are the
        # broken top-level dicts that the old buggy exporter produced
        # (e.g. key="proxy", value='{"url": "..."}'). They shadow the
        # correct dotted keys like "proxy.url" at read time.
        conn.execute("""
            DELETE FROM config_kv
            WHERE key NOT LIKE '%.%'
              AND key NOT IN ('_schema_version', '_last_migration')
        """)

        if mode == "replace":
            # Keep skip-keys (machine-local) but wipe everything else
            conn.execute("""
                DELETE FROM config_kv
                WHERE key NOT IN (%s)
            """ % ",".join("?" * len(_EXPORT_SKIP_KEYS)),
                tuple(_EXPORT_SKIP_KEYS))

        # ── Config keys ──
        cfg_in = bundle.get("config") or {}

        # Detect legacy nested format and flatten it. Heuristic: if any
        # top-level key has NO dot AND its value is a dict, it's nested.
        is_nested = any(
            "." not in k and isinstance(v, dict)
            for k, v in cfg_in.items()
        )
        if is_nested:
            def _flatten(d: dict, prefix: str = "") -> dict:
                flat = {}
                for k, v in d.items():
                    key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        flat.update(_flatten(v, key))
                    else:
                        flat[key] = v
                return flat
            cfg_in = _flatten(cfg_in)
            logging.info(
                f"[import-config] detected legacy nested bundle — "
                f"flattened to {len(cfg_in)} dotted keys"
            )

        for key, value in cfg_in.items():
            if key in _EXPORT_SKIP_KEYS:
                continue   # never import machine-specific keys
            # Sanity — only accept dotted keys, refuse top-level junk
            # that would re-introduce the mush bug.
            if "." not in key:
                logging.warning(
                    f"[import-config] skipping non-dotted key {key!r}"
                )
                continue
            db.config_set(key, value)
            stats["config_keys"] += 1

        # ── Profiles ── (upsert)
        for p in (bundle.get("profiles") or []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            try:
                name = p["name"]
                meta = {k: v for k, v in p.items() if k != "name"}
                db.profile_save(name, meta)
                stats["profiles"] += 1
            except Exception as e:
                logging.debug(f"profile_save {p.get('name')}: {e}")

        # ── Action pipelines ──
        ap = bundle.get("action_pipelines") or {}
        for key in ("main_script", "post_ad_actions", "on_target_domain_actions"):
            if key in ap and isinstance(ap[key], list):
                db.config_set(f"actions.{key}", ap[key])
                stats["pipelines"] += 1

        conn.commit()
        return jsonify({"ok": True, "mode": mode, "imported": stats})

    except Exception as e:
        logging.error(f"import-config failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def cleanup_orphan_config_keys():
    """
    One-shot cleanup for a bug in early export/import (format_version=1):
    the buggy exporter round-tripped config through the nested dict,
    and on import it wrote values like {"proxy.url": "..."} as a single
    row with key="proxy" and value='{"url":"..."}'.

    These "mush" keys shadow the correct dotted keys (`proxy.url`,
    `search.queries`, etc.) in config_get_all()'s nested output because
    the nested builder processes them in row-order and the last write
    wins.

    This function removes any top-level (non-dotted) key in config_kv
    that's NOT an internal schema marker. Safe to run on every startup
    — a correctly-seeded DB has zero non-dotted keys.
    """
    try:
        db = get_db()
        conn = db._get_conn()
        rows = conn.execute("""
            SELECT key FROM config_kv
            WHERE key NOT LIKE '%.%'
              AND key NOT IN ('_schema_version', '_last_migration')
        """).fetchall()
        if rows:
            deleted = [r["key"] for r in rows]
            conn.execute("""
                DELETE FROM config_kv
                WHERE key NOT LIKE '%.%'
                  AND key NOT IN ('_schema_version', '_last_migration')
            """)
            conn.commit()
            logging.warning(
                f"[dashboard] cleaned up {len(deleted)} orphan config_kv keys "
                f"(probably from a broken v1 import): {deleted!r}. "
                f"If anything looks unconfigured, re-enter values in the UI."
            )
    except Exception as e:
        logging.debug(f"cleanup_orphan_config_keys failed: {e}")


if __name__ == "__main__":
    # Auto-migration from legacy files
    get_db().migrate_from_files(verbose=True)
    # Clean up stale runs left from previous crashes
    cleanup_stale_runs()
    # One-shot fix for broken v1 import/export (see function docstring)
    cleanup_orphan_config_keys()

    port = int(os.environ.get("PORT", 5000))
    url  = f"http://127.0.0.1:{port}"
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Ghost Shell Dashboard                                    ║")
    print(f"║  → {url:<55}║")
    print("║  Ctrl+C to stop                                           ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    # Auto-open the dashboard in the user's default browser.
    # Disable via env: GHOST_SHELL_NO_BROWSER=1 (useful for headless hosts).
    if os.environ.get("GHOST_SHELL_NO_BROWSER") != "1":
        import webbrowser, threading, time as _t
        def _open_after_ready():
            # Wait briefly so Flask is actually listening before we hit it
            _t.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"[warn] couldn't auto-open browser: {e}")
        threading.Thread(target=_open_after_ready, daemon=True).start()

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
