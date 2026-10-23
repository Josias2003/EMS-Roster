"""
Background execution for roster generation.

The CP-SAT solve can run for minutes, far longer than a web request should be
held open (and longer than most proxies/serverless limits allow). Generation
is started as a job, the request returns immediately with a job id, and the
page polls /api/roster/jobs/{id} for the result.

State is kept in this process's memory, so this assumes a single long-running
server process (the Dockerfile's single uvicorn worker). It does NOT work on
serverless hosts (Vercel) where a function is frozen once its response is sent.
Only one solve runs at a time — the solver already uses several CPU threads,
so running two at once just makes both slower.
"""
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

from fastapi import HTTPException

_MAX_KEPT_JOBS = 50

_lock = threading.Lock()
_solve_slot = threading.Semaphore(1)
_jobs: dict = {}  # job_id -> job dict


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def active_job_for_run(run_id: int) -> Optional[dict]:
    with _lock:
        for job in _jobs.values():
            if job["run_id"] == run_id and job["status"] in ("queued", "running"):
                return _public(job)
    return None


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def start_job(run_id: int, label: str, work: Callable[[], dict]) -> dict:
    """Runs `work()` on a background thread (after any earlier solve finishes).
    `work` must open its own database session and return the result dict."""
    job = {
        "job_id": uuid.uuid4().hex, "run_id": run_id, "label": label, "status": "queued",
        "created_at": datetime.utcnow().isoformat(), "started_at": None, "finished_at": None,
        "result": None, "error": None, "error_status": None,
    }
    with _lock:
        _jobs[job["job_id"]] = job
        finished = sorted((j for j in _jobs.values() if j["status"] in ("done", "failed")),
                          key=lambda j: j["created_at"])
        for old in finished[:max(0, len(finished) - _MAX_KEPT_JOBS)]:
            _jobs.pop(old["job_id"], None)

    def runner():
        with _solve_slot:
            with _lock:
                job["status"] = "running"
                job["started_at"] = datetime.utcnow().isoformat()
            try:
                result = work()
                with _lock:
                    job["result"], job["status"] = result, "done"
            except HTTPException as exc:
                with _lock:
                    job["error"], job["error_status"], job["status"] = str(exc.detail), exc.status_code, "failed"
            except Exception as exc:  # never leave a job stuck in "running"
                with _lock:
                    job["error"], job["error_status"], job["status"] = f"{type(exc).__name__}: {exc}", 500, "failed"
            finally:
                with _lock:
                    job["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=runner, name=f"roster-job-{job['job_id'][:8]}", daemon=True).start()
    return _public(job)
