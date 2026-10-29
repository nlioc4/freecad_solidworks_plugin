# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for FreeCAD - https://github.com/djbarrow/freecad_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Read the Parasolid geometry out of a SolidWorks part. Pure Python (no FreeCAD)."""

import os
import re

from . import container, partition, xt


class ConversionError(RuntimeError):
    pass


def _short(exc, limit=80):
    """Error text fit for a message box: decoder errors can quote raw bytes."""
    text = "".join(c if c.isprintable() and ord(c) < 128 else "?" for c in str(exc))
    return text if len(text) <= limit else text[:limit] + "..."


def most_recent_config(streams):
    """swID of the configuration the part was last shown in, from
    swXmlContents/Features, or None. The swID is the <n> of Config-<n>-Partition."""
    xml = container.find_stream(streams, "swXmlContents/Features")
    if not xml:
        return None
    for tag in re.findall(rb"<swConfiguration\b[^>]*>", xml):
        m = re.search(rb'\bswID="(\d+)"', tag)
        if m and b'swMostRecentConfiguration="YES"' in tag:
            return int(m.group(1))
    return None


def load_transmits(path, config=None):
    """Decode the Parasolid geometry of a SolidWorks part (or a raw .x_t/.x_b file).

    Returns (transmits, kind, skipped): usually a single transmit, the main
    partition. Parts built from imported geometry (e.g. a STEP import) can leave
    the main Config-<n>-Partition empty and keep each body in its own section of a
    FeatureBodies/LocalBodies stream; then every section of that stream that
    holds faces is returned. `skipped` lists the sections that could not be
    decoded.

    `config` is the swID of the configuration to load (an assembly component
    names one); without it, the part's most recent configuration. When that
    configuration has no stored geometry, another one is used."""
    data = open(path, "rb").read()
    ext = os.path.splitext(path)[1].lower()
    if ext in (".x_t", ".xmt_txt"):
        return [xt.parse(data, binary=False)], "x_t", []
    if ext in (".x_b", ".xmt_bin"):
        return [xt.parse(data, binary=True)], "x_b", []
    try:
        kind, streams = container.read_streams(data)
    except Exception as exc:
        raise ConversionError("not a readable SolidWorks file: %s" % exc)
    if config is None:
        config = most_recent_config(streams)
    cands = partition.candidates(streams, config)
    if not cands:
        if any(k.endswith("__Zip") for k in streams):
            raise ConversionError(
                "this file uses the pre-2011 SolidWorks storage format, which is not supported; "
                "open and re-save it in a newer SolidWorks version")
        raise ConversionError("no Parasolid geometry found in the file")
    errors = []
    found, found_in = [], None
    for name, idx, k, payload in cands:
        if found_in is not None and name != found_in:
            break
        try:
            t = xt.parse(payload, binary=True)
        except xt.XTError as exc:
            if partition.is_main_partition(name):
                # never fall back to ghost / feature bodies when the main
                # geometry is unreadable: that silently drops most of the part
                raise ConversionError("could not decode the part geometry in %s (%s)"
                                      % (name.rsplit("/", 1)[-1], _short(exc)))
            errors.append("%s #%d: %s" % (name, idx, _short(exc)))
            continue
        if not t.of_type(14):
            continue
        if "LocalBodies" not in name:
            return [t], kind, []
        # one body per section: keep collecting from this stream
        found.append(t)
        found_in = name
    if found:
        return found, kind, [e for e in errors if e.startswith(found_in + " ")]
    if errors:
        raise ConversionError("could not decode geometry: " + "; ".join(errors))
    raise ConversionError("the file contains no solid or surface geometry")


def load_transmit(path):
    """The first transmit of `load_transmits` and the container kind."""
    transmits, kind, _ = load_transmits(path)
    return transmits[0], kind


def check(path):
    """Fast pre-flight check: can this file be converted? Returns a dict."""
    try:
        transmits, kind, skipped = load_transmits(path)
    except ConversionError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # malformed input must never crash the caller
        return {"ok": False, "message": "unreadable file (%s)" % exc.__class__.__name__}
    nfaces = nbodies = 0
    for t in transmits:
        faces = t.of_type(14)
        bodies = set()
        for f in faces:
            shell = t[f["shell"]]
            if shell is not None:
                bodies.add(shell["body"])
        nfaces += len(faces)
        nbodies += max(1, len(bodies))
    out = {"ok": True, "container": kind, "faces": nfaces, "bodies": nbodies, "message": "ok"}
    if skipped:
        out["message"] = "ok (%d section(s) could not be decoded)" % len(skipped)
    return out
