# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for FreeCAD - https://github.com/djbarrow/freecad_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Rebuild a part's bodies in several processes, with the same result.

Rebuilding faces is pure Python plus many small OpenCASCADE calls, so one
process uses one core. Each body is built whole by one worker process, so
its faces share edges exactly as in a serial build. For a big body, workers
first refine its intersection curves (most of the work there) in parallel
runs; the body's worker then starts from those points.

FreeCAD's embedded interpreter cannot host `multiprocessing`, so workers are
plain processes of FreeCAD's own python running _worker.py. They stay alive
between parts and keep each decoded transmit, so an assembly import pays
their start-up once.
"""

import atexit
import hashlib
import itertools
import os
import pickle
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future

import FreeCAD
import Part

from .build import Builder

# a body with more faces than this has its intersection curves refined in
# parallel before it is built
BIG_BODY_FACES = 150
# a part that is one body is only worth spreading if it has this many
# intersection curves to refine
MIN_CURVES = 64


class Unavailable(RuntimeError):
    pass


def default_jobs():
    return max(1, (os.cpu_count() or 2) - 1)


def worker_python():
    """FreeCAD's own python interpreter, or None."""
    env = os.environ.get("SW2FC_PYTHON")
    if env:
        return env if os.path.isfile(env) else None
    dirs = [os.path.dirname(sys.executable)]
    try:
        dirs.append(os.path.join(FreeCAD.getHomePath(), "bin"))
    except Exception:
        pass
    for d in dirs:
        for n in ("python.exe", "python3", "python"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def _freecad_lib_dir():
    f = getattr(FreeCAD, "__file__", None)
    if f:
        return os.path.dirname(os.path.abspath(f))
    return os.path.join(FreeCAD.getHomePath(), "lib")


class _Worker:
    def __init__(self, python):
        here = os.path.dirname(os.path.abspath(__file__))
        args = [python, "-I", os.path.join(here, "_worker.py"), os.path.dirname(here),
                _freecad_lib_dir()]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, creationflags=flags)
            msg = self._recv()
        except Exception as exc:
            raise Unavailable("could not start a worker process (%s)" % exc)
        if not msg or msg[0] != "ready":
            self.kill()
            raise Unavailable("worker process did not start (is %s FreeCAD's python?)" % python)

    def _recv(self):
        head = self.p.stdout.read(4)
        if len(head) < 4:
            raise Unavailable("worker process exited")
        n = struct.unpack(">I", head)[0]
        return pickle.loads(self.p.stdout.read(n))

    def call(self, req):
        data = pickle.dumps(req, protocol=pickle.HIGHEST_PROTOCOL)
        self.p.stdin.write(struct.pack(">I", len(data)) + data)
        self.p.stdin.flush()
        msg = self._recv()
        if msg[0] == "error":
            raise RuntimeError("worker failed:\n" + msg[1])
        return msg[1:]

    def close(self):
        try:
            data = pickle.dumps(("quit",))
            self.p.stdin.write(struct.pack(">I", len(data)) + data)
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:
            self.kill()

    def kill(self):
        try:
            self.p.kill()
        except Exception:
            pass


class Pool:
    """`n` worker processes, each fed by its own thread from one priority
    queue (lowest first). Workers start on first use and are kept."""

    def __init__(self, n):
        self.n = n
        self.python = worker_python()
        if not self.python:
            raise Unavailable("FreeCAD's python interpreter was not found (set SW2FC_PYTHON)")
        self._q = queue.PriorityQueue()
        self._seq = itertools.count()
        self._workers = []
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._serve, daemon=True) for _ in range(n)]
        for th in self._threads:
            th.start()

    def submit(self, req, priority=0):
        fut = Future()
        self._q.put((priority, next(self._seq), req, fut))
        return fut

    def _serve(self):
        w = None
        while True:
            _, _, req, fut = self._q.get()
            if req is None:
                if w is not None:
                    w.close()
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if w is None:
                    w = _Worker(self.python)
                    with self._lock:
                        self._workers.append(w)
                fut.set_result(w.call(req))
            except BaseException as exc:
                if w is not None:
                    w.kill()
                    w = None
                fut.set_exception(exc)

    def close(self):
        for _ in self._threads:
            self._q.put(((9,), next(self._seq), None, None))
        for th in self._threads:
            th.join(10)


_pool = None
_tmp = None


def get_pool(jobs):
    global _pool
    if _pool is not None and _pool.n != jobs:
        _pool.close()
        _pool = None
    if _pool is None:
        _pool = Pool(jobs)
    return _pool


def _shutdown():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
    if _tmp is not None:
        shutil.rmtree(_tmp, ignore_errors=True)


atexit.register(_shutdown)


def _payload_file(t):
    """The transmit's bytes in a temp file named by content, for the workers."""
    global _tmp
    if t.data is None:
        raise Unavailable("transmit has no source data")
    if _tmp is None:
        _tmp = tempfile.mkdtemp(prefix="sw2fc-")
    path = os.path.join(_tmp, hashlib.sha1(t.data).hexdigest()[:20] + ".xt")
    if not os.path.exists(path):
        with open(path + ".part", "wb") as fh:
            fh.write(t.data)
        os.replace(path + ".part", path)
    return path


def _then(pool, futs, make_req, priority):
    """Future of (reply to make_req(results of futs), results of futs); the
    request is queued as soon as the last of `futs` is done."""
    out = Future()
    left = [len(futs)]
    lock = threading.Lock()

    def finished(inner):
        if inner.exception() is not None:
            out.set_exception(inner.exception())
        else:
            out.set_result((inner.result(), [f.result() for f in futs]))

    def one_done(_):
        with lock:
            left[0] -= 1
            if left[0]:
                return
        try:
            req = make_req([f.result() for f in futs])
        except BaseException as exc:
            out.set_exception(exc)
            return
        pool.submit(req, priority).add_done_callback(finished)

    for f in futs:
        f.add_done_callback(one_done)
    return out


def _shape(brep):
    s = Part.Shape()
    s.importBrepFromString(brep, False)
    return s


def build(transmits, sew_tolerance=0.01, jobs=None):
    """Like running build_all() on each transmit, in `jobs` processes.

    Returns (items, warnings, stats) where items is a list of
    (shape, kind, appearance) in the order build_all() gives, or None when
    there is nothing to spread (one body with few intersection curves)."""
    jobs = jobs or default_jobs()
    bodies = []                    # (transmit file, binary, body index, face count, curves)
    for t in transmits:
        b = Builder(t, sew_tolerance=sew_tolerance)
        for body, shells in b.bodies():
            faces = [f for fs in shells for f in fs]
            curves = b.intersection_curves(faces) if len(faces) > BIG_BODY_FACES else []
            bodies.append((t, body.index if body is not None else 0, len(faces), curves))
    if len(bodies) < 2 and sum(len(c) for *_, c in bodies) < MIN_CURVES:
        return None
    pool = get_pool(jobs)
    bodies = [(_payload_file(t), t.binary, bidx, n, curves) for t, bidx, n, curves in bodies]
    # Big bodies: refine their curves first, in runs spread over all workers,
    # then build the body from those points as soon as its runs are done.
    # Bodies go biggest first, so the last ones to finish are short.
    futs = {}
    for k, (path, binary, bidx, nfaces, curves) in enumerate(bodies):
        if not curves:
            continue
        size = max(8, -(-len(curves) // jobs))
        runs = [pool.submit(("curves", path, binary, sew_tolerance, curves[i:i + size]),
                            (0, -nfaces))
                for i in range(0, len(curves), size)]

        def body_req(results, path=path, binary=binary, bidx=bidx):
            seed = {}
            for points, _, _ in results:
                seed.update(points)
            return ("body", path, binary, sew_tolerance, (bidx, seed))
        futs[k] = _then(pool, runs, body_req, (1, -nfaces))
    for k, (path, binary, bidx, nfaces, curves) in enumerate(bodies):
        if not curves:
            futs[k] = pool.submit(("body", path, binary, sew_tolerance, (bidx, None)),
                                  (2, -nfaces))
    warnings = []
    items, stats = [], {}
    for k in range(len(bodies)):
        reply = futs[k].result()
        if bodies[k][4]:
            # curves that did not refine were left unseeded, so the body's
            # own build warns about those it needs, as a serial build does
            reply, _ = reply
        (brep, kind, app), warns, st = reply
        warnings += warns
        for key, n in st.items():
            stats[key] = stats.get(key, 0) + n
        if brep is not None:
            items.append((_shape(brep), kind, app))
    return items, warnings, stats
