"""Static HTML rendering of a governance report.

``render_html`` turns the report produced by
:func:`metaxu.governance.aggregate_artifacts` into one self-contained HTML
file — no external scripts, stylesheets, or fonts — so it can be opened
from disk, attached to a review ticket, or archived next to the artifacts
it summarizes. Reports may quote clinical questions, so treat the output
with the same PHI controls as the artifacts themselves.

Design notes: values use the reference dataviz palette (single sequential
blue for magnitude bars — one series, so no legend; fixed status colors,
always paired with a text label, for severity). All text wears ink
tokens, never series color. Dark mode is real: both the OS preference and
an explicit ``data-theme`` attribute are honored.
"""

from __future__ import annotations

import html
from typing import Any


def _bar_rows(rows: list[tuple[str, float, str, str]], max_value: float) -> str:
    """Horizontal bars: (label, value, tip-label, tooltip) against max_value."""
    out = []
    for label, value, tip, tooltip in rows:
        width = 0.0 if max_value <= 0 else max(0.0, min(1.0, value / max_value)) * 100
        out.append(
            f'<div class="bar-row" data-tip="{html.escape(tooltip, quote=True)}">'
            f'<span class="bar-label">{html.escape(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{width:.1f}%"></span></span>'
            f'<span class="bar-value">{html.escape(tip)}</span>'
            f"</div>"
        )
    return "\n".join(out)


def _stat_tile(label: str, value: str, note: str = "", tone: str = "") -> str:
    note_html = f'<div class="tile-note{tone}">{html.escape(note)}</div>' if note else ""
    return (
        f'<div class="tile"><div class="tile-label">{html.escape(label)}</div>'
        f'<div class="tile-value">{html.escape(value)}</div>{note_html}</div>'
    )


def render_html(report: dict[str, Any], title: str = "Metaxu governance report") -> str:
    count = report["artifact_count"]

    # -- KPI tiles ---------------------------------------------------------
    tiles = [_stat_tile("Artifacts", f"{count:,}")]
    if count:
        triggered = sum(p["triggered"] for p in report["policies"].values())
        passed = sum(p["passed"] for p in report["policies"].values())
        tiles.append(
            _stat_tile(
                "Policy pass rate",
                f"{passed / triggered:.0%}" if triggered else "—",
                note=f"{passed:,} of {triggered:,} triggered",
            )
        )
        tiles.append(
            _stat_tile(
                "Hallucination rate",
                f"{report['safety']['hallucination_rate']:.0%}",
                note="artifacts citing never-retrieved resources",
            )
        )
        integrity = report["integrity"]
        tiles.append(
            _stat_tile(
                "Integrity",
                f"{integrity['verified']:,}/{count:,}",
                note=(
                    f"{integrity['failed']} hash mismatch(es)"
                    if integrity["failed"]
                    else "all artifact hashes verified"
                ),
                tone=" bad" if integrity["failed"] else " good",
            )
        )
        tiles.append(
            _stat_tile(
                "Needs review",
                f"{len(report['needs_review']):,}",
                note="critical findings, failed policies, or integrity failures",
                tone=" bad" if report["needs_review"] else " good",
            )
        )
    tiles_html = "\n".join(tiles)

    sections: list[str] = []

    # -- Trust dimensions (0..1 magnitude, single hue, value at tip) -------
    if report["trust"]:
        rows = [
            (
                dimension,
                stats["mean"],
                f"{stats['mean']:.2f}",
                f"{dimension}: mean {stats['mean']:.2f}, min {stats['min']:.2f} "
                f"across {stats['artifacts']} artifact(s)",
            )
            for dimension, stats in report["trust"].items()
        ]
        sections.append(
            "<section><h2>Trust dimensions</h2>"
            '<p class="section-note">Mean score per dimension across all artifacts '
            "(hover for the minimum). Deliberately not aggregated into one number.</p>"
            f'<div class="bars">{_bar_rows(rows, 1.0)}</div></section>'
        )

    # -- Policy pass rates --------------------------------------------------
    if report["policies"]:
        rows = []
        details = []
        for name, stats in report["policies"].items():
            rows.append(
                (
                    name,
                    stats["pass_rate"],
                    f"{stats['passed']}/{stats['triggered']}",
                    f"{name}: {stats['pass_rate']:.0%} pass rate",
                )
            )
            if stats["top_unsatisfied_requirements"]:
                worst = ", ".join(
                    f"{html.escape(req)} ×{n}"
                    for req, n in stats["top_unsatisfied_requirements"].items()
                )
                details.append(
                    f"<li><strong>{html.escape(name)}</strong> most unsatisfied: {worst}</li>"
                )
        details_html = f'<ul class="detail-list">{"".join(details)}</ul>' if details else ""
        sections.append(
            "<section><h2>Policy pass rates</h2>"
            f'<div class="bars">{_bar_rows(rows, 1.0)}</div>{details_html}</section>'
        )

    # -- Safety findings ----------------------------------------------------
    safety = report["safety"]
    if count:
        severity_chips = "".join(
            f'<span class="chip {html.escape(sev)}">'
            f"{html.escape(sev)}: {n}</span>"
            for sev, n in safety["findings_by_severity"].items()
        ) or '<span class="chip good">no findings</span>'
        if safety["findings_by_check"]:
            max_findings = max(safety["findings_by_check"].values())
            rows = [
                (check, float(n), str(n), f"{check}: {n} finding(s)")
                for check, n in safety["findings_by_check"].items()
            ]
            findings_html = f'<div class="bars">{_bar_rows(rows, float(max_findings))}</div>'
        else:
            findings_html = ""
        sections.append(
            "<section><h2>Safety findings</h2>"
            f'<p class="section-note">unsupported-claim rate '
            f"{safety['unsupported_claim_rate']:.0%} · severity: {severity_chips}</p>"
            f"{findings_html}</section>"
        )

    # -- Tool reliability (table) --------------------------------------------
    if report["tools"]:
        tool_rows = []
        for name, stats in report["tools"].items():
            mean = stats["mean_duration_ms"]
            duration = f"{mean:.1f}" if mean is not None else "—"
            tool_rows.append(
                "<tr>"
                f"<td>{html.escape(name)}</td>"
                f'<td class="num">{stats["calls"]:,}</td>'
                f'<td class="num">{stats["error_rate"]:.0%}</td>'
                f'<td class="num">{duration}</td>'
                "</tr>"
            )
        rows = "".join(tool_rows)
        sections.append(
            "<section><h2>Tool reliability</h2>"
            '<table><thead><tr><th>Tool</th><th class="num">Calls</th>'
            '<th class="num">Error rate</th><th class="num">Mean ms</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></section>"
        )

    # -- Missing data ---------------------------------------------------------
    if report["missing_data"]:
        items = "".join(
            f"<li>{html.escape(item)} <span class='mono'>×{n}</span></li>"
            for item, n in report["missing_data"].items()
        )
        sections.append(
            f"<section><h2>Most-missed data</h2><ul class='detail-list'>{items}</ul></section>"
        )

    # -- Needs review (table) --------------------------------------------------
    if report["needs_review"]:
        rows = "".join(
            "<tr>"
            f"<td class='mono'>{html.escape(entry['artifact_id'])}</td>"
            f"<td>{html.escape(_truncate(entry['question'], 90))}</td>"
            f"<td>{html.escape('; '.join(entry['reasons']))}</td>"
            "</tr>"
            for entry in report["needs_review"]
        )
        sections.append(
            "<section><h2>Needs review</h2>"
            "<table><thead><tr><th>Artifact</th><th>Question</th><th>Reasons</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )

    time_range = report["time_range"]
    period = (
        f"{html.escape(time_range['from'])} — {html.escape(time_range['to'])}"
        if time_range
        else "no artifacts"
    )
    observers = ", ".join(
        f"{html.escape(str(k))} ({v})" for k, v in sorted(report["observers"].items())
    )

    return _PAGE_TEMPLATE.format(
        title=html.escape(title),
        period=period,
        observers=observers or "—",
        tiles=tiles_html,
        sections="\n".join(sections),
    )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
.viz-root {{
  color-scheme: light;
  --page:           #f9f9f7;
  --surface-1:      #fcfcfb;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --grid:           #e1e0d9;
  --baseline:       #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --series-1:       #2a78d6;
  --good:           #0ca30c;
  --warning:        #fab219;
  --critical:       #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface-1:      #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --baseline:       #383835;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
  }}
}}
:root[data-theme="dark"] .viz-root {{
  color-scheme: dark;
  --page:           #0d0d0d;
  --surface-1:      #1a1a19;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --grid:           #2c2c2a;
  --baseline:       #383835;
  --border:         rgba(255,255,255,0.10);
  --series-1:       #3987e5;
}}
.viz-root {{
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--text-primary);
  margin: 0; padding: 24px; min-height: 100vh; box-sizing: border-box;
}}
.viz-root * {{ box-sizing: border-box; }}
header h1 {{ font-size: 20px; margin: 0 0 4px; }}
header .meta {{ color: var(--text-muted); font-size: 13px; margin-bottom: 20px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 24px; }}
.tile {{ background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; }}
.tile-label {{ font-size: 12px; color: var(--text-secondary); }}
.tile-value {{ font-size: 30px; font-weight: 600; margin-top: 2px; }}
.tile-note {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; }}
.tile-note.bad {{ color: var(--critical); }}
.tile-note.good {{ color: var(--good); }}
section {{ background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }}
section h2 {{ font-size: 14px; margin: 0 0 8px; }}
.section-note {{ font-size: 12px; color: var(--text-secondary); margin: 0 0 12px; }}
.bars {{ display: flex; flex-direction: column; gap: 8px; }}
.bar-row {{ display: grid; grid-template-columns: 200px 1fr 72px;
  align-items: center; gap: 10px; }}
.bar-label {{ font-size: 12.5px; color: var(--text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.bar-track {{ position: relative; height: 20px; border-left: 1px solid var(--baseline); }}
.bar-fill {{ position: absolute; inset: 0 auto 0 0; background: var(--series-1);
  border-radius: 0 4px 4px 0; min-width: 2px; }}
.bar-value {{ font-size: 12px; color: var(--text-secondary);
  font-variant-numeric: tabular-nums; }}
.chip {{ display: inline-block; border: 1px solid var(--border); border-radius: 999px;
  padding: 1px 9px; font-size: 11.5px; margin-right: 4px; color: var(--text-primary); }}
.chip::before {{ content: "●"; margin-right: 5px; font-size: 9px; vertical-align: 1px; }}
.chip.critical::before {{ color: var(--critical); }}
.chip.warning::before {{ color: var(--warning); }}
.chip.info::before, .chip.good::before {{ color: var(--good); }}
table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; }}
th {{ text-align: left; color: var(--text-muted); font-weight: 500;
  border-bottom: 1px solid var(--grid); padding: 4px 10px 6px 0; }}
td {{ border-bottom: 1px solid var(--grid); padding: 6px 10px 6px 0;
  color: var(--text-secondary); vertical-align: top; }}
th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.detail-list {{ font-size: 12.5px; color: var(--text-secondary);
  margin: 10px 0 0; padding-left: 18px; }}
.mono {{ font-family: ui-monospace, monospace; font-size: 11.5px; }}
#tip {{ position: fixed; display: none; background: var(--text-primary);
  color: var(--surface-1); font-size: 12px; padding: 5px 9px; border-radius: 6px;
  pointer-events: none; max-width: 340px; z-index: 10; }}
</style>
</head>
<body class="viz-root">
<header>
  <h1>{title}</h1>
  <div class="meta">period {period} · observers: {observers}</div>
</header>
<div class="tiles">
{tiles}
</div>
{sections}
<div id="tip" role="tooltip"></div>
<script>
(function () {{
  var tip = document.getElementById("tip");
  document.querySelectorAll(".bar-row[data-tip]").forEach(function (row) {{
    row.addEventListener("mousemove", function (e) {{
      tip.textContent = row.getAttribute("data-tip");
      tip.style.display = "block";
      var pad = 12;
      var x = Math.min(e.clientX + pad, window.innerWidth - tip.offsetWidth - pad);
      var y = Math.min(e.clientY + pad, window.innerHeight - tip.offsetHeight - pad);
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    }});
    row.addEventListener("mouseleave", function () {{ tip.style.display = "none"; }});
  }});
}})();
</script>
</body>
</html>
"""
