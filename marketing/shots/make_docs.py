"""Demo documents for the store screenshots (all names, places, numbers and addresses are invented).

    python marketing/shots/make_docs.py [out_dir]     (default: marketing/shots/work/docs)

Writes:
    canopy-report.pdf        6-page research report: headings, vector figures, a table, an outline, review comments
    rental-application.pdf   2-page AcroForm (text, checkboxes, dropdowns, required fields, signature field)
    invoice.pdf              studio invoice with emails and phone numbers (for Search & redact)
    services-agreement.pdf   1-page agreement (signed digitally by the scene script)
    scanned-notice.pdf       a letter "scanned" to an image with paper tone, tilt and noise (for OCR)
Fonts are Windows system fonts, embedded in full so the Edit text tool can rewrite lines in the original font.
"""
from __future__ import annotations

import io
import math
import os
import random
import sys

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
FONT_FILES = {
    "serif": "georgia.ttf", "serif-b": "georgiab.ttf", "serif-i": "georgiai.ttf",
    "sans": "segoeui.ttf", "sans-sb": "seguisb.ttf", "sans-b": "segoeuib.ttf", "sans-l": "segoeuil.ttf",
    "mono": "consola.ttf", "type": "cour.ttf",
}
_font_cache: dict[str, pymupdf.Font] = {}


def font(key: str) -> pymupdf.Font:
    if key not in _font_cache:
        _font_cache[key] = pymupdf.Font(fontfile=os.path.join(FONTS, FONT_FILES[key]))
    return _font_cache[key]


def rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


INK = rgb("#1d2327")
MUTED = rgb("#5b6670")
RULE = rgb("#d5dbe0")
GREEN = rgb("#2f6b4f")
GREEN_L = rgb("#e7f1ec")
TEAL = rgb("#3f8f8a")
SAND = rgb("#c99a4b")
CLAY = rgb("#b5603c")
SKY = rgb("#5b86b5")


class Pen:
    """Small text-layout helper over a page: fonts are embedded once per page by name (PyMuPDF reuses the xref)."""

    def __init__(self, page: pymupdf.Page):
        self.page = page
        self.used: set[str] = set()

    def _use(self, key: str) -> str:
        name = key.replace("-", "")
        if key not in self.used:
            self.page.insert_font(fontname=name, fontfile=os.path.join(FONTS, FONT_FILES[key]))
            self.used.add(key)
        return name

    def text(self, x, y, s, key="serif", size=10.5, color=INK, align="left", width=None):
        if align != "left":
            w = font(key).text_length(s, size)
            x = x + (width - w) if align == "right" else x + (width - w) / 2
        self.page.insert_text((x, y), s, fontname=self._use(key), fontsize=size, color=color)
        return font(key).text_length(s, size)

    def wrap(self, s: str, key: str, size: float, width: float) -> list[str]:
        f = font(key)
        lines, cur = [], ""
        for word in s.split():
            t = f"{cur} {word}".strip()
            if f.text_length(t, size) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def para(self, x, y, width, s, key="serif", size=10.5, leading=1.45, color=INK, after=8.0):
        for ln in self.wrap(s, key, size, width):
            self.text(x, y, ln, key, size, color)
            y += size * leading
        return y + after

    def bullets(self, x, y, width, items, key="serif", size=10.5, leading=1.45, color=INK, after=4.0, mark="•"):
        for it in items:
            self.text(x, y, mark, key, size, GREEN)
            y = self.para(x + 14, y, width - 14, it, key, size, leading, color, after)
        return y


# ============================================================================ report
REPORT_TITLE = "Riverbend Urban Canopy Study"
W, H = 595.0, 842.0
M = 64.0  # side margin


def report_header(pen: Pen, pno: int, total: int) -> None:
    p = pen.page
    if pno > 1:  # the title page has its own banner
        pen.text(M, 40, REPORT_TITLE + "  ·  2026 Field Season Report", "sans", 8, MUTED)
        pen.text(M, 40, "Greenway Field Lab", "sans-sb", 8, GREEN, align="right", width=W - 2 * M)
        p.draw_line((M, 48), (W - M, 48), color=RULE, width=0.6)
    p.draw_line((M, H - 46), (W - M, H - 46), color=RULE, width=0.6)
    pen.text(M, H - 32, "Prepared for the Riverbend Parks Commission", "sans", 8, MUTED)
    pen.text(M, H - 32, f"{pno} / {total}", "sans", 8, MUTED, align="right", width=W - 2 * M)


def heading(pen: Pen, y: float, num: str, title: str) -> float:
    y += 10
    if num:
        pen.text(M, y, num, "sans-b", 15, GREEN)
        pen.text(M + 26, y, title, "sans-sb", 15, INK)
    else:
        pen.text(M, y, title, "sans-sb", 15, INK)
    return y + 22


def subheading(pen: Pen, y: float, title: str) -> float:
    pen.text(M, y + 4, title, "sans-sb", 11.5, GREEN)
    return y + 22


def caption(pen: Pen, x: float, y: float, w: float, s: str) -> float:
    return pen.para(x, y, w, s, "sans", 8.5, 1.4, MUTED, after=6)


def bar_chart(page: pymupdf.Page, pen: Pen, r: pymupdf.Rect, data: list[tuple[str, float, float]]) -> None:
    """Grouped bars: (label, 2021, 2026) canopy cover %."""
    page.draw_rect(r, color=None, fill=rgb("#f7f9f8"))
    plot = pymupdf.Rect(r.x0 + 40, r.y0 + 34, r.x1 - 16, r.y1 - 34)
    vmax = 30
    for v in range(0, vmax + 1, 5):
        y = plot.y1 - plot.height * v / vmax
        page.draw_line((plot.x0, y), (plot.x1, y), color=RULE, width=0.5)
        pen.text(plot.x0 - 30, y + 3, f"{v}%", "sans", 7.5, MUTED, align="right", width=24)
    n = len(data)
    slot = plot.width / n
    bw = slot * 0.3
    for i, (lab, a, b) in enumerate(data):
        cx = plot.x0 + slot * (i + 0.5)
        for j, (v, col) in enumerate(((a, rgb("#a9cbb8")), (b, GREEN))):
            x0 = cx - bw + j * bw
            page.draw_rect(pymupdf.Rect(x0 + 1, plot.y1 - plot.height * v / vmax, x0 + bw - 1, plot.y1), color=None, fill=col)
        pen.text(cx - slot / 2, plot.y1 + 13, lab, "sans", 7.5, INK, align="center", width=slot)
        pen.text(cx - slot / 2, plot.y1 - plot.height * b / vmax - 4, f"{b:.1f}", "sans-sb", 7, GREEN, align="center", width=slot + bw)
    page.draw_line((plot.x0, plot.y1), (plot.x1, plot.y1), color=MUTED, width=0.8)
    pen.text(r.x0 + 12, r.y0 + 18, "Canopy cover by district", "sans-sb", 9.5, INK)
    lx = r.x1 - 150
    for k, (lab, col) in enumerate((("2021", rgb("#a9cbb8")), ("2026", GREEN))):
        page.draw_rect(pymupdf.Rect(lx + k * 60, r.y0 + 11, lx + k * 60 + 9, r.y0 + 20), color=None, fill=col)
        pen.text(lx + k * 60 + 13, r.y0 + 19, lab, "sans", 8, INK)


def line_chart(page: pymupdf.Page, pen: Pen, r: pymupdf.Rect) -> None:
    page.draw_rect(r, color=None, fill=rgb("#f7f9f8"))
    plot = pymupdf.Rect(r.x0 + 40, r.y0 + 34, r.x1 - 20, r.y1 - 30)
    years = list(range(2016, 2027))
    series = {
        "Citywide": ([15.2, 15.4, 15.3, 15.9, 16.1, 16.3, 16.8, 17.2, 17.6, 18.0, 18.4], GREEN),
        "Riverside district": ([19.0, 19.6, 20.1, 20.4, 21.2, 21.9, 22.4, 23.3, 23.9, 24.6, 25.1], TEAL),
        "Old Mill district": ([10.8, 10.6, 10.1, 10.2, 10.0, 10.4, 10.9, 11.3, 11.8, 12.2, 12.9], CLAY),
    }
    lo, hi = 8, 28
    for v in range(lo, hi + 1, 4):
        y = plot.y1 - plot.height * (v - lo) / (hi - lo)
        page.draw_line((plot.x0, y), (plot.x1, y), color=RULE, width=0.5)
        pen.text(plot.x0 - 30, y + 3, f"{v}%", "sans", 7.5, MUTED, align="right", width=24)
    for i, yr in enumerate(years):
        if i % 2 == 0:
            x = plot.x0 + plot.width * i / (len(years) - 1)
            pen.text(x - 15, plot.y1 + 13, str(yr), "sans", 7.5, INK, align="center", width=30)
    # planting programme band
    x0 = plot.x0 + plot.width * 5 / 10
    page.draw_rect(pymupdf.Rect(x0, plot.y0, plot.x1, plot.y1), color=None, fill=GREEN_L)
    pen.text(x0 + 6, plot.y0 + 11, "Street Tree Programme", "sans", 7.5, GREEN)
    for name, (vals, col) in series.items():
        pts = [pymupdf.Point(plot.x0 + plot.width * i / (len(vals) - 1), plot.y1 - plot.height * (v - lo) / (hi - lo))
               for i, v in enumerate(vals)]
        page.draw_polyline(pts, color=col, width=1.8)
        for p in pts[::2] + [pts[-1]]:
            page.draw_circle(p, 2.1, color=col, fill=(1, 1, 1), width=1.2)
        pen.text(pts[-1].x - 34, pts[-1].y - 6, f"{vals[-1]:.1f}%", "sans-sb", 7.5, col)
    page.draw_line((plot.x0, plot.y1), (plot.x1, plot.y1), color=MUTED, width=0.8)
    pen.text(r.x0 + 12, r.y0 + 18, "Canopy cover 2016-2026", "sans-sb", 9.5, INK)
    lx = r.x0 + 170
    for k, (name, (_, col)) in enumerate(series.items()):
        page.draw_line((lx, r.y0 + 15), (lx + 14, r.y0 + 15), color=col, width=2)
        lx += pen.text(lx + 18, r.y0 + 18, name, "sans", 8, INK) + 30


def district_map(page: pymupdf.Page, pen: Pen, r: pymupdf.Rect) -> None:
    """A stylised vector map: districts tinted by canopy cover, a river, survey plots."""
    page.draw_rect(r, color=None, fill=rgb("#eef2ef"))
    rnd = random.Random(7)
    cols, rows = 5, 3
    cw, ch = r.width / cols, r.height / rows
    shades = ["#cfe3d6", "#a9cbb8", "#7fb196", "#579777", "#2f6b4f"]
    jitter = {}
    for i in range(cols + 1):
        for j in range(rows + 1):
            dx = 0 if i in (0, cols) else rnd.uniform(-cw * 0.18, cw * 0.18)
            dy = 0 if j in (0, rows) else rnd.uniform(-ch * 0.18, ch * 0.18)
            jitter[i, j] = pymupdf.Point(r.x0 + i * cw + dx, r.y0 + j * ch + dy)
    names = ["Old Mill", "Fairgrounds", "Linden Hill", "Northgate", "Quarry Park",
             "Station", "Riverside", "Market", "Elm Row", "Westfield",
             "Brickworks", "Harbour", "Orchard", "Kestrel Way", "Sandpit"]
    k = 0
    for j in range(rows):
        for i in range(cols):
            poly = [jitter[i, j], jitter[i + 1, j], jitter[i + 1, j + 1], jitter[i, j + 1], jitter[i, j]]
            shade = shades[(i * 2 + j * 3 + (1 if names[k] == "Riverside" else 0)) % 5]
            if names[k] == "Old Mill":
                shade = shades[0]
            if names[k] == "Riverside":
                shade = shades[4]
            page.draw_polyline(poly, color=(1, 1, 1), fill=rgb(shade), width=1.4, closePath=True)
            c = pymupdf.Point(sum(p.x for p in poly[:4]) / 4, sum(p.y for p in poly[:4]) / 4)
            pen.text(c.x - 40, c.y + 3, names[k], "sans-sb", 7, (1, 1, 1) if shades.index(shade) >= 3 else INK,
                     align="center", width=80)
            k += 1
    # river
    pts = [pymupdf.Point(r.x0 + r.width * t, r.y0 + r.height * (0.55 + 0.18 * math.sin(t * 5.2 + 0.4))) for t in
           [x / 40 for x in range(41)]]
    page.draw_polyline(pts, color=rgb("#7fa7cf"), width=7)
    page.draw_polyline(pts, color=rgb("#a7c4e2"), width=4)
    # survey plots
    for _ in range(46):
        p = pymupdf.Point(rnd.uniform(r.x0 + 8, r.x1 - 8), rnd.uniform(r.y0 + 8, r.y1 - 8))
        page.draw_circle(p, 1.9, color=(1, 1, 1), fill=rgb("#c99a4b"), width=0.6)
    # legend
    lg = pymupdf.Rect(r.x1 - 118, r.y1 - 40, r.x1 - 8, r.y1 - 8)
    page.draw_rect(lg, color=None, fill=(1, 1, 1), fill_opacity=0.9)
    for n, s in enumerate(shades):
        page.draw_rect(pymupdf.Rect(lg.x0 + 8 + n * 19, lg.y0 + 8, lg.x0 + 25 + n * 19, lg.y0 + 15), color=None, fill=rgb(s))
    pen.text(lg.x0 + 8, lg.y0 + 26, "8%", "sans", 6.5, MUTED)
    pen.text(lg.x0 + 80, lg.y0 + 26, "26% cover", "sans", 6.5, MUTED)


def stat_box(page, pen, r: pymupdf.Rect, big: str, label: str, col) -> None:
    page.draw_rect(r, color=None, fill=GREEN_L, radius=0.08)
    pen.text(r.x0 + 12, r.y0 + 30, big, "sans-sb", 20, col)
    y = r.y0 + 46
    for ln in pen.wrap(label, "sans", 8.5, r.width - 24):
        pen.text(r.x0 + 12, y, ln, "sans", 8.5, MUTED)
        y += 11.5


def table(page, pen, x, y, widths, rows, head_fill=GREEN):
    rh = 19
    total = sum(widths)
    page.draw_rect(pymupdf.Rect(x, y, x + total, y + rh), color=None, fill=head_fill)
    for i, row in enumerate(rows):
        cx = x
        if i > 0 and i % 2 == 0:
            page.draw_rect(pymupdf.Rect(x, y + i * rh, x + total, y + (i + 1) * rh), color=None, fill=rgb("#f3f6f4"))
        for j, cell in enumerate(row):
            key = "sans-sb" if i == 0 else "sans"
            col = (1, 1, 1) if i == 0 else INK
            align = "left" if j == 0 else "right"
            pen.text(cx + 8, y + i * rh + 13, cell, key, 8.5, col, align=align, width=widths[j] - 16)
            cx += widths[j]
    page.draw_line((x, y + len(rows) * rh), (x + total, y + len(rows) * rh), color=MUTED, width=0.6)
    return y + len(rows) * rh


def make_report(path: str) -> None:
    doc = pymupdf.open()
    total = 6
    tw = W - 2 * M

    # ---- page 1: title + executive summary
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    pg.draw_rect(pymupdf.Rect(0, 0, W, 196), color=None, fill=GREEN)
    for i in range(9):  # soft leaf motif
        cx, cy, rr = W - 70 + 18 * math.cos(i), 70 + 40 * math.sin(i * 1.7), 36 + 6 * i
        pg.draw_circle((cx, cy), rr, color=rgb("#3d7d5f"), width=0.8)
    pen.text(M, 66, "GREENWAY FIELD LAB  ·  RESEARCH REPORT", "sans-sb", 8.5, rgb("#bfe0cd"))
    pen.text(M, 110, "Riverbend Urban Canopy Study", "serif-b", 28, (1, 1, 1))
    pen.text(M, 140, "2026 Field Season Report", "serif", 16, rgb("#e2f1e8"))
    pen.text(M, 174, "Mara Ellison, Tomas Reyes and Ana Kowal  ·  November 2026", "sans", 9.5, rgb("#d4eadc"))
    y = heading(pen, 232, "", "Executive summary")
    y = pen.para(M, y, tw,
                 "Between April and September 2026 our survey teams measured 41,820 street and park trees across the "
                 "fifteen districts of Riverbend. Combined with aerial imagery, the survey puts citywide canopy cover "
                 "at 18.4 percent, up 2.1 points since the 2021 baseline and the largest five-year gain on record.")
    y = pen.para(M, y, tw,
                 "Growth was uneven. Riverside and Linden Hill now exceed 24 percent cover, while Old Mill and Brickworks "
                 "remain below 13 percent and lost mature trees to the 2024 summer drought. Street trees planted since "
                 "2021 have a survival rate of 87 percent where watering bags were used, against 64 percent without them.")
    y = pen.para(M, y, tw,
                 "We recommend focusing the next planting round on the four districts below 13 percent cover, extending "
                 "the watering programme to every new planting, and repeating this survey in 2028 with the same plots.")
    y += 6
    bw = (tw - 24) / 3
    for k, (big, lab, col) in enumerate((("41,820", "trees surveyed in 2026 across 312 plots", GREEN),
                                         ("18.4%", "citywide canopy cover, up from 16.3% in 2021", TEAL),
                                         ("87%", "survival of new street trees with watering bags", CLAY))):
        stat_box(pg, pen, pymupdf.Rect(M + k * (bw + 12), y, M + k * (bw + 12) + bw, y + 78), big, lab, col)
    y += 104
    y = subheading(pen, y, "Key findings")
    y = pen.bullets(M, y, tw, [
        "Canopy cover rose in eleven of fifteen districts; the citywide gain came mostly from trees planted in 2019-2021.",
        "Lindens, plane trees and field maples make up 46 percent of the street population, above our 30 percent "
        "diversity target for any three species.",
        "Heat mapping links each 10 points of canopy cover to afternoon pavement temperatures 2.4 °C lower.",
    ])
    report_header(pen, 1, total)

    # ---- page 2: introduction, methods, map
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    report_header(pen, 2, total)
    y = heading(pen, 80, "1", "Introduction")
    y = pen.para(M, y, tw,
                 "Riverbend adopted its Urban Forest Plan in 2019 with a goal of 22 percent canopy cover by 2035. The plan "
                 "asks for a full field survey every five years so that planting budgets follow measured need rather than "
                 "requests. This report covers the second survey and compares it with the 2021 baseline.")
    y = pen.para(M, y, tw,
                 "Trees shade streets, slow storm runoff and make walking routes more pleasant, but those benefits are "
                 "not shared evenly. A second aim of the study was therefore to show where cover is lowest and where new "
                 "planting would reach the most residents.")
    y = heading(pen, y + 4, "2", "Methods")
    y = pen.para(M, y, tw,
                 "We revisited the 312 permanent plots set out in 2021, each a 0.1 hectare circle. Volunteers recorded "
                 "species, trunk diameter, crown width and condition for every tree taller than two metres. Canopy cover "
                 "was then estimated for each district from 15 cm aerial imagery, checked against the plot data.")
    district_map(pg, pen, pymupdf.Rect(M, y + 4, W - M, y + 214))
    y = caption(pen, M, y + 230, tw, "Figure 1. Survey districts shaded by 2026 canopy cover. Dots mark the 312 permanent "
                                    "plots; the river corridor runs east to west through Riverside and Harbour.")
    y = subheading(pen, y + 2, "Data quality")
    y = pen.para(M, y, tw,
                 "Two teams independently re-measured 10 percent of plots. Species agreed in 98 percent of cases and trunk "
                 "diameters within 2 cm in 95 percent, which is within the tolerance set in the survey protocol.")

    # ---- page 3: results + bar chart
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    report_header(pen, 3, total)
    y = heading(pen, 80, "3", "Results")
    y = subheading(pen, y, "3.1 Canopy cover by district")
    y = pen.para(M, y, tw,
                 "Cover increased in eleven districts. The largest gains were in Riverside (+3.6 points) and Station "
                 "(+3.1), both of which received more than 900 new street trees in 2019-2021. Old Mill gained only 0.5 "
                 "points after losing 140 mature trees during the 2024 drought.")
    data = [("Old Mill", 12.4, 12.9), ("Brickworks", 11.9, 12.6), ("Market", 14.2, 15.8), ("Station", 15.0, 18.1),
            ("Elm Row", 17.3, 19.0), ("Linden Hill", 22.6, 24.3), ("Riverside", 21.5, 25.1)]
    bar_chart(pg, pen, pymupdf.Rect(M, y + 4, W - M, y + 234), data)
    y = caption(pen, M, y + 250, tw, "Figure 2. Canopy cover in 2021 and 2026 for the seven districts with the most "
                                    "survey plots. Values above the bars are 2026 cover.")
    y = subheading(pen, y + 2, "3.2 Survival of new plantings")
    y = pen.para(M, y, tw,
                 "Of the 3,410 street trees planted between 2021 and 2025, 2,690 were alive and in good condition in "
                 "2026. Survival was 87 percent where watering bags were fitted for the first two summers and 64 percent "
                 "where they were not. Losses were concentrated in narrow verges next to parking.")
    y = pen.para(M, y, tw,
                 "Trees planted in autumn outperformed spring plantings by 9 points, a gap that held across species "
                 "and districts.")

    # ---- page 4: trend + species table
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    report_header(pen, 4, total)
    y = subheading(pen, 74, "3.3 Ten-year trend")
    y = pen.para(M, y, tw,
                 "Combining the two field surveys with annual aerial estimates gives a consistent ten-year series. "
                 "Growth accelerated after the Street Tree Programme began in 2021.")
    line_chart(pg, pen, pymupdf.Rect(M, y + 4, W - M, y + 214))
    y = caption(pen, M, y + 230, tw, "Figure 3. Canopy cover from aerial imagery, 2016-2026. The shaded band marks the "
                                    "years of the Street Tree Programme.")
    y = subheading(pen, y + 2, "3.4 Species composition")
    y = pen.para(M, y, tw, "The ten most common street trees account for 71 percent of the population.")
    rows = [("Species", "Trees", "Share", "Median DBH"), ("Small-leaved lime", "7,940", "19.0%", "31 cm"),
            ("London plane", "6,120", "14.6%", "44 cm"), ("Field maple", "5,180", "12.4%", "22 cm"),
            ("Silver birch", "3,060", "7.3%", "19 cm"), ("Rowan", "2,710", "6.5%", "16 cm"),
            ("Hornbeam", "2,180", "5.2%", "18 cm"), ("Cherry (ornamental)", "2,050", "4.9%", "21 cm")]
    y = table(pg, pen, M, y, [tw * 0.4, tw * 0.2, tw * 0.2, tw * 0.2], rows)
    y = caption(pen, M, y + 14, tw, "Table 1. Most common street tree species, 2026 survey. DBH: trunk diameter at "
                                   "breast height.")

    # ---- page 5: discussion + recommendations
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    report_header(pen, 5, total)
    y = heading(pen, 80, "4", "Discussion")
    y = pen.para(M, y, tw,
                 "The citywide trend is encouraging: at the current rate Riverbend would reach 22 percent cover around "
                 "2033, two years ahead of plan. The average hides a widening gap, however. Districts that were already "
                 "green gained the most, partly because wide verges and parks leave more room to plant.")
    y = pen.para(M, y, tw,
                 "The drought losses in Old Mill and Brickworks show that mature trees on compacted ground are "
                 "vulnerable. Structural soil cells and larger tree pits cost more at planting but pay back through "
                 "survival, and they are now standard in neighbouring towns.")
    y = heading(pen, y + 4, "5", "Recommendations")
    y = pen.bullets(M, y, tw, [
        "Direct at least 60 percent of the 2027-2028 planting budget to Old Mill, Brickworks, Market and Westfield.",
        "Fit watering bags to every new street tree for its first two summers and log refills in the asset system.",
        "Plant in autumn where possible; reserve spring planting for sites with irrigation.",
        "Limit any single species to 10 percent of new planting to improve resilience to pests and heat.",
        "Repeat the field survey in 2028 using the same 312 plots and protocol.",
    ])
    y += 8
    pg.draw_rect(pymupdf.Rect(M, y, W - M, y + 92), color=None, fill=GREEN_L)
    pg.draw_rect(pymupdf.Rect(M, y, M + 4, y + 92), color=None, fill=GREEN)
    pen.text(M + 18, y + 24, "Estimated cost", "sans-sb", 10.5, GREEN)
    pen.para(M + 18, y + 42, tw - 36,
             "The recommended programme would plant about 2,400 trees over two years at an estimated cost of "
             "1.9 million, including soil cells at 300 of the most constrained sites.", "serif", 10, after=0)

    # ---- page 6: appendix
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    report_header(pen, 6, total)
    y = heading(pen, 80, "A", "Appendix: survey protocol")
    y = pen.bullets(M, y, tw, [
        "Plot centres located with GNSS receivers to within 1 m and marked with buried survey nails.",
        "Every tree over 2 m tall measured for species, DBH, crown width in two directions and condition class.",
        "Condition classes: good, fair, poor, dead; the class definitions follow the 2021 protocol unchanged.",
        "Aerial imagery classified into tree, grass, hard surface and water, then checked against 1,200 random points.",
    ])
    y = heading(pen, y + 6, "", "Acknowledgements")
    y = pen.para(M, y, tw,
                 "We thank the 140 volunteers who gave 3,900 hours to the field survey, and the Riverbend Parks "
                 "Commission for access to planting records.")

    doc.set_toc([[1, "Executive summary", 1], [2, "Key findings", 1], [1, "1  Introduction", 2],
                 [1, "2  Methods", 2], [2, "Data quality", 2], [1, "3  Results", 3],
                 [2, "3.1  Canopy cover by district", 3], [2, "3.2  Survival of new plantings", 3],
                 [2, "3.3  Ten-year trend", 4], [2, "3.4  Species composition", 4], [1, "4  Discussion", 5],
                 [1, "5  Recommendations", 5], [1, "Appendix: survey protocol", 6]])
    doc.set_metadata({"title": "Riverbend Urban Canopy Study: 2026 Field Season Report",
                      "author": "Greenway Field Lab", "subject": "Urban tree canopy survey"})
    add_review_comments(doc)
    _save(doc, path)


def add_review_comments(doc: pymupdf.Document) -> None:
    """Review comments by colleagues (threaded replies), as another PDF app would have left them."""
    p = doc[0]

    def note(page, at, text, author, icon="Note", color=(1.0, 0.82, 0.25)):
        a = page.add_text_annot(at, text, icon=icon)
        a.set_info(title=author, content=text)
        a.set_colors(stroke=color)
        a.update()
        return a

    def reply(page, parent, text, author):
        a = page.add_text_annot(parent.rect.tl, text, icon="Comment")
        a.set_info(title=author, content=text)
        a.update()
        doc = page.parent
        doc.xref_set_key(a.xref, "IRT", f"{parent.xref} 0 R")
        doc.xref_set_key(a.xref, "RT", "/R")
        # like replies written by other PDF apps: no appearance of their own (viewers don't draw replies)
        doc.xref_set_key(a.xref, "Rect", "[-40 -40 -40 -40]")
        doc.xref_set_key(a.xref, "AP", "null")
        return a

    hits = p.search_for("87 percent where watering bags were used")
    if hits:
        h = p.add_highlight_annot(hits)
        h.set_info(title="Tomas Reyes", content="Can we cite the 2025 pilot here as well?")
        h.set_colors(stroke=(0.55, 0.85, 1.0))
        h.update()
        reply(p, h, "Added to the appendix draft; will link it in the next version.", "Mara Ellison")
    n = note(p, (W - 44, 300), "Should the summary mention the drought losses in numbers (140 trees)?", "Ana Kowal")
    reply(p, n, "Yes, good idea. I'll add it to the second paragraph.", "Mara Ellison")
    reply(p, n, "Agreed. Keep it to one sentence.", "Tomas Reyes")
    p3 = doc[2]
    hits = p3.search_for("Old Mill gained only 0.5")
    if hits:
        u = p3.add_underline_annot(hits)
        u.set_info(title="Ana Kowal", content="Double-check this against the plot data.")
        u.update()
    note(doc[4], (W - 44, 120), "Cost estimate confirmed with the parks budget team.", "Tomas Reyes", icon="Note",
         color=(0.5, 0.85, 0.55))


# ============================================================================ rental application
def field(page, name, rect, kind="text", value=None, label=None, required=False, choices=None, fontsize=10):
    w = pymupdf.Widget()
    w.field_name = name
    w.field_label = label or name
    w.rect = pymupdf.Rect(rect)
    w.text_font = "Helv"
    w.text_fontsize = fontsize
    w.text_color = INK
    w.border_color = None
    w.fill_color = None
    if kind == "text":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_value = value or ""
    elif kind == "check":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        w.field_value = False
        w.border_color = MUTED
        w.border_width = 0.8
    elif kind == "combo":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
        w.choice_values = choices
        w.field_value = value or ""
    elif kind == "sig":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    if required:
        w.field_flags |= pymupdf.PDF_FIELD_IS_REQUIRED
    page.add_widget(w)


def form_header(pen: Pen, title: str, pno: int):
    p = pen.page
    p.draw_rect(pymupdf.Rect(0, 0, W, 92), color=None, fill=rgb("#1f3b57"))
    # logo: a little house with a wave
    lx, ly = M, 30
    p.draw_polyline([(lx, ly + 16), (lx + 14, ly + 3), (lx + 28, ly + 16)], color=(1, 1, 1), width=2)
    p.draw_rect(pymupdf.Rect(lx + 5, ly + 16, lx + 23, ly + 32), color=(1, 1, 1), width=2)
    p.draw_bezier((lx - 2, ly + 38), (lx + 8, ly + 33), (lx + 18, ly + 43), (lx + 30, ly + 37), color=rgb("#8fc3e8"), width=2)
    pen.text(lx + 42, 50, "Harbor Lane Apartments", "sans-sb", 17, (1, 1, 1))
    pen.text(lx + 42, 66, "Leasing Office  ·  leasing@harborlane.example  ·  (555) 010-2468", "sans", 8.5, rgb("#c4d6e8"))
    pen.text(M, 50, title, "sans-sb", 12, (1, 1, 1), align="right", width=W - 2 * M)
    pen.text(M, 66, f"Page {pno} of 2", "sans", 8.5, rgb("#c4d6e8"), align="right", width=W - 2 * M)


def section(pen: Pen, y: float, title: str) -> float:
    pen.page.draw_rect(pymupdf.Rect(M, y, W - M, y + 22), color=None, fill=rgb("#e8eef4"))
    pen.text(M + 10, y + 15, title.upper(), "sans-b", 8.5, rgb("#1f3b57"))
    return y + 34


def labeled(pen: Pen, x, y, w, label, name, required=False, kind="text", choices=None, h=20):
    pen.text(x, y, label + (" *" if required else ""), "sans", 7.5, MUTED)
    r = pymupdf.Rect(x, y + 4, x + w, y + 4 + h)
    pen.page.draw_line(r.bl, r.br, color=rgb("#9aa7b3"), width=0.7)
    field(pen.page, name, r, kind, label=label, required=required, choices=choices)
    return y + h + 20


def checkbox(pen: Pen, x, y, label, name):
    field(pen.page, name, (x, y - 9, x + 11, y + 2), "check", label=label)
    pen.text(x + 17, y, label, "sans", 9, INK)


def make_rental(path: str) -> None:
    doc = pymupdf.open()
    tw = W - 2 * M
    col = (tw - 20) / 2

    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    form_header(pen, "Rental Application", 1)
    y = 124
    pen.para(M, y, tw, "Please complete every field marked with an asterisk. One application per adult occupant. "
                       "Applications are reviewed within three business days.", "sans", 9, 1.4, MUTED)
    y = section(pen, 160, "Applicant")
    y2 = labeled(pen, M, y, col, "Full legal name", "applicant_name", True)
    labeled(pen, M + col + 20, y, col, "Date of birth", "applicant_dob", True)
    y = y2
    y2 = labeled(pen, M, y, col, "Email", "applicant_email", True)
    labeled(pen, M + col + 20, y, col, "Phone", "applicant_phone", True)
    y = y2
    y = labeled(pen, M, y, tw, "Current address", "current_address", True)
    y2 = labeled(pen, M, y, col, "Current landlord", "current_landlord")
    labeled(pen, M + col + 20, y, col, "Years at current address", "years_at_address")
    y = section(pen, y2 + 4, "Desired unit")
    c3 = (tw - 40) / 3
    labeled(pen, M, y, c3, "Unit type", "unit_type", True, "combo",
            ["Studio", "One bedroom", "Two bedroom", "Two bedroom with balcony", "Three bedroom"])
    labeled(pen, M + c3 + 20, y, c3, "Lease term", "lease_term", True, "combo",
            ["6 months", "12 months", "18 months", "24 months"])
    y = labeled(pen, M + 2 * (c3 + 20), y, c3, "Move-in date", "move_in", True)
    y2 = labeled(pen, M, y, col, "Number of occupants", "occupants", True)
    labeled(pen, M + col + 20, y, col, "Preferred floor", "floor", False, "combo",
            ["No preference", "Ground floor", "Upper floor", "Top floor"])
    y = section(pen, y2 + 4, "Household")
    opts = [("Cat", "pet_cat"), ("Dog", "pet_dog"), ("No pets", "pet_none"), ("Parking space", "parking"),
            ("Bike storage", "bike_storage"), ("Storage unit", "storage"), ("Smoker in household", "smoker"),
            ("Need step-free access", "step_free"), ("Receive updates by email", "updates")]
    for i, (lab, name) in enumerate(opts):
        checkbox(pen, M + (i % 3) * (tw / 3), y + (i // 3) * 24, lab, name)
    y += 3 * 24 + 6
    pen.text(M, y, "Notes for the leasing team", "sans", 7.5, MUTED)
    r = pymupdf.Rect(M, y + 6, W - M, y + 66)
    pg.draw_rect(r, color=rgb("#9aa7b3"), width=0.7)
    w = pymupdf.Widget()
    w.field_name, w.field_label = "notes", "Notes for the leasing team"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_flags = pymupdf.PDF_TX_FIELD_IS_MULTILINE
    w.rect, w.text_font, w.text_fontsize, w.text_color = r, "Helv", 9.5, INK
    pg.add_widget(w)
    pen.text(M, H - 36, "Harbor Lane Apartments is a fictional property used for demonstration purposes.", "sans", 7, MUTED)

    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    form_header(pen, "Rental Application", 2)
    y = section(pen, 120, "Employment and income")
    y2 = labeled(pen, M, y, col, "Employer", "employer", True)
    labeled(pen, M + col + 20, y, col, "Position", "position")
    y = y2
    y2 = labeled(pen, M, y, col, "Monthly income (before tax)", "income", True)
    labeled(pen, M + col + 20, y, col, "Employed since", "employed_since")
    y = section(pen, y2 + 4, "Emergency contact")
    y2 = labeled(pen, M, y, col, "Name", "emergency_name", True)
    labeled(pen, M + col + 20, y, col, "Relationship", "emergency_relationship")
    y = y2
    y2 = labeled(pen, M, y, col, "Phone", "emergency_phone", True)
    labeled(pen, M + col + 20, y, col, "Email", "emergency_email")
    y = section(pen, y2 + 4, "Authorization")
    y = pen.para(M, y, tw,
                 "I confirm that the information in this application is true and complete. I authorize Harbor Lane "
                 "Apartments to verify my employment, income and rental history and to contact the references listed. "
                 "I understand that a holding deposit is refundable if the application is declined.",
                 "sans", 9, 1.5, INK, after=14)
    checkbox(pen, M, y, "I agree to the terms above", "agree_terms")
    checkbox(pen, M + tw / 2, y, "I consent to a credit check", "agree_credit")
    y += 30
    pen.text(M, y, "Applicant signature *", "sans", 7.5, MUTED)
    sr = pymupdf.Rect(M, y + 6, M + col + 40, y + 62)
    pg.draw_line(sr.bl, sr.br, color=rgb("#9aa7b3"), width=0.7)
    field(pg, "applicant_signature", sr, "sig", label="Applicant signature", required=True)
    pen.text(M + col + 60, y, "Date *", "sans", 7.5, MUTED)
    dr = pymupdf.Rect(M + col + 60, y + 38, W - M, y + 62)
    pg.draw_line(dr.bl, dr.br, color=rgb("#9aa7b3"), width=0.7)
    field(pg, "signature_date", dr, label="Date", required=True)
    y += 92
    pg.draw_rect(pymupdf.Rect(M, y, W - M, y + 74), color=None, fill=rgb("#f4f6f8"))
    pen.text(M + 12, y + 18, "FOR OFFICE USE", "sans-b", 7.5, MUTED)
    for i, lab in enumerate(("Received by", "Date received", "Unit offered")):
        x = M + 12 + i * (tw - 24) / 3
        pen.text(x, y + 40, lab, "sans", 7.5, MUTED)
        pg.draw_line((x, y + 62), (x + (tw - 24) / 3 - 16, y + 62), color=rgb("#c3ccd5"), width=0.6)
    pen.text(M, H - 36, "Harbor Lane Apartments is a fictional property used for demonstration purposes.", "sans", 7, MUTED)
    doc.set_metadata({"title": "Harbor Lane Apartments: Rental Application", "author": "Harbor Lane Apartments"})
    _save(doc, path)


# ============================================================================ invoice
def make_invoice(path: str) -> None:
    doc = pymupdf.open()
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    tw = W - 2 * M
    accent = rgb("#8a4f2a")
    # logo: maple leaf-ish mark
    cx, cy = M + 16, 72
    for k in range(5):
        a = math.radians(-90 + (k - 2) * 38)
        pg.draw_line((cx, cy + 8), (cx + 17 * math.cos(a), cy + 8 + 17 * math.sin(a)), color=accent, width=3.2)
    pg.draw_line((cx, cy + 8), (cx, cy + 20), color=accent, width=2)
    pen.text(M + 42, 74, "Maple & Fern Studio", "serif-b", 19, INK)
    pen.text(M + 42, 90, "Illustration · Signage · Wayfinding", "sans", 8.5, MUTED)
    pen.text(M, 70, "INVOICE", "sans-l", 26, accent, align="right", width=tw)
    pen.text(M, 90, "No. MF-2026-118", "sans-sb", 9.5, INK, align="right", width=tw)
    pg.draw_line((M, 112), (W - M, 112), color=RULE, width=0.8)
    y = 138
    blocks = [
        ("From", ["Maple & Fern Studio", "Unit 4, The Ropeworks", "Port Alder", "studio@mapleandfern.example",
                  "+1 (555) 014-3380"]),
        ("Bill to", ["Harbor Lane Apartments", "Attn: Elena Marsh, Property Manager", "200 Lantern Quay, Port Alder",
                     "e.marsh@harborlane.example", "(555) 010-2468"]),
        ("Details", ["Issued: 4 November 2026", "Due: 4 December 2026", "Project: Lobby wayfinding",
                     "PO ref: HL-PO-0932", ""]),
    ]
    cw = tw / 3
    for i, (head, lines) in enumerate(blocks):
        x = M + i * cw
        pen.text(x, y, head.upper(), "sans-b", 7.5, accent)
        for k, ln in enumerate(lines):
            pen.text(x, y + 18 + k * 14, ln, "sans-sb" if k == 0 else "sans", 9, INK if k == 0 else rgb("#3a444c"))
    y = 252
    items = [("Discovery workshop and site walk", "1", "650.00"),
             ("Wayfinding concept: 3 directions", "1", "1,400.00"),
             ("Lobby directory board artwork", "1", "980.00"),
             ("Floor numerals and door signs (set of 48)", "48", "1,152.00"),
             ("Illustrated welcome mural, lobby wall", "1", "2,300.00"),
             ("Print-ready files and installation guide", "1", "420.00")]
    widths = [tw * 0.6, tw * 0.12, tw * 0.28]
    pg.draw_rect(pymupdf.Rect(M, y, W - M, y + 24), color=None, fill=rgb("#f5ede6"))
    for j, (h, al) in enumerate((("Description", "left"), ("Qty", "right"), ("Amount", "right"))):
        pen.text(M + sum(widths[:j]) + 10, y + 16, h, "sans-sb", 9, accent, align=al, width=widths[j] - 20)
    y += 24
    for desc, q, amt in items:
        y += 26
        pen.text(M + 10, y - 9, desc, "sans", 9.5, INK)
        pen.text(M + widths[0] + 10, y - 9, q, "sans", 9.5, INK, align="right", width=widths[1] - 20)
        pen.text(M + widths[0] + widths[1] + 10, y - 9, amt, "sans", 9.5, INK, align="right", width=widths[2] - 20)
        pg.draw_line((M, y), (W - M, y), color=RULE, width=0.5)
    y += 24
    for k, (lab, val) in enumerate((("Subtotal", "6,902.00"), ("Tax (8%)", "552.16"), ("Total due", "7,454.16"))):
        big = k == 2
        pen.text(M + tw * 0.5, y, lab, "sans-sb" if big else "sans", 11 if big else 9.5, INK)
        pen.text(M, y, val, "sans-sb" if big else "sans", 11 if big else 9.5, accent if big else INK, align="right",
                 width=tw - 10)
        y += 20
    y += 18
    pen.text(M, y, "PAYMENT", "sans-b", 7.5, accent)
    y = pen.para(M, y + 16, tw * 0.8,
                 "Bank transfer within 30 days, quoting the invoice number. Questions about this invoice: "
                 "accounts@mapleandfern.example or +1 (555) 014-3381. Remittance advice to the same address.",
                 "sans", 9, 1.5, rgb("#3a444c"))
    y += 8
    pen.text(M, y, "PROJECT CONTACTS", "sans-b", 7.5, accent)
    y += 16
    for ln in ("Lead designer: Rowan Achebe, rowan@mapleandfern.example, (555) 014-3392",
               "Site contact: Dev Patel, facilities@harborlane.example, (555) 010-7731"):
        pen.text(M, y, ln, "sans", 9, rgb("#3a444c"))
        y += 15
    pg.draw_line((M, H - 70), (W - M, H - 70), color=RULE, width=0.6)
    pen.text(M, H - 52, "Thank you for working with us.", "serif-i", 10, INK)
    pen.text(M, H - 52, "Maple & Fern Studio is a fictional company.", "sans", 7.5, MUTED, align="right", width=tw)
    doc.set_metadata({"title": "Invoice MF-2026-118", "author": "Maple & Fern Studio"})
    _save(doc, path)


# ============================================================================ agreement (signed later)
def make_agreement(path: str) -> None:
    doc = pymupdf.open()
    pg = doc.new_page(width=W, height=H)
    pen = Pen(pg)
    tw = W - 2 * M
    accent = rgb("#8a4f2a")
    pen.text(M, 78, "MAPLE & FERN STUDIO", "sans-b", 8.5, accent)
    pen.text(M, 110, "Design Services Agreement", "serif-b", 24, INK)
    pen.text(M, 132, "Lobby wayfinding and mural  ·  Agreement MF-A-2026-07", "sans", 9.5, MUTED)
    pg.draw_line((M, 150), (W - M, 150), color=RULE, width=0.8)
    y = pen.para(M, 178, tw,
                 "This agreement is made on 21 October 2026 between Maple & Fern Studio (the Studio) and Harbor Lane "
                 "Apartments (the Client) for the design of wayfinding signage and an illustrated mural for the "
                 "lobby at 200 Lantern Quay.", "serif", 10.5, 1.5, after=10)
    clauses = [
        ("1. Scope", "The Studio will deliver a wayfinding concept in three directions, final artwork for the "
                     "directory board, floor numerals and door signs, and an illustrated mural, as described in the "
                     "proposal dated 2 October 2026."),
        ("2. Schedule", "Concepts within three weeks of signing; final files within four weeks of the Client's choice "
                        "of direction. Installation is arranged by the Client."),
        ("3. Fees", "A fixed fee of 6,902.00 plus tax, invoiced on delivery of print-ready files and payable within "
                    "30 days. Two rounds of revisions are included."),
        ("4. Rights", "On payment the Client receives a perpetual licence to use the artwork at the property. The "
                      "Studio may show the work in its portfolio."),
        ("5. Changes", "Changes to the scope are agreed in writing and may adjust the fee and schedule."),
    ]
    for head, body in clauses:
        pen.text(M, y + 4, head, "sans-sb", 10.5, INK)
        y = pen.para(M, y + 22, tw, body, "serif", 10.5, 1.5, after=12)
    y += 10
    col = (tw - 30) / 2
    for i, (who, name) in enumerate((("For the Studio", "Rowan Achebe"), ("For the Client", "Elena Marsh"))):
        x = M + i * (col + 30)
        pen.text(x, y, who.upper(), "sans-b", 7.5, accent)
        pg.draw_line((x, y + 92), (x + col, y + 92), color=rgb("#9aa7b3"), width=0.7)
        pen.text(x, y + 106, name, "sans-sb", 9.5, INK)
    pen.text(M, H - 40, "Maple & Fern Studio and Harbor Lane Apartments are fictional.", "sans", 7.5, MUTED)
    doc.set_metadata({"title": "Design Services Agreement MF-A-2026-07", "author": "Maple & Fern Studio"})
    _save(doc, path)


# ============================================================================ scanned letter
def make_scan(path: str) -> None:
    import numpy as np
    from PIL import Image, ImageFilter

    src = pymupdf.open()
    pg = src.new_page(width=W, height=H)
    pen = Pen(pg)
    tw = W - 2 * 72
    pen.text(72, 86, "HARBOR LANE APARTMENTS", "sans-b", 13, INK)
    pen.text(72, 102, "Leasing Office  ·  200 Lantern Quay  ·  Port Alder", "sans", 9, MUTED)
    pg.draw_line((72, 114), (W - 72, 114), color=INK, width=1)
    pen.text(72, 150, "3 November 2026", "serif", 11)
    pen.text(72, 186, "To all residents of Building B", "serif", 11)
    pen.text(72, 228, "Notice of scheduled maintenance", "serif-b", 14)
    y = 260
    for para in (
        "The hot water boilers in Building B will be serviced on Tuesday 17 November between 9:00 and 15:00. "
        "Hot water will be unavailable in all apartments during this time. Cold water, heating and electricity "
        "are not affected.",
        "The lifts will also be inspected on the same day. Each lift will be out of service for about one hour; "
        "at least one lift will remain in use at all times.",
        "If you rely on hot water for medical reasons, or need help with stairs during the inspection, please let "
        "the leasing office know by Friday 13 November and we will make arrangements.",
        "Thank you for your patience while we keep the building in good working order.",
    ):
        y = pen.para(72, y, tw, para, "serif", 11, 1.55, after=12)
    pen.text(72, y + 26, "Kind regards,", "serif", 11)
    pen.text(72, y + 66, "Elena Marsh", "serif-b", 11)
    pen.text(72, y + 82, "Property Manager", "serif", 11)
    pix = pg.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    src.close()

    rnd = np.random.default_rng(11)
    a = np.asarray(img).astype(np.float32)
    a = 255 - (255 - a) * 0.86  # toner a little faded
    paper = 236 + 8 * np.sin(np.linspace(0, 3.1, a.shape[1]))[None, :] - 10 * np.linspace(0, 1, a.shape[0])[:, None] ** 2
    a = np.minimum(a, paper)
    a += rnd.normal(0, 6.5, a.shape)
    speck = rnd.random(a.shape) > 0.9993
    a[speck] -= rnd.uniform(60, 140, speck.sum())
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    img = img.filter(ImageFilter.GaussianBlur(0.55))
    img = img.rotate(0.7, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=228)
    a = np.asarray(img).astype(np.float32)
    edge = np.linspace(1, 0, 60) ** 2 * 70  # scanner-lid shadow on the left edge
    a[:, :60] -= edge[None, :]
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    out = pymupdf.open()
    out.new_page(width=W, height=H).insert_image(pymupdf.Rect(0, 0, W, H), stream=buf.getvalue())
    out.set_metadata({"title": "Scan 2026-11-03", "producer": "Scanner"})
    out.save(path, garbage=3, deflate=True)
    out.close()


# PyMuPDF writes ToUnicode maps by inverting the font's cmap, and Windows fonts map one glyph to several code
# points: Segoe UI's "(" comes back as U+FD3E, Georgia's space as U+00A0 and its hyphen as U+00AD. Real documents
# made by word processors carry the plain characters, so put those back (text search, redaction presets, copy and
# the Edit text tool then see what a reader sees).
_PLAIN = {0x37E: 0x3B, 0xFD3E: 0x28, 0xFD3F: 0x29, 0xA0: 0x20, 0xAD: 0x2D, 0x2010: 0x2D, 0x2011: 0x2D, 0x2219: 0xB7, 0xA78F: 0xB7}


def _parse_cmap(text: str) -> dict[int, str]:
    import re

    out: dict[int, str] = {}
    hexs = lambda h: "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4))  # noqa: E731
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            out[int(src, 16)] = hexs(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*(\[[^\]]*\]|<[0-9a-fA-F]+>)", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for k, d in enumerate(re.findall(r"<([0-9a-fA-F]+)>", rest)):
                    out[lo + k] = hexs(d)
            else:
                base = hexs(rest[1:-1])
                for k in range(hi - lo + 1):
                    out[lo + k] = base[:-1] + chr(ord(base[-1]) + k)
    return out


def plain_tounicode(doc: pymupdf.Document) -> None:
    for x in range(1, doc.xref_length()):
        t = doc.xref_get_key(x, "ToUnicode")
        if t[0] != "xref":
            continue
        ux = int(t[1].split()[0])
        text = doc.xref_stream(ux).decode("latin-1")
        m = _parse_cmap(text)
        if not any(ord(c) in _PLAIN for v in m.values() for c in v):
            continue
        m = {k: "".join(chr(_PLAIN.get(ord(c), ord(c))) for c in v) for k, v in m.items()}
        width = 4 if "<0000> <FFFF>" in text or "<0000> <ffff>" in text else 2
        items = sorted(m.items())
        body = []
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            body.append(f"{len(chunk)} beginbfchar")
            body += [f"<{k:0{width}x}> <{''.join(f'{ord(c):04x}' for c in v)}>" for k, v in chunk]
            body.append("endbfchar")
        head = text[:text.index("endcodespacerange") + len("endcodespacerange")]
        new = head + "\n" + "\n".join(body) + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        doc.update_stream(ux, new.encode("latin-1"))


def _save(doc: pymupdf.Document, path: str) -> None:
    plain_tounicode(doc)
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    make_report(os.path.join(out_dir, "canopy-report.pdf"))
    make_rental(os.path.join(out_dir, "rental-application.pdf"))
    make_invoice(os.path.join(out_dir, "invoice.pdf"))
    make_agreement(os.path.join(out_dir, "services-agreement.pdf"))
    make_scan(os.path.join(out_dir, "scanned-notice.pdf"))
    print("demo documents in", out_dir)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work", "docs"))
