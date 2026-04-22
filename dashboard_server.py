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

    return jsonify({
        "total_profiles":    len(profiles),
        "total_searches":    total_summary.get("search_ok", 0),
        "total_captchas":    total_summary.get("captcha", 0),
        "total_blocks":      total_summary.get("blocked", 0),
        "total_competitors": total_comp,
        "unique_domains":    unique_domains,
        "daily":             db.daily_stats(days=14),
        "run_status":        get_run_status_dict(),
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
    return jsonify({
        "total_records":  total,
        "unique_domains": unique,
        "by_domain":      db.competitors_by_domain(),
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
        RUNNER.started_at     = datetime.now().isoformat(timespec="seconds")
        RUNNER.finished_at    = None
        RUNNER.last_exit_code = None
        RUNNER.last_error     = None

        RUNNER.log(f"Overпуск #{run_id} мониторинга...", "info")

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
    """List available device templates for the create-profile UI."""
    try:
        from device_templates import DEVICE_TEMPLATES
        out = []
        for t in DEVICE_TEMPLATES:
            out.append({
                "name":        t.get("name"),
                "platform":    t.get("platform"),
                "description": t.get("description") or "",
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
    if provider != "none" and api_url:
        import requests
        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            if method.upper() == "POST":
                r = requests.post(api_url, headers=headers, timeout=10)
            else:
                r = requests.get(api_url, headers=headers, timeout=10)
            rotation_called = r.ok
        except Exception as e:
            return jsonify({"ok": False,
                            "error": f"rotation API call failed: {e}"}), 500
        import time as _t
        _t.sleep(2)

    info = _fetch_exit_info(proxy_url)
    info["rotation_called"] = rotation_called
    info["provider"] = provider
    return jsonify(info)


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
    """
    db = get_db()
    return jsonify({
        "post_ad_actions":
            db.config_get("actions.post_ad_actions") or [],
        "on_target_domain_actions":
            db.config_get("actions.on_target_domain_actions") or [],
    })


@app.route("/api/actions/pipelines", methods=["POST"])
def api_actions_pipelines_save():
    """
    Save one or both pipelines. Body:
      { "post_ad_actions": [...], "on_target_domain_actions": [...] }
    Either key is optional.
    """
    data = request.get_json(silent=True) or {}
    db = get_db()
    saved = {}

    for key in ("post_ad_actions", "on_target_domain_actions"):
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
# MAIN
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Auto-migration from legacy files
    get_db().migrate_from_files(verbose=True)
    # Clean up stale runs left from previous crashes
    cleanup_stale_runs()

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
