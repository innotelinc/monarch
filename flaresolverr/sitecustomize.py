# FlareSolverr serves /v1 with waitress, which is multi-threaded by default (it
# switched to waitress from single-threaded wsgiref for FlareSolverr#680). Every
# /v1 request starts its own headless Chromium, so four worker threads means up
# to four Chromium trees live at once — on monarch that filled the 8 GiB cgroup
# and the kernel reaped whichever Chromium it found (62 of the 89 OOM kills in
# one boot; see fleet-snapshot-2026-10-27.md on i1).
#
# Pin waitress to a single worker, so solves queue behind each other instead of
# spawning in parallel. Nothing upstream is modified: this module is loaded by
# `site` because the container runs with PYTHONPATH=/patch, and it patches the
# waitress attribute that flaresolverr.py imports at call time (`from waitress
# import serve` inside its server adapter).
#
# Accepted trade-off: a solve can now wait behind another one, so callers must
# tolerate the queue. Prowlarr's FlareSolverr indexer proxy therefore carries a
# requestTimeout sized for the queue (180 s, see docker-compose.yml), not the
# ~60 s that only ever covered one solve.
import waitress

_serve = waitress.serve


def serve(*args, **kwargs):
    kwargs["threads"] = 1
    return _serve(*args, **kwargs)


waitress.serve = serve

# --- abandoned chromedriver sessions -----------------------------------------------
# Every /v1 request starts its own chromedriver. When the browser dies mid-solve
# ("tab crashed"), selenium's Service._terminate_process() raises PermissionError from
# os.kill() — the pid it is holding is no longer this process's to signal — and because
# the raise happens before Popen.wait(), the exited chromedriver is never reaped. On
# 2026-11-16 that left 15 `chromedriver` zombies in monarch's cgroup in under an hour,
# one per FlareSolverr request, and check-zombie-processes.py warned on i2.
#
# Swallow the failed signal and always wait: a child that cannot be terminated is still
# a child that can be reaped. Image updates stay safe — this is still PYTHONPATH=/patch,
# not a patched upstream file.
from selenium.webdriver.common import service as _selenium_service

_terminate_process = _selenium_service.Service._terminate_process


def _terminate_process_and_reap(self):
    try:
        _terminate_process(self)
    except Exception:  # gone already, or the pid now belongs to someone else
        try:
            self.process.wait(timeout=10)
        except Exception:
            pass


_selenium_service.Service._terminate_process = _terminate_process_and_reap

# Selenium retries the termination and then calls process.kill(); both paths go through
# Popen.send_signal, which raises PermissionError for a pid this process no longer owns
# before Popen.wait() can run. Swallowing the failed signal lets selenium's own wait()
# reap the child, which is the only thing that clears a zombie.
import subprocess as _subprocess

_send_signal = _subprocess.Popen.send_signal


def _send_signal_if_ours(self, sig):
    try:
        _send_signal(self, sig)
    except PermissionError:
        pass


_subprocess.Popen.send_signal = _send_signal_if_ours
