# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for FreeCAD - https://github.com/djbarrow/freecad_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Worker process for parallel.py: builds bodies, or refines intersection curves.

Run by FreeCAD's own python as  python _worker.py PLUGIN_DIR FREECAD_LIB_DIR.
Reads pickled requests from stdin and writes pickled replies to stdout, each
framed by a 4-byte big-endian length. Shapes travel as BRep strings.
"""

import os
import pickle
import struct
import sys
import traceback


def _frames(out):
    def send(obj):
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        out.write(struct.pack(">I", len(data)) + data)
        out.flush()
    return send


def _read(inp):
    head = inp.read(4)
    if len(head) < 4:
        return None
    n = struct.unpack(">I", head)[0]
    return pickle.loads(inp.read(n))


def main():
    # replies go to a private copy of stdout; anything OpenCASCADE or FreeCAD
    # prints to fd 1 lands on stderr instead of corrupting the stream
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    send = _frames(out)
    for p in reversed(sys.argv[1:3]):
        if p not in sys.path:
            sys.path.insert(0, p)
    import FreeCAD  # noqa: F401
    from sw2fc import xt
    from sw2fc.build import Builder, Log

    decoded = {}         # transmit file -> (transmit, {body index: shells})
    inp = sys.stdin.buffer
    send(("ready", os.getpid()))
    while True:
        req = _read(inp)
        if req is None or req[0] == "quit":
            return
        try:
            kind, path, binary, sew_tol, arg = req
            if path not in decoded:
                if len(decoded) >= 4:
                    decoded.pop(next(iter(decoded)))
                with open(path, "rb") as fh:
                    t = xt.parse(fh.read(), binary)
                decoded[path] = (t, {(body.index if body is not None else 0): shells
                                     for body, shells in Builder(t).bodies()})
            t, shells_of = decoded[path]
            # a fresh builder per request: a body starts from empty caches
            # (plus any seeded curve points), as in a serial build
            b = Builder(t, log=Log(), sew_tolerance=sew_tol)
            if kind == "body":
                bidx, seed = arg
                if seed:
                    b.seed_intersections(seed)
                body = b.node(bidx)
                try:
                    shape, skind = b.build_body(body, shells_of.get(bidx, []))
                    reply = (shape.exportBrepToString(), skind, b.last_appearance)
                except Exception as exc:
                    b.log.warn("body %s failed: %s" % (bidx, exc))
                    reply = (None, None, None)
            else:                               # "curves": refine intersection curves
                reply = b.intersections(arg)
            send(("ok", reply, b.log.warnings, b.stats))
        except Exception:
            send(("error", traceback.format_exc()))


if __name__ == "__main__":
    main()
