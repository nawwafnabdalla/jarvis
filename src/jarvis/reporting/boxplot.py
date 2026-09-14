"""Hand-rolled SVG box plots (WP-019/WP-020 characterization: stdlib +
numpy only, no charting dependency -- see D-074 for why, and for the
ATR-unit / one-shared-SVG-with-side-by-side-panels design choices, both
inferences from R1's spec text rather than something the text states
outright).

A box plot needs only a five-number summary per category (Q1, median,
Q3, whiskers, outliers) -- all directly computable from `numpy.
percentile`, with no dependency beyond what this project already has.
Renders one SVG containing one panel per named group (e.g. one per
session), each with its own y-scale, years along a shared x-axis
convention.
"""

from dataclasses import dataclass

import numpy as np

_PANEL_WIDTH = 360
_PANEL_HEIGHT = 260
_PANEL_GAP = 30
_MARGIN_TOP = 36
_MARGIN_BOTTOM = 46
_MARGIN_LEFT = 50
_MARGIN_RIGHT = 16
_BOX_WIDTH = 22


@dataclass(frozen=True, slots=True)
class BoxStats:
    label: str  # e.g. a year, as a string
    q1: float
    median: float
    q3: float
    whisker_lo: float
    whisker_hi: float
    outliers: tuple[float, ...]
    n: int


def compute_box_stats(label: str, values: np.ndarray) -> BoxStats:
    """Standard Tukey five-number summary: whiskers extend to the most
    extreme data point still within 1.5*IQR of the box; anything beyond
    that is plotted individually as an outlier, never hidden."""
    q1, median, q3 = (float(v) for v in np.percentile(values, [25, 50, 75]))
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    within = values[(values >= lo_fence) & (values <= hi_fence)]
    whisker_lo = float(within.min()) if within.size else q1
    whisker_hi = float(within.max()) if within.size else q3
    outliers = tuple(float(v) for v in values[(values < lo_fence) | (values > hi_fence)])
    return BoxStats(
        label=label, q1=q1, median=median, q3=q3, whisker_lo=whisker_lo, whisker_hi=whisker_hi,
        outliers=outliers, n=values.size,
    )


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_panel(title: str, boxes: list[BoxStats], *, x_offset: int) -> list[str]:
    plot_top = _MARGIN_TOP
    plot_bottom = _PANEL_HEIGHT - _MARGIN_BOTTOM
    plot_height = plot_bottom - plot_top
    plot_left = x_offset + _MARGIN_LEFT
    plot_right = x_offset + _PANEL_WIDTH - _MARGIN_RIGHT
    plot_width = plot_right - plot_left

    all_values = [v for b in boxes for v in (b.whisker_lo, b.whisker_hi, *b.outliers)] or [0.0, 1.0]
    y_min, y_max = min(all_values), max(all_values)
    if y_min == y_max:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    pad = (y_max - y_min) * 0.08
    y_min, y_max = y_min - pad, y_max + pad

    def y(v: float) -> float:
        return plot_bottom - (v - y_min) / (y_max - y_min) * plot_height

    lines = [
        f'<text x="{x_offset + _PANEL_WIDTH / 2:.1f}" y="18" text-anchor="middle" '
        f'font-family="sans-serif" font-size="14" font-weight="bold">{_svg_escape(title)}</text>',
        f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" y2="{plot_bottom}" stroke="#333" stroke-width="1"/>',
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" y2="{plot_bottom}" stroke="#333" stroke-width="1"/>',
    ]

    # Y-axis ticks: 4 evenly-spaced labels.
    for i in range(5):
        v = y_min + (y_max - y_min) * i / 4
        yy = y(v)
        lines.append(f'<line x1="{plot_left - 4}" y1="{yy:.1f}" x2="{plot_left}" y2="{yy:.1f}" stroke="#333"/>')
        lines.append(
            f'<text x="{plot_left - 7:.1f}" y="{yy + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="9">{v:.4f}</text>'
        )

    if not boxes:
        return lines

    slot_width = plot_width / len(boxes)
    for i, b in enumerate(boxes):
        cx = plot_left + slot_width * (i + 0.5)
        box_left, box_right = cx - _BOX_WIDTH / 2, cx + _BOX_WIDTH / 2
        y_q1, y_q3, y_med = y(b.q1), y(b.q3), y(b.median)
        y_whisker_lo, y_whisker_hi = y(b.whisker_lo), y(b.whisker_hi)

        lines.append(f'<line x1="{cx:.1f}" y1="{y_whisker_lo:.1f}" x2="{cx:.1f}" y2="{y_q1:.1f}" stroke="#333"/>')
        lines.append(f'<line x1="{cx:.1f}" y1="{y_q3:.1f}" x2="{cx:.1f}" y2="{y_whisker_hi:.1f}" stroke="#333"/>')
        lines.append(
            f'<rect class="box" x="{box_left:.1f}" y="{min(y_q1, y_q3):.1f}" width="{_BOX_WIDTH}" '
            f'height="{abs(y_q1 - y_q3):.1f}" fill="#a8c8e8" stroke="#333"/>'
        )
        lines.append(
            f'<line x1="{box_left:.1f}" y1="{y_med:.1f}" x2="{box_right:.1f}" y2="{y_med:.1f}" '
            f'stroke="#333" stroke-width="2"/>'
        )
        for o in b.outliers:
            lines.append(f'<circle class="outlier" cx="{cx:.1f}" cy="{y(o):.1f}" r="2.5" fill="none" stroke="#c00"/>')
        lines.append(
            f'<text x="{cx:.1f}" y="{plot_bottom + 16}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="10">{_svg_escape(b.label)}</text>'
        )

    return lines


def render_box_plot_svg(panels: list[tuple[str, list[BoxStats]]]) -> str:
    """`panels`: one (title, boxes) pair per panel, rendered side by side
    left to right, each with its own y-scale."""
    total_width = len(panels) * _PANEL_WIDTH + max(0, len(panels) - 1) * _PANEL_GAP
    body: list[str] = []
    x_offset = 0
    for title, boxes in panels:
        body.extend(_render_panel(title, boxes, x_offset=x_offset))
        x_offset += _PANEL_WIDTH + _PANEL_GAP

    header = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_width} {_PANEL_HEIGHT}" '
        f'width="{total_width}" height="{_PANEL_HEIGHT}">'
        f'<rect x="0" y="0" width="{total_width}" height="{_PANEL_HEIGHT}" fill="white"/>'
    )
    return header + "".join(body) + "</svg>"
