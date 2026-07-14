"""Strictly public presentation payloads and one-page PDF exports.

Only the fields explicitly allowlisted in this module can reach a share or
download surface.  Personal state, provider diagnostics, filesystem paths,
and credentials are intentionally not accepted as inputs to the renderer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import math
import re
from typing import Any, Mapping, Optional


_UNSAFE_TEXT = (
    re.compile(r"/(?:Users|home|private|tmp|var|etc|mount|mnt)(?:/|\b)", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\bTraceback\b|\b(?:[A-Za-z]+Error|Exception):", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password)\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _clean_text(value: Any, fallback: str = "Unavailable", *, limit: int = 500) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text or any(pattern.search(text) for pattern in _UNSAFE_TEXT):
        return fallback
    # Built-in PDF fonts are deliberately used for dependable cloud exports.
    text = (
        text.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )
    return text[:limit]


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _money(value: Any) -> str:
    number = _number(value)
    return f"${number:,.2f}" if number is not None else "Unavailable"


def _percent(value: Any) -> str:
    number = _number(value)
    return f"{number:.0f}%" if number is not None else "Unavailable"


def build_presentation_payload(
    decision: Optional[Mapping[str, Any]],
    *,
    timeframe: str,
    tradingview_symbol: str,
    tradingview_url: str,
    generated_at: Optional[datetime] = None,
    selected_analysis: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build a public-only export model from one analysis decision."""

    source = decision if isinstance(decision, Mapping) else {}
    plan = source.get("plan") if isinstance(source.get("plan"), Mapping) else {}
    market = (
        source.get("market_context")
        if isinstance(source.get("market_context"), Mapping)
        else {}
    )
    frames = source.get("timeframes") if isinstance(source.get("timeframes"), Mapping) else {}
    selected_timeframe = str(timeframe or "15m").strip()
    alignment: list[dict[str, str]] = []
    engine_labels = {
        "1M": "1M",
        "1W": "1W",
        "1D": "1D",
        "4H": "4H",
        "1H": "1H",
        "15m": "15M",
        "5m": "5M",
    }
    for label in ("1M", "1W", "1D", "4H", "1H", "30m", "15m", "5m", "3m", "1m"):
        engine_label = engine_labels.get(label)
        row = frames.get(engine_label) if engine_label and isinstance(frames.get(engine_label), Mapping) else {}
        if label == selected_timeframe and isinstance(selected_analysis, Mapping):
            merged_row = dict(row)
            selected_direction = selected_analysis.get("direction")
            if str(selected_direction or "").strip().lower() not in {
                "",
                "unavailable",
                "unknown",
            }:
                merged_row["direction"] = selected_direction
            row = merged_row
        alignment.append(
            {
                "timeframe": label,
                "direction": _clean_text(row.get("direction"), "Unavailable", limit=24).title(),
                "active": "yes" if label == selected_timeframe else "no",
            }
        )

    raw_reasons = source.get("reasons") if isinstance(source.get("reasons"), list) else []
    earnings_status = str(source.get("earnings_status") or "unresolved").strip().lower()
    earnings_label = (
        f"{_clean_text(source.get('earnings_date_status'), 'Scheduled', limit=16).title()} { _clean_text(source.get('earnings_date'), 'Unavailable', limit=16)}"
        if earnings_status == "scheduled" and source.get("earnings_date")
        else f"No vendor-calendar event through {_clean_text(source.get('earnings_checked_through'), 'checked window', limit=20)}"
        if earnings_status == "verified_none"
        else "Unresolved - entry gated"
    )
    timestamp = generated_at or datetime.now(timezone.utc)
    return {
        "product": "Trading Autopilot",
        "ticker": _clean_text(str(source.get("ticker") or "").upper(), "UNRESOLVED", limit=24),
        "name": _clean_text(source.get("name"), "Security name unavailable", limit=90),
        "exchange": _clean_text(source.get("exchange"), "Exchange unresolved", limit=32),
        "verdict": _clean_text(source.get("verdict"), "PASS", limit=32).upper(),
        "state": _clean_text(source.get("state"), "BLOCKED", limit=32).upper(),
        "direction": _clean_text(source.get("direction"), "Neutral", limit=24).title(),
        "confidence": _percent(source.get("confidence")),
        "grade": _clean_text(source.get("grade"), "-", limit=8),
        "current_price": _money(source.get("current_price")),
        "market_status": _clean_text(source.get("market_status"), "Unavailable", limit=24).title(),
        "data_label": _clean_text(source.get("data_label"), "Unavailable", limit=32).title(),
        "data_source": _clean_text(source.get("data_source"), "Unavailable", limit=32),
        "data_timestamp": _clean_text(source.get("data_timestamp"), "Unavailable", limit=48),
        "setup_type": _clean_text(plan.get("setup_type"), "Unavailable", limit=70),
        "trigger": _money(plan.get("trigger")),
        "entry_low": _money(plan.get("entry_low")),
        "entry_high": _money(plan.get("entry_high")),
        "invalidation": _money(plan.get("invalidation")),
        "target_1": _money(plan.get("target_1")),
        "target_2": _money(plan.get("target_2")),
        "stretch_target": _money(plan.get("stretch_target")),
        "reward_to_risk": (
            f"{_number(plan.get('reward_to_risk')):.2f}:1"
            if _number(plan.get("reward_to_risk")) is not None
            else "Unavailable"
        ),
        "horizon": _clean_text(plan.get("horizon"), "Unavailable", limit=60),
        "do_now": _clean_text(source.get("do_this_now"), "No entry decision was made.", limit=280),
        "primary_risk": _clean_text(source.get("primary_risk"), "Unavailable", limit=240),
        "upgrade": _clean_text(source.get("upgrade_condition"), "Unavailable", limit=240),
        "invalidate": _clean_text(source.get("invalidation_condition"), "Unavailable", limit=240),
        "reasons": [_clean_text(reason, limit=180) for reason in raw_reasons[:3]],
        "regime": _clean_text(market.get("regime"), "Unavailable", limit=32).upper(),
        "earnings": earnings_label,
        "alignment": alignment,
        "timeframe": _clean_text(timeframe, "15m", limit=8),
        "tradingview_symbol": _clean_text(tradingview_symbol, "Unavailable", limit=72),
        "tradingview_url": _clean_text(tradingview_url, "Unavailable", limit=240),
        "generated_at": timestamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "disclaimer": "Decision support only. No broker connection and no order placement.",
    }


def presentation_pdf_bytes(payload: Mapping[str, Any]) -> bytes:
    """Render a polished single-page PDF using the strict public payload."""

    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("ReportLab is required for PDF presentation export.") from exc

    buffer = BytesIO()
    page_width, page_height = letter
    pdf = canvas.Canvas(buffer, pagesize=letter, pageCompression=1)
    pdf.setTitle(f"Trading Autopilot - {_clean_text(payload.get('ticker'), 'Setup')}")
    pdf.setAuthor("Trading Autopilot")

    bg = HexColor("#07111F")
    panel = HexColor("#0D1C2E")
    panel_soft = HexColor("#10243A")
    line = HexColor("#243B53")
    text = HexColor("#E8F0FA")
    muted = HexColor("#93A7BD")
    cyan = HexColor("#38BDF8")
    green = HexColor("#34D399")
    amber = HexColor("#FBBF24")
    red = HexColor("#FB7185")
    verdict = str(payload.get("verdict") or "PASS").upper()
    accent = green if verdict == "ENTER" else amber if verdict.startswith("WAIT") else red

    pdf.setFillColor(bg)
    pdf.rect(0, 0, page_width, page_height, fill=1, stroke=0)

    def rounded_panel(x: float, y: float, width: float, height: float, *, fill=panel, stroke=line) -> None:
        pdf.setFillColor(fill)
        pdf.setStrokeColor(stroke)
        pdf.setLineWidth(0.8)
        pdf.roundRect(x, y, width, height, 10, fill=1, stroke=1)

    def draw_text(value: Any, x: float, y: float, size: float, color=text, font="Helvetica") -> None:
        pdf.setFillColor(color)
        pdf.setFont(font, size)
        pdf.drawString(x, y, _clean_text(value, "", limit=500))

    def wrap(value: Any, width: float, size: float, *, max_lines: int = 3) -> list[str]:
        words = _clean_text(value, "Unavailable", limit=500).split()
        lines: list[str] = []
        current = ""
        truncated = False
        for word in words:
            candidate = f"{current} {word}".strip()
            if stringWidth(candidate, "Helvetica", size) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                if len(lines) >= max_lines:
                    truncated = True
                    break
                current = word
        if current and len(lines) < max_lines:
            lines.append(current)
        elif current:
            truncated = True
        if truncated and lines:
            final = lines[-1]
            if stringWidth(final + "...", "Helvetica", size) <= width:
                lines[-1] = final + "..."
        return lines

    margin = 34
    draw_text("TRADING AUTOPILOT", margin, page_height - 42, 9, cyan, "Helvetica-Bold")
    draw_text(
        f"{payload.get('ticker', 'UNRESOLVED')}  /  {payload.get('exchange', 'Exchange unresolved')}",
        margin,
        page_height - 69,
        23,
        text,
        "Helvetica-Bold",
    )
    draw_text(payload.get("name"), margin, page_height - 88, 9.5, muted)
    draw_text(
        f"{payload.get('timeframe', '15m')} view  |  {payload.get('data_source', 'Unavailable')} {payload.get('data_label', 'Unavailable')}",
        page_width - 230,
        page_height - 42,
        8.5,
        muted,
    )
    draw_text(f"Earnings: {payload.get('earnings', 'Unresolved')}", page_width - 230, page_height - 56, 7.5, muted)

    rounded_panel(margin, page_height - 181, page_width - (margin * 2), 73, stroke=accent)
    verdict_size = 17 if len(verdict) > 10 else 21
    draw_text(verdict, margin + 16, page_height - 137, verdict_size, accent, "Helvetica-Bold")
    draw_text(f"{payload.get('state')}  |  {payload.get('direction')}", margin + 16, page_height - 164, 9, text, "Helvetica-Bold")
    for index, line_text in enumerate(wrap(payload.get("do_now"), 245, 8.6, max_lines=3)):
        draw_text(line_text, margin + 280, page_height - 133 - (index * 11), 8.6, muted)

    metrics_y = page_height - 255
    metric_width = (page_width - (margin * 2) - 24) / 4
    metrics = (
        ("PRICE", payload.get("current_price")),
        ("CONFIDENCE", payload.get("confidence")),
        ("GRADE", payload.get("grade")),
        ("REWARD / RISK", payload.get("reward_to_risk")),
    )
    for index, (label, value) in enumerate(metrics):
        x = margin + index * (metric_width + 8)
        rounded_panel(x, metrics_y, metric_width, 55, fill=panel_soft)
        draw_text(label, x + 11, metrics_y + 35, 7, muted, "Helvetica-Bold")
        draw_text(value, x + 11, metrics_y + 14, 13, text, "Helvetica-Bold")

    plan_y = page_height - 446
    plan_width = 245
    rounded_panel(margin, plan_y, plan_width, 174)
    draw_text("TRADE PLAN", margin + 14, plan_y + 151, 8, cyan, "Helvetica-Bold")
    levels = (
        ("STRETCH", payload.get("stretch_target"), green),
        ("TARGET 2", payload.get("target_2"), green),
        ("TARGET 1", payload.get("target_1"), green),
        ("TRIGGER", payload.get("trigger"), cyan),
        ("ENTRY", f"{payload.get('entry_low')} - {payload.get('entry_high')}", text),
        ("INVALIDATION", payload.get("invalidation"), red),
    )
    for index, (label, value, color) in enumerate(levels):
        y = plan_y + 128 - index * 22
        pdf.setStrokeColor(color)
        pdf.setLineWidth(1)
        pdf.line(margin + 14, y - 3, margin + 34, y - 3)
        draw_text(label, margin + 42, y - 6, 7, muted, "Helvetica-Bold")
        draw_text(value, margin + 132, y - 6, 8.5, color, "Helvetica-Bold")

    context_x = margin + plan_width + 12
    context_width = page_width - margin - context_x
    rounded_panel(context_x, plan_y, context_width, 174)
    draw_text("DECISION CONTEXT", context_x + 14, plan_y + 151, 8, cyan, "Helvetica-Bold")
    context_items = (
        ("PRIMARY RISK", payload.get("primary_risk"), red),
        ("UPGRADES THE SETUP", payload.get("upgrade"), green),
        ("INVALIDATES", payload.get("invalidate"), amber),
    )
    cursor_y = plan_y + 128
    for label, value, color in context_items:
        draw_text(label, context_x + 14, cursor_y, 7, color, "Helvetica-Bold")
        cursor_y -= 12
        for line_text in wrap(value, context_width - 28, 8.2, max_lines=2):
            draw_text(line_text, context_x + 14, cursor_y, 8.2, text)
            cursor_y -= 10
        cursor_y -= 8

    alignment_y = page_height - 520
    draw_text(f"TIMEFRAME ALIGNMENT  |  MARKET REGIME {payload.get('regime')}", margin, alignment_y + 44, 8, muted, "Helvetica-Bold")
    alignment_rows = payload.get("alignment") if isinstance(payload.get("alignment"), list) else []
    chip_width = (page_width - (margin * 2) - 20) / 5
    for index, row in enumerate(alignment_rows[:10]):
        direction = str(row.get("direction") or "Mixed").lower()
        chip_color = green if direction == "bullish" else red if direction == "bearish" else amber
        column = index % 5
        row_index = index // 5
        x = margin + column * (chip_width + 5)
        y = alignment_y - row_index * 39
        stroke = cyan if row.get("active") == "yes" else chip_color
        rounded_panel(x, y, chip_width, 34, fill=panel_soft, stroke=stroke)
        draw_text(row.get("timeframe"), x + 7, y + 20, 7, muted, "Helvetica-Bold")
        draw_text(row.get("direction"), x + 7, y + 8, 7.5, chip_color, "Helvetica-Bold")

    notes_y = 84
    rounded_panel(margin, notes_y, page_width - (margin * 2), 104)
    draw_text("WHY THIS DECISION", margin + 14, notes_y + 82, 8, cyan, "Helvetica-Bold")
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    cursor_y = notes_y + 62
    for index, reason in enumerate(reasons[:3], start=1):
        lines = wrap(reason, page_width - (margin * 2) - 48, 8.5, max_lines=1)
        draw_text(f"{index}.", margin + 14, cursor_y, 8.5, cyan, "Helvetica-Bold")
        draw_text(lines[0] if lines else "Unavailable", margin + 31, cursor_y, 8.5, text)
        cursor_y -= 19

    draw_text(payload.get("disclaimer"), margin, 53, 7.5, muted)
    draw_text(f"Generated {payload.get('generated_at')}", page_width - 188, 53, 7.5, muted)
    draw_text(payload.get("tradingview_symbol"), margin, 38, 7, muted)
    draw_text("tradingview.com/chart", page_width - 135, 38, 7, muted)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


__all__ = ["build_presentation_payload", "presentation_pdf_bytes"]
