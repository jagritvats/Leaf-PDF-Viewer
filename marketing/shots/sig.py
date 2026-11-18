"""Handwritten-looking signature strokes in Leaf's signature-pad coordinates (about 560x200, baseline y~140).
The scene script draws them into the pad with the mouse, like a person signing."""
from __future__ import annotations

import math


def _bez(p0, p1, p2, p3, n=24):
    out = []
    for k in range(n + 1):
        t = k / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _script(x0: float, base: float, letters: str, slant: float = 0.3, rise: float = -0.06, sx: float = 1.9,
            sy: float = 1.5):
    """A connected lower-case run. Letter shapes: e/l = loops (l tall), n/m = arches, u = cups, o = small oval,
    g = descender loop, t = tall stem."""
    pts = []
    x = x0
    for ch in letters:
        w = sx * {"m": 22, "n": 15, "u": 14, "e": 10, "l": 9, "o": 12, "g": 13, "t": 9, "a": 13, "r": 11}.get(ch, 12)
        n = 16
        for k in range(n + 1):
            t = k / n
            if ch in "el":
                h = 34 if ch == "l" else 15
                lx = x + w * t - h * 0.22 * math.sin(2 * math.pi * t)
                ly = base - h * (1 - math.cos(2 * math.pi * t)) / 2
            elif ch in "nm":
                humps = 2 if ch == "m" else 1
                lx = x + w * t
                ly = base - 15 * abs(math.sin(math.pi * humps * t)) ** 0.6
            elif ch == "u":
                lx = x + w * t
                ly = base - 15 * (1 - abs(math.sin(math.pi * t))) ** 0.8
            elif ch in "oa":
                lx = x + w * 0.5 + w * 0.5 * math.sin(2 * math.pi * t - math.pi / 2) * (1 if t < 0.85 else 1.6)
                ly = base - 8 + 8 * math.cos(2 * math.pi * t)
            elif ch == "g":
                lx = x + w * t - 6 * math.sin(2 * math.pi * t)
                ly = base + 34 * math.sin(math.pi * t) ** 2 * (1 if t > 0.25 else 0.3) - 6 * math.sin(2 * math.pi * t)
            elif ch == "t":
                lx = x + w * t
                ly = base - 38 * math.sin(math.pi * t) ** 3
            else:  # r and others: a small arch
                lx = x + w * t
                ly = base - 13 * math.sin(math.pi * t) ** 0.5
            pts.append((lx, ly))
        x += w
    # slant and a slight upward drift to the right
    return [(px + slant * (base - py) * 0.5, base + (py - base) * sy + rise * (px - x0)) for px, py in pts]


def _end(stroke):
    return stroke[-1]


def signature_strokes(style: str = "avery"):
    if style == "elena":
        e = _bez((40, 120), (90, 100), (96, 54), (70, 52), 16) + _bez((70, 52), (34, 56), (30, 140), (78, 140), 20)
        run1 = e + _script(78, 140, "lena")
        x = _end(run1)[0] + 16
        m = _bez((x, 136), (x + 6, 90), (x + 14, 50), (x + 16, 46), 12) +             _bez((x + 16, 46), (x + 24, 90), (x + 30, 120), (x + 36, 132), 10) +             _bez((x + 36, 132), (x + 46, 90), (x + 56, 50), (x + 60, 46), 10) +             _bez((x + 60, 46), (x + 66, 100), (x + 72, 130), (x + 78, 136), 10)
        run2 = m + _script(x + 78, 136, "arsh")
        end = _end(run2)[0]
        under = _bez((60, 172), (180, 156), (end - 120, 168), (end + 20, 126), 40)
        return [run1, run2, under]
    if style == "rowan":
        r = _bez((50, 146), (54, 100), (58, 60), (62, 44), 14) + _bez((62, 44), (130, 26), (138, 96), (72, 98), 22)             + _bez((72, 98), (96, 108), (106, 136), (120, 136), 12)
        run1 = r + _script(120, 136, "owan")
        x = _end(run1)[0] + 14
        a = _bez((x, 140), (x + 18, 100), (x + 34, 54), (x + 42, 42), 14) +             _bez((x + 42, 42), (x + 48, 70), (x + 54, 120), (x + 64, 138), 14)
        run2 = a + _script(x + 64, 136, "chebe")
        bar = _bez((x + 6, 110), (x + 28, 104), (x + 48, 102), (x + 70, 100), 12)
        end = _end(run2)[0]
        under = _bez((56, 172), (170, 160), (end - 120, 170), (end + 10, 140), 36)
        return [run1, run2, bar, under]
    # avery
    a = _bez((36, 150), (52, 110), (66, 52), (80, 40), 16) + _bez((80, 40), (88, 60), (96, 120), (106, 140), 16)
    run1 = a + _script(106, 140, "very")
    bar = _bez((52, 112), (80, 104), (110, 104), (128, 108), 12)
    x = _end(run1)[0] + 14
    l_ = _bez((x, 132), (x + 16, 80), (x + 30, 34), (x + 38, 36), 14) +         _bez((x + 38, 36), (x + 44, 52), (x + 30, 120), (x + 42, 136), 14)
    run2 = l_ + _script(x + 42, 136, "indqvist")
    end = _end(run2)[0]
    under = _bez((40, 176), (160, 164), (end - 140, 180), (end + 10, 136), 40)
    return [run1, bar, run2, under]
