# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for FreeCAD - https://github.com/djbarrow/freecad_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""File paths past Windows' 260-character limit.

SolidWorks projects nest deeply (vendor folders inside drawing folders inside
project folders), and Windows refuses longer paths unless they carry the
\\\\?\\ prefix. Use long_path() for file system calls and short_path() on
paths that come back from them; elsewhere both return the path unchanged.
"""

import os

_PREFIX = "\\\\?\\"


def long_path(p):
    """Absolute `p` with the \\\\?\\ prefix on Windows; unchanged elsewhere."""
    if os.name != "nt" or not p or p.startswith(_PREFIX):
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\"):                   # \\server\share\...
        return _PREFIX + "UNC\\" + p[2:]
    return _PREFIX + p


def short_path(p):
    """`p` without the prefix long_path() adds."""
    if p.startswith(_PREFIX + "UNC\\"):
        return "\\\\" + p[len(_PREFIX) + 4:]
    if p.startswith(_PREFIX):
        return p[len(_PREFIX):]
    return p
