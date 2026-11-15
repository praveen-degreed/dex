"""Run `claude -p` as a subprocess, stream its events, and enforce a timeout and a stop."""
import subprocess
import threading

from . import config, progress, state


class Stopped(Exception):
    """The run was stopped on purpose (the `stop` command)."""


def run_claude(args, cwd, env, raw_path, err_path, key=None, timeout=None, stop_key=None, label="review"):
    """Stream a run. `key` updates a job's live progress; `stop_key` ((channel, thread_ts)) lets `stop` kill it.

    Returns the final `result` event. Raises Stopped, or RuntimeError on timeout, a non-zero exit or an error result.
    """
    timeout = timeout or config.TIMEOUT_S
    timed_out, was_stopped, result = threading.Event(), False, None
    with err_path.open("w") as err:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=err, text=True)
        if stop_key is not None:
            with state.lock:
                state.procs[stop_key] = proc
        timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
        timer.start()
        try:
            with raw_path.open("w") as raw:
                for line in proc.stdout:
                    raw.write(line)
                    ev = progress.track(key, line)
                    if ev and ev.get("type") == "result":
                        result = ev
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()  # avoid leaking a file descriptor per run
            if stop_key is not None:
                with state.lock:
                    state.procs.pop(stop_key, None)
                    was_stopped = stop_key in state.stopped
                    state.stopped.discard(stop_key)
    if was_stopped:
        raise Stopped()
    if timed_out.is_set():
        raise RuntimeError(f"{label} timed out after {timeout // 60} min")
    if proc.returncode != 0 or result is None or result.get("is_error"):
        tail = err_path.read_text()[-300:].strip()
        raise RuntimeError(f"claude exited {proc.returncode}" + (f": {tail}" if tail else ""))
    return result
