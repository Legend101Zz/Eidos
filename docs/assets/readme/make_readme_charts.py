#!/usr/bin/env python3
"""Draw the experiment charts shown in README.md.

Run:  python docs/assets/readme/make_readme_charts.py [--out DIR]

Writes light and dark SVG variants of four charts next to this script:

    results-by-pilot-{light,dark}.svg
    reliability-{light,dark}.svg
    pilot4-by-card-{light,dark}.svg
    tokens-{light,dark}.svg

README.md embeds each pair with <picture> so GitHub picks the variant that
matches the reader's colour scheme.

Every number below is copied from the README tables or from the Pilot 4
capture; the comment above each block names the source. No number is
computed, rounded or estimated here: the only derived values are bar
heights, which are the printed x/n fractions.

Colour pairs (validated for lightness band, chroma floor, colour-blind
separation and contrast against the chart surface):
    Eidos investigator            blue    light #2a78d6   dark #3987e5
    Coding agent + Eidos toolkit  orange  light #eb6834   dark #d95926
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, PathPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402

# Text is drawn as glyph outlines so every viewer renders identical geometry
# (no font substitution, no label overlap on GitHub). DejaVu Sans ships with
# matplotlib, so the output is the same on every machine.
plt.rcParams["svg.fonttype"] = "path"
plt.rcParams["svg.hashsalt"] = "eidos-readme-charts"
plt.rcParams["font.family"] = "DejaVu Sans"

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

# Verified improvements per pilot: (verified, runs).
# Source: README.md "How Eidos has improved" table (columns "Eidos
# investigator" and "Coding agent + toolkit") and the Benchmarks four-arm
# table (arm 2 and arm 4 rows). Verified = confirmed by the independent
# two-VM grader; Pilot 4 uses the hosted verdict plus the run-status gate.
#   Pilot 1, card D02, 3 runs:   Eidos 1/3,   coding agent 3/3
#   Pilot 2, D03-D06, 12 runs:   Eidos 12/12, coding agent 12/12
#   Pilot 3, N01-N04, 12 runs:   Eidos 2/12,  coding agent 11/12
#   Pilot 4, N05-N08 sealed, 24 runs: Eidos 20/24, coding agent 19/24
PILOTS = [
    # name,      detail line,              Eidos (arm 4), coding agent + toolkit (arm 2)
    ("Pilot 1", "D02 · 3 runs", (1, 3), (3, 3)),
    ("Pilot 2", "D03–D06 · 12 runs", (12, 12), (12, 12)),
    ("Pilot 3", "N01–N04 · 12 runs", (2, 12), (11, 12)),
    ("Pilot 4", "N05–N08 sealed · 24 runs", (20, 24), (19, 24)),
]

# Eidos investigator runs that completed: (completed, runs).
# Source: README.md "How Eidos has improved" table.
#   Pilot 3 (257792a): 2/12, ten runs stopped by one timing rule; the two
#       that concluded were verified.
#   Round-4 gate (e503821), 7 practice cards: 7/7 completed.
#   Gate G-R5 (86dfe57), 7 practice cards: 7/7 completed.
#   Pilot 4 (86dfe57), 24 runs on sealed cards: 24/24 completed
#       (all ended with run status ok; none crashed, timed out or hit the
#       token ceiling).
RELIABILITY = [
    ("Pilot 3", "build 257792a · 12 runs", (2, 12)),
    ("Round-4 gate", "build e503821 · 7 cards", (7, 7)),
    ("Gate G-R5", "build 86dfe57 · 7 cards", (7, 7)),
    ("Pilot 4", "build 86dfe57 · 24 runs", (24, 24)),
]

# Pilot 4 verified improvements per card, six runs per arm and card.
# Source: README.md Benchmarks, "Pilot 4" table (verified improvements, six
# runs per arm and card), and summary.json -> card_arm[].hosted_verified in
# the final Addendum-4 capture (final-addendum4-r6-63506160a763). Metrics
# per card are from the README mean-verified-gain table.
#   N05: coding agent 4/6, Eidos 6/6   (accuracy)
#   N06: coding agent 5/6, Eidos 5/6   (accuracy)
#   N07: coding agent 4/6, Eidos 4/6   (RMSE reduction)
#   N08: coding agent 6/6, Eidos 5/6   (RMSE reduction)
PILOT4_CARDS = [
    # card,  metric,          Eidos (arm 4), coding agent + toolkit (arm 2)
    ("N05", "accuracy", (6, 6), (4, 6)),
    ("N06", "accuracy", (5, 6), (5, 6)),
    ("N07", "RMSE reduction", (4, 6), (4, 6)),
    ("N08", "RMSE reduction", (5, 6), (6, 6)),
]

# Processed tokens per pilot and arm, in millions (total over all runs of
# the arm in that pilot; runs per arm are on the x-axis).
# Source: README.md "How Eidos has improved" table, column "Eidos tokens vs
# arm 2": Pilot 1 7.5M vs 31.3M, Pilot 2 5.6M vs 30.6M, Pilot 3 15.3M vs
# 30.5M, Pilot 4 24.0M vs 78.9M. Pilot 4 also in estimator/summary capture:
# arm 4 24,048,475 and arm 2 78,936,966 tokens (summary.json -> resources).
# Processed tokens include cache reads. They are not cost.
TOKENS = [
    # name,      detail line,              Eidos (arm 4), coding agent + toolkit (arm 2)
    ("Pilot 1", "D02 · 3 runs", 7.5, 31.3),
    ("Pilot 2", "D03–D06 · 12 runs", 5.6, 30.6),
    ("Pilot 3", "N01–N04 · 12 runs", 15.3, 30.5),
    ("Pilot 4", "N05–N08 sealed · 24 runs", 24.0, 78.9),
]

EIDOS_LABEL = "Eidos investigator"
CODING_LABEL = "Coding agent + Eidos toolkit"

# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

THEMES = {
    "light": dict(
        surface="#fcfcfb", border="#e3e2dd", text="#1f1f1e", text2="#5c5b56",
        grid="#e9e8e3", axis="#cfcec8", eidos="#2a78d6", coding="#eb6834",
    ),
    "dark": dict(
        surface="#1a1a19", border="#2e2e2c", text="#f0efec", text2="#c3c2b7",
        grid="#2c2c2a", axis="#4a4a46", eidos="#3987e5", coding="#d95926",
    ),
}

# Geometry, in points (1 pt = 1/72 inch). The SVG is 640 pt wide.
W = 640.0
PAD_X = 24.0
BAR_W = 30.0     # thin bars
BAR_GAP = 4.0    # small gap between the two bars of a pair
BAR_R = 3.0      # rounded data-end, square at the baseline
AX_LEFT = 58.0
AX_RIGHT = 20.0
AX_BOTTOM_PAD = 50.0   # room under the baseline for the two-line category labels
TOP_NO_LEGEND = 74.0
TOP_LEGEND = 90.0


class Chart:
    """One figure in one theme, positioned in points from the top-left."""

    def __init__(self, theme: str, height: float, title: str, subtitle: str,
                 legend: bool, n_cats: int, ylim: tuple[float, float],
                 yticks: list[float], ytick_fmt):
        self.t = THEMES[theme]
        self.H = height
        self.fig = plt.figure(figsize=(W / 72, height / 72), dpi=72)
        self.fig.patch.set_alpha(0.0)
        # Rounded card behind everything: surface fill, hairline border.
        card = FancyBboxPatch(
            (0.5 / 72, 0.5 / 72), (W - 1) / 72, (height - 1) / 72,
            boxstyle="round,pad=0,rounding_size=0.11",
            transform=self.fig.dpi_scale_trans, facecolor=self.t["surface"],
            edgecolor=self.t["border"], linewidth=1.0, zorder=0,
        )
        self.fig.add_artist(card)

        self.texts: list = []   # every text artist, for the overlap check
        self.bars: list = []    # every bar patch, for the overlap check

        self.title_artist = self._fig_text(
            PAD_X, 29, title, size=13.5, weight="bold", color=self.t["text"])
        self._fig_text(PAD_X, 48, subtitle, size=9.5, color=self.t["text2"])

        top = TOP_LEGEND if legend else TOP_NO_LEGEND
        self.ax_w = W - AX_LEFT - AX_RIGHT
        self.ax_h = height - top - AX_BOTTOM_PAD
        ax = self.fig.add_axes([
            AX_LEFT / W, AX_BOTTOM_PAD / height, self.ax_w / W, self.ax_h / height])
        self.ax = ax
        ax.set_facecolor("none")
        ax.set_xlim(-0.5, n_cats - 0.5)
        ax.set_ylim(*ylim)
        self.n_cats = n_cats
        self.ylim = ylim
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(self.t["axis"])
        ax.spines["bottom"].set_linewidth(1.0)
        ax.set_xticks([])
        ax.set_yticks(yticks)
        ax.set_yticklabels([ytick_fmt(v) for v in yticks])
        ax.tick_params(axis="y", length=0, pad=8, labelsize=9, labelcolor=self.t["text2"])
        ax.grid(axis="y", color=self.t["grid"], linewidth=1.0, linestyle="-")
        ax.set_axisbelow(True)
        for lbl in ax.get_yticklabels():
            self.texts.append(lbl)

        if legend:
            self._legend(y_top=TOP_LEGEND - 20)

    # -- helpers ---------------------------------------------------------
    def _fig_text(self, x_pt, y_top_pt, s, size, color, weight="normal",
                  ha="left", va="baseline"):
        txt = self.fig.text(
            x_pt / 72, (self.H - y_top_pt) / 72, s, fontsize=size, color=color,
            fontweight=weight, ha=ha, va=va, transform=self.fig.dpi_scale_trans)
        self.texts.append(txt)
        return txt

    def _legend(self, y_top):
        """Swatch + label per series, left-aligned under the subtitle."""
        renderer = self.fig.canvas.get_renderer()
        x = PAD_X
        sw = 8.0
        for label, key in ((EIDOS_LABEL, "eidos"), (CODING_LABEL, "coding")):
            sq = FancyBboxPatch(
                (x / 72, (self.H - y_top - 0.5) / 72), sw / 72, sw / 72,
                boxstyle="round,pad=0,rounding_size=0.02",
                transform=self.fig.dpi_scale_trans, facecolor=self.t[key],
                edgecolor="none")
            self.fig.add_artist(sq)
            txt = self._fig_text(x + sw + 6, y_top, label, size=9.5,
                                 color=self.t["text2"])
            bb = txt.get_window_extent(renderer)
            x += sw + 6 + bb.width + 22

    def slot_pt(self):
        return self.ax_w / self.n_cats

    def pt_to_x(self, pts):
        return pts / self.slot_pt()

    def bar(self, x_center_units, height, color, offset_pt=0.0, width_pt=BAR_W):
        """Column with a rounded data-end and a square base on the axis."""
        xc = x_center_units + self.pt_to_x(offset_pt)
        hw = self.pt_to_x(width_pt) / 2
        x0, x1 = xc - hw, xc + hw
        y_range = self.ylim[1] - self.ylim[0]
        rx = self.pt_to_x(BAR_R)
        ry = BAR_R / (self.ax_h / y_range)
        r = min(ry, height)
        verts = [
            (x0, 0), (x0, height - r), (x0, height), (x0 + rx, height),
            (x1 - rx, height), (x1, height), (x1, height - r), (x1, 0), (x0, 0),
        ]
        codes = [MPath.MOVETO, MPath.LINETO, MPath.CURVE3, MPath.CURVE3,
                 MPath.LINETO, MPath.CURVE3, MPath.CURVE3, MPath.LINETO,
                 MPath.CLOSEPOLY]
        patch = PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none",
                          zorder=3, linewidth=0)
        self.ax.add_patch(patch)
        self.bars.append(patch)
        return xc

    def value_label(self, xc, height, s):
        txt = self.ax.annotate(
            s, (xc, height), xytext=(0, 4), textcoords="offset points",
            ha="center", va="bottom", fontsize=9, color=self.t["text"],
            fontweight="bold", zorder=4)
        self.texts.append(txt)

    def category_label(self, i, name, detail):
        for dy, s, size, color, weight in (
            (-17, name, 10.5, self.t["text"], "bold"),
            (-31, detail, 8.5, self.t["text2"], "normal"),
        ):
            txt = self.ax.annotate(
                s, (i, 0), xytext=(0, dy), textcoords="offset points",
                ha="center", va="baseline", fontsize=size, color=color,
                fontweight=weight, annotation_clip=False)
            self.texts.append(txt)

    # -- checks and output ----------------------------------------------
    def check(self, name):
        """Fail loudly on clipped or overlapping text."""
        self.fig.canvas.draw()
        r = self.fig.canvas.get_renderer()
        fig_bb = self.fig.bbox
        problems = []
        boxes = []
        for t in self.texts:
            if not t.get_text().strip() or not t.get_visible():
                continue
            bb = t.get_window_extent(r)
            boxes.append((t.get_text(), bb))
            if bb.x0 < fig_bb.x0 + 8 or bb.x1 > fig_bb.x1 - 8 \
                    or bb.y0 < fig_bb.y0 + 4 or bb.y1 > fig_bb.y1 - 4:
                problems.append(f"text outside card margin: {t.get_text()!r} {bb}")
        for (a, ba), (b, bb2) in combinations(boxes, 2):
            if ba.overlaps(bb2):
                problems.append(f"text overlap: {a!r} vs {b!r}")
        for s, bb in boxes:
            for patch in self.bars:
                pb = patch.get_window_extent(r)
                if bb.overlaps(pb):
                    problems.append(f"text over bar: {s!r}")
        if problems:
            raise SystemExit(f"[{name}] layout problems:\n  " + "\n  ".join(problems))

    def save(self, path: Path, title: str):
        self.fig.savefig(path, format="svg", transparent=True,
                         metadata={"Date": None, "Creator": "make_readme_charts.py",
                                   "Title": title})
        plt.close(self.fig)


def pct(v):
    return f"{int(round(v * 100))}%"


# --------------------------------------------------------------------------
# The four charts
# --------------------------------------------------------------------------

def paired_chart(theme, name, title, subtitle, rows, ylim, yticks, ytick_fmt,
                 value_of, label_of, height=352.0):
    c = Chart(theme, height, title, subtitle, legend=True, n_cats=len(rows),
              ylim=ylim, yticks=yticks, ytick_fmt=ytick_fmt)
    off = (BAR_W + BAR_GAP) / 2
    for i, row in enumerate(rows):
        cat, detail, eidos, coding = row
        for key, offset, data in (("eidos", -off, eidos), ("coding", +off, coding)):
            h = value_of(data)
            xc = c.bar(i, h, c.t[key], offset_pt=offset)
            c.value_label(xc, h, label_of(data))
        c.category_label(i, cat, detail)
    return c


def chart_results_by_pilot(theme):
    # Bars show the verified-success rate; labels show the printed x/n.
    rows = [(n, d, e, k) for n, d, e, k in PILOTS]
    return paired_chart(
        theme, "results-by-pilot",
        "Eidos trailed in Pilots 1 and 3, and matched in Pilots 2 and 4",
        "Verified improvements per pilot (label = verified runs / runs)",
        rows, ylim=(0, 1.09), yticks=[0, 0.25, 0.5, 0.75, 1.0], ytick_fmt=pct,
        value_of=lambda d: d[0] / d[1], label_of=lambda d: f"{d[0]}/{d[1]}")


def chart_pilot4_by_card(theme):
    rows = [(card, metric, e, k) for card, metric, e, k in PILOT4_CARDS]
    return paired_chart(
        theme, "pilot4-by-card",
        "Pilot 4: Eidos led on N05, trailed on N08, tied on N06 and N07",
        "Verified improvements per card, six runs per arm (sealed cards)",
        rows, ylim=(0, 6.6), yticks=[0, 1, 2, 3, 4, 5, 6], ytick_fmt=lambda v: f"{int(v)}",
        value_of=lambda d: d[0], label_of=lambda d: f"{d[0]}/{d[1]}")


def chart_tokens(theme):
    rows = [(n, d, e, k) for n, d, e, k in TOKENS]
    return paired_chart(
        theme, "tokens",
        "The Eidos investigator processed fewer tokens in every pilot",
        "Total processed tokens per arm, millions (includes cache reads; not cost)",
        rows, ylim=(0, 86), yticks=[0, 20, 40, 60, 80], ytick_fmt=lambda v: f"{int(v)}M",
        value_of=lambda d: d, label_of=lambda d: f"{d}M")


def chart_reliability(theme):
    c = Chart(theme, 330.0,
              "Completed investigator runs rose from 2 of 12 to 24 of 24",
              "Eidos investigator runs that completed (label = completed / runs)",
              legend=False, n_cats=len(RELIABILITY), ylim=(0, 1.09),
              yticks=[0, 0.5, 1.0], ytick_fmt=pct)
    for i, (name, detail, (done, runs)) in enumerate(RELIABILITY):
        h = done / runs
        xc = c.bar(i, h, c.t["eidos"], width_pt=40.0)
        c.value_label(xc, h, f"{done}/{runs}")
        c.category_label(i, name, detail)
    return c


CHARTS = {
    "results-by-pilot": chart_results_by_pilot,
    "reliability": chart_reliability,
    "pilot4-by-card": chart_pilot4_by_card,
    "tokens": chart_tokens,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, build in CHARTS.items():
        for theme in ("light", "dark"):
            chart = build(theme)
            chart.check(f"{name}-{theme}")
            target = args.out / f"{name}-{theme}.svg"
            chart.save(target, name.replace("-", " "))
            print("wrote", target)


if __name__ == "__main__":
    main()
