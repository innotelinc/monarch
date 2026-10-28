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
