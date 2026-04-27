"""
jobs.py — In-process background job queue for the Flask dashboard.

The dashboard's Flask request workers should not run multi-second
synchronous tasks (extension solo test = 5-8s, future health-canary
visits = 10-30s, batch operations on hundreds of profiles, etc.). A
blocked Flask thread starves OTHER concurrent requests, the user sees
the dashboard freeze even though the tab they're looking at isn't the
one running the slow task.

This module runs slow work in a ThreadPoolExecutor and exposes a
poll-style status API:

    job_id = enqueue("solo_test:<ext_id>", do_test, ext_id, timeout=8)
    state  = get_status(job_id)   # → {"status": "...", "result": ...}

Status values:
  ``queued``   — submitted, awaiting an idle worker
  ``running``  — worker executing the callable
  ``done``     — finished, ``result`` populated with the return value
  ``error``    — failed, ``error`` populated with str(exception)

The frontend polls ``GET /api/jobs/<job_id>`` every ~1s while showing
a spinner; renders when status flips to ``done``/``error``.

Bounded concurrency: max 2 workers. Solo test spawns headless Chrome
which is itself heavy on CPU+GPU; running 5 simultaneously would
trash the host. 2 covers the practical case (user clicks Test on
several extensions before any finishes).

TTL: completed jobs stick around for ``_DONE_TTL_SEC`` after finish
so the frontend can poll one more time and pick up the result.
After that, garbage-collected on next enqueue / get_status.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Optional


_MAX_WORKERS  = 2          # bounded by ext-solo-test resource cost
_DONE_TTL_SEC = 5 * 60     # how long a completed job's result stays
_QUEUE_MAX    = 50         # refuse enqueue past this — protect dashboard


class _Job:
    __slots__ = ("id", "kind", "status", "submitted_at", "started_at",
                 "finished_at", "result", "error", "future")

    def __init__(self, job_id: str, kind: str, future: Future):
        self.id           = job_id
        self.kind         = kind
        self.status       = "queued"   # queued | running | done | error
        self.submitted_at = time.time()
        self.started_at   = None
        self.finished_at  = None
        self.result       = None
        self.error        = None
        self.future       = future

    def to_dict(self) -> dict:
        elapsed = None
        if self.started_at:
            base = self.finished_at or time.time()
            elapsed = round(base - self.started_at, 2)
        return {
            "id":            self.id,
            "kind":          self.kind,
            "status":        self.status,
            "submitted_at":  self.submitted_at,
            "started_at":    self.started_at,
            "finished_at":   self.finished_at,
            "elapsed":       elapsed,
            "result":        self.result,
            "error":         self.error,
        }


# Singletons — created lazily on first enqueue. Keeping module-load
# cheap so importing dashboard.server doesn't spawn worker threads
# for processes that never use the queue.
_executor: Optional[ThreadPoolExecutor] = None
_jobs: dict[str, _Job] = {}
_lock  = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS,
                    thread_name_prefix="GS-job",
                )
    return _executor


def _gc_locked():
    """Drop completed jobs older than TTL. Called under _lock."""
    cutoff = time.time() - _DONE_TTL_SEC
    stale_ids = [
        jid for jid, j in _jobs.items()
        if j.status in ("done", "error")
        and j.finished_at is not None
        and j.finished_at < cutoff
    ]
    for jid in stale_ids:
        _jobs.pop(jid, None)


def enqueue(kind: str,
            fn: Callable[..., Any],
            *args, **kwargs) -> str:
    """Submit ``fn(*args, **kwargs)`` to the worker pool. Returns the
    job id. The job's status starts as 'queued' and transitions to
    'running' when a worker picks it up, then 'done' or 'error'.

    Raises RuntimeError if the queue is full (>{_QUEUE_MAX} active
    jobs) — protects against runaway clients submitting unbounded
    work."""
    with _lock:
        # Trim before counting
        _gc_locked()
        active = sum(
            1 for j in _jobs.values()
            if j.status in ("queued", "running")
        )
        if active >= _QUEUE_MAX:
            raise RuntimeError(
                f"job queue full ({active} active, max {_QUEUE_MAX}) — "
                f"wait for some to finish"
            )

    job_id = uuid.uuid4().hex[:16]

    def runner():
        # Mark started under lock
        with _lock:
            j = _jobs.get(job_id)
            if j is None:   # was never registered or got GC'd — bail
                return
            j.status     = "running"
            j.started_at = time.time()
        # Run unlocked so other threads can poll status
        try:
            result = fn(*args, **kwargs)
            with _lock:
                j = _jobs.get(job_id)
                if j is not None:
                    j.result      = result
                    j.status      = "done"
                    j.finished_at = time.time()
        except Exception as e:
            logging.exception(f"[jobs] {kind} ({job_id}) failed")
            with _lock:
                j = _jobs.get(job_id)
                if j is not None:
                    j.error       = f"{type(e).__name__}: {e}"
                    j.status      = "error"
                    j.finished_at = time.time()

    fut = _get_executor().submit(runner)

    with _lock:
        _jobs[job_id] = _Job(job_id, kind, fut)
    return job_id


def get_status(job_id: str) -> Optional[dict]:
    """Return the job's current status dict, or None if unknown
    (never existed, or already GC'd after TTL)."""
    with _lock:
        _gc_locked()
        j = _jobs.get(job_id)
        if j is None:
            return None
        return j.to_dict()


def list_active() -> list[dict]:
    """Diagnostic: return all jobs currently queued or running.
    Used by the dashboard's debug page."""
    with _lock:
        return [
            j.to_dict() for j in _jobs.values()
            if j.status in ("queued", "running")
        ]


def cancel(job_id: str) -> bool:
    """Best-effort cancel. Only works if the job is still queued
    (worker hasn't picked it up). Running jobs cannot be interrupted
    safely from outside — they'd leave Chrome / chromedriver
    orphans. Returns True if cancellation was requested successfully."""
    with _lock:
        j = _jobs.get(job_id)
        if j is None:
            return False
        if j.status != "queued":
            return False
        ok = j.future.cancel()
        if ok:
            j.status      = "error"
            j.error       = "cancelled before start"
            j.finished_at = time.time()
        return ok
