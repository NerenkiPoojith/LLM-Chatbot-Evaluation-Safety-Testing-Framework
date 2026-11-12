"""Small helpers on top of reportlab used by build_guide.py.

Content is written with a tiny markup:  **bold**,  `inline code`,  ~~italic~~.
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.fonts import addMapping
from reportlab.platypus.flowables import Flowable, PageBreakIfNotEmpty

FONTS = Path("C:/Windows/Fonts")
pdfmetrics.registerFont(TTFont("Body", str(FONTS / "calibri.ttf")))
pdfmetrics.registerFont(TTFont("Body-Bold", str(FONTS / "calibrib.ttf")))
pdfmetrics.registerFont(TTFont("Body-Italic", str(FONTS / "calibrii.ttf")))
pdfmetrics.registerFont(TTFont("Body-BoldItalic", str(FONTS / "calibriz.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONTS / "consola.ttf")))
pdfmetrics.registerFont(TTFont("Mono-Bold", str(FONTS / "consolab.ttf")))
addMapping("Body", 0, 0, "Body")
addMapping("Body", 1, 0, "Body-Bold")
addMapping("Body", 0, 1, "Body-Italic")
addMapping("Body", 1, 1, "Body-BoldItalic")
addMapping("Mono", 0, 0, "Mono")
addMapping("Mono", 1, 0, "Mono-Bold")
addMapping("Mono", 0, 1, "Mono")
addMapping("Mono", 1, 1, "Mono-Bold")

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#5b6675")
ACCENT = colors.HexColor("#1d4ed8")
ACCENT_LIGHT = colors.HexColor("#dbe7ff")
CODE_BG = colors.HexColor("#f4f5f7")
RULE = colors.HexColor("#d9dee5")
NOTE_BG = {"info": colors.HexColor("#eef4ff"), "warn": colors.HexColor("#fff6e5"), "ok": colors.HexColor("#eafaf1")}
NOTE_BAR = {"info": ACCENT, "warn": colors.HexColor("#d97706"), "ok": colors.HexColor("#15803d")}
BOX_FILLS = {
    "blue": colors.HexColor("#dbe7ff"),
    "green": colors.HexColor("#dcf5e7"),
    "amber": colors.HexColor("#fdeccc"),
    "red": colors.HexColor("#fde0dd"),
    "grey": colors.HexColor("#eceff3"),
    "purple": colors.HexColor("#ebe3fb"),
}

PAGE_W, PAGE_H = A4
MARGIN = 2 * cm
CONTENT_W = PAGE_W - 2 * MARGIN

S = {
    "title": ParagraphStyle("Title", fontName="Body-Bold", fontSize=30, leading=36, textColor=INK, spaceAfter=10),
    "subtitle": ParagraphStyle("Subtitle", fontName="Body", fontSize=15, leading=20, textColor=MUTED, spaceAfter=24),
    # keepWithNext: a heading always moves to the next page together with what follows it.
    "H1": ParagraphStyle("H1", fontName="Body-Bold", fontSize=21, leading=26, textColor=ACCENT, spaceBefore=4,
                         spaceAfter=12, keepWithNext=1),
    "H2": ParagraphStyle("H2", fontName="Body-Bold", fontSize=14.5, leading=19, textColor=INK, spaceBefore=14,
                         spaceAfter=6, keepWithNext=1),
    "H3": ParagraphStyle("H3", fontName="Body-Bold", fontSize=12, leading=16, textColor=INK, spaceBefore=10,
                         spaceAfter=4, keepWithNext=1),
    "body": ParagraphStyle("Body", fontName="Body", fontSize=11, leading=15.5, textColor=INK, spaceAfter=7),
    "bullet": ParagraphStyle("Bullet", fontName="Body", fontSize=11, leading=15, textColor=INK, leftIndent=16,
                             bulletIndent=4, spaceAfter=3),
    "cell": ParagraphStyle("Cell", fontName="Body", fontSize=9.5, leading=12.5, textColor=INK),
    "cellhead": ParagraphStyle("CellHead", fontName="Body-Bold", fontSize=9.5, leading=12.5, textColor=INK),
    "code": ParagraphStyle("Code", fontName="Mono", fontSize=8.2, leading=10.6, textColor=INK),
    "caption": ParagraphStyle("Caption", fontName="Body-Italic", fontSize=9.5, leading=12, textColor=MUTED,
                              alignment=TA_CENTER, spaceBefore=3, spaceAfter=10),
    "note": ParagraphStyle("Note", fontName="Body", fontSize=10.5, leading=14.5, textColor=INK),
    "toc0": ParagraphStyle("TOC0", fontName="Body-Bold", fontSize=11.5, leading=17, leftIndent=0),
    "toc1": ParagraphStyle("TOC1", fontName="Body", fontSize=10, leading=13.5, leftIndent=18, textColor=MUTED),
    "small": ParagraphStyle("Small", fontName="Body", fontSize=9.5, leading=13, textColor=MUTED),
}


def _code_span(match: re.Match) -> str:
    code = match.group(1)
    if len(code) <= 32:
        # Short code (a placeholder, a key/value pair, a sample row) must never
        # wrap in the middle; non-breaking spaces keep it on one line.
        code = code.replace(" ", " ")
    return f'<font name="Mono" size="9.3" color="#9a3412">{code}</font>'


def fmt(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"`([^`]+)`", _code_span, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<i>\1</i>", text)
    text = re.sub(r"([≥≤≈]) ", "\\1\u00a0", text)  # never leave "≥" alone at the end of a line
    return text.replace("\n", "<br/>")  # explicit line break, e.g. a long file name in a table cell


# ---------------------------------------------------------------- flowables
def H1(text):
    # Every chapter starts on a new page, but never leaves a blank page behind
    # (a plain page break right after another one would).
    return [PageBreakIfNotEmpty(), Paragraph(fmt(text), S["H1"])]


def H2(text):
    # No forced break: keepWithNext (in the style) already moves a heading together with its content.
    return [Paragraph(fmt(text), S["H2"])]


def H3(text):
    return [Paragraph(fmt(text), S["H3"])]


class Gap(Flowable):
    """Vertical space after a block that shrinks to fit instead of spilling onto the next page.

    A plain Spacer at the very bottom of a page moves to the next page; the chapter
    break that follows then sees a "non-empty" page and leaves it blank.
    """

    def __init__(self, height):
        super().__init__()
        self.gap = height

    def wrap(self, availWidth, availHeight):
        return 0, max(0.0, min(self.gap, availHeight))

    def draw(self):
        pass


BODY_LEADS_IN = ParagraphStyle("BodyLeadIn", parent=S["body"], keepWithNext=1)


def P(*paragraphs):
    # A paragraph ending in ":" introduces what follows (a table, code, list): keep them on one page.
    return [Paragraph(fmt(p), BODY_LEADS_IN if p.rstrip().endswith(":") else S["body"]) for p in paragraphs]


def BUL(items, numbered=False):
    out = []
    for i, item in enumerate(items, start=1):
        out.append(Paragraph(fmt(item), S["bullet"], bulletText=f"{i}." if numbered else "•"))
    out.append(Gap(5))
    return out


def CODE(text, title=None):
    text = text.strip("\n")
    pre = Preformatted(text, S["code"])
    # Title and code share ONE table cell: a one-row table cannot split across pages, so the title
    # never strands, and (unlike KeepTogether) it can still be glued to a keepWithNext heading.
    cell = [pre]
    if title:
        cell = [Paragraph(f'<font name="Body-Bold" size="8.5" color="#5b6675">{fmt(title)}</font>', S["cell"]),
                Spacer(1, 6), pre]
    t = Table([[cell]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [t, Gap(8)]


def NOTE(text, kind="info", title=None):
    body = (f"<b>{fmt(title)}</b><br/>" if title else "") + fmt(text)
    t = Table([[Paragraph(body, S["note"])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG[kind]),
        ("LINEBEFORE", (0, 0), (0, -1), 3, NOTE_BAR[kind]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return [t, Gap(9)]


def TABLE(rows, widths, header=True, font_size=None):
    style_cell = S["cell"] if not font_size else ParagraphStyle("c", parent=S["cell"], fontSize=font_size, leading=font_size + 3)
    style_head = S["cellhead"] if not font_size else ParagraphStyle("h", parent=S["cellhead"], fontSize=font_size, leading=font_size + 3)
    data = []
    for r, row in enumerate(rows):
        data.append([Paragraph(fmt(str(c)), style_head if (header and r == 0) else style_cell) for c in row])
    total = sum(widths)
    t = Table(data, colWidths=[w / total * CONTENT_W for w in widths], repeatRows=1 if header else 0)
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    if header:
        cmds.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")))
    for r in range(1 if header else 0, len(rows)):
        if r % 2 == 0:
            cmds.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#fafbfc")))
    t.setStyle(TableStyle(cmds))
    return [t, Gap(10)]


def CAPTION(text):
    return [Paragraph(fmt(text), S["caption"])]


# ---------------------------------------------------------------- diagrams
def _box(d, x, y, w, h, text, fill="blue", size=8.6, bold_first=True):
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=BOX_FILLS[fill], strokeColor=colors.HexColor("#9aa5b1"), strokeWidth=0.7))
    lines = text.split("\n")
    line_h = size + 2.4
    top = y + h / 2 + (len(lines) - 1) * line_h / 2 - size / 3
    for i, line in enumerate(lines):
        font = "Body-Bold" if (i == 0 and bold_first) else "Body"
        d.add(String(x + w / 2, top - i * line_h, line, fontName=font, fontSize=size, fillColor=INK, textAnchor="middle"))


def _arrow(d, x1, y1, x2, y2, color=MUTED):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1.1))
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    size = 5
    p1 = (x2 - size * math.cos(ang - 0.45), y2 - size * math.sin(ang - 0.45))
    p2 = (x2 - size * math.cos(ang + 0.45), y2 - size * math.sin(ang + 0.45))
    d.add(Polygon([x2, y2, p1[0], p1[1], p2[0], p2[1]], fillColor=color, strokeColor=color, strokeWidth=0.5))


def row_diagram(boxes, height=62, gap=16, size=8.4):
    """Boxes left-to-right with arrows. boxes = [(text, fill), ...]"""
    n = len(boxes)
    w = (CONTENT_W - gap * (n - 1)) / n
    d = Drawing(CONTENT_W, height + 6)
    for i, (text, fill) in enumerate(boxes):
        x = i * (w + gap)
        _box(d, x, 3, w, height, text, fill, size)
        if i < n - 1:
            _arrow(d, x + w + 2, 3 + height / 2, x + w + gap - 2, 3 + height / 2)
    return [d, Gap(4)]


def architecture_diagram():
    """Request flow (left column) and start-up ingestion (right column).

    Layout rules: the vector-store box sits at exactly the height of the
    Retrieve box so the green arrow between them is horizontal, and the blue
    response line has its own lane with its label below the ingestion column,
    so no text touches a line.
    """
    W, H = CONTENT_W, 412
    d = Drawing(W, H)
    main_x, main_w, bh = 0, 232, 38
    lane_x = main_x + main_w + 18           # blue response line
    side_x = lane_x + 64                    # leaves room for the response label
    side_w, sh = W - side_x, 40
    ys = [350, 296, 242, 188, 134, 80, 20]  # bottom edge of each main box
    labels = [
        ("Client\nSwagger UI · Postman · pytest + requests", "grey"),
        ("FastAPI  POST /v1/chat\nPydantic validation (bad input → 422)", "blue"),
        ("1. Input guard\nblock injection / jailbreak patterns", "red"),
        ("2. Retrieve\ntop-k most similar chunks (cosine)", "green"),
        ("3. Context guard\ndrop injected or PII-dense chunks", "red"),
        ("4. Generate\nClaude (or offline extractive mock)", "purple"),
        ("5. Output guard\ncanary · unknown links · PII redaction", "red"),
    ]
    for (text, fill), y in zip(labels, ys):
        _box(d, main_x, y, main_w, bh, text, fill)
    for a, b in zip(ys, ys[1:]):
        _arrow(d, main_x + main_w / 2, a - 1, main_x + main_w / 2, b + bh + 1)

    # Response travels back up to the client in its own lane.
    bottom_mid, top_mid = ys[-1] + bh / 2, ys[0] + bh / 2
    d.add(Line(main_x + main_w + 2, bottom_mid, lane_x, bottom_mid, strokeColor=ACCENT, strokeWidth=1.1))
    d.add(Line(lane_x, bottom_mid, lane_x, top_mid, strokeColor=ACCENT, strokeWidth=1.1))
    _arrow(d, lane_x, top_mid, main_x + main_w + 2, top_mid, ACCENT)
    for i, (text, font) in enumerate([("Response", "Body-Bold"), ("answer", "Body"), ("+ sources", "Body"),
                                      ("+ guardrail", "Body"), ("details", "Body")]):
        d.add(String(lane_x + 6, 96 - i * 11, text, fontName=font, fontSize=8, fillColor=ACCENT))

    # Ingestion column, aligned so the vector store sits level with Retrieve.
    retrieve_mid = ys[3] + bh / 2
    step = sh + 16
    side_ys = [retrieve_mid - sh / 2 + k * step for k in (3, 2, 1, 0)]
    d.add(String(side_x + side_w / 2, side_ys[0] + sh + 8, "At start-up (once)", fontName="Body-Bold", fontSize=9,
                 fillColor=MUTED, textAnchor="middle"))
    side = [
        ("data/manuals/*.md | *.pdf\n5 manuals + 2 risk documents", "grey"),
        ("Clean + chunk\ntables → lines · 700-char chunks", "amber"),
        ("Embed (fastembed)\nbge-small-en-v1.5 → 384 numbers", "amber"),
        ("Vector store\nInMemoryVectorStore (61 chunks)", "green"),
    ]
    for y, (text, fill) in zip(side_ys, side):
        _box(d, side_x, y, side_w, sh, text, fill)
    for a, b in zip(side_ys, side_ys[1:]):
        _arrow(d, side_x + side_w / 2, a - 1, side_x + side_w / 2, b + sh + 1)
    green = colors.HexColor("#15803d")
    # Line "hop": a white gap in the blue line shows the green arrow passes over it, not into it.
    d.add(Line(lane_x, retrieve_mid - 5, lane_x, retrieve_mid + 5, strokeColor=colors.white, strokeWidth=3))
    _arrow(d, side_x - 1, retrieve_mid, main_x + main_w + 2, retrieve_mid, green)
    d.add(String((lane_x + side_x) / 2 + 4, retrieve_mid + 5, "search", fontName="Body", fontSize=7.5,
                 fillColor=green, textAnchor="middle"))
    return [d, Gap(2)]


# ---------------------------------------------------------------- document
class GuideDoc(BaseDocTemplate):
    def __init__(self, filename, title):
        super().__init__(filename, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                         bottomMargin=MARGIN, title=title, author="LLM Chatbot Eval project")
        frame = Frame(MARGIN, MARGIN, CONTENT_W, PAGE_H - 2 * MARGIN, id="f", leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.doc_title = title
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame], onPage=self._cover),
            PageTemplate(id="body", frames=[frame], onPage=self._body),
        ])
        self._heading_no = 0

    def beforeDocument(self):
        self._heading_no = 0  # multiBuild runs several passes; keep bookmark keys identical in each

    def _cover(self, canv, doc):
        canv.saveState()
        canv.setFillColor(ACCENT)
        canv.rect(0, PAGE_H - 1.2 * cm, PAGE_W, 1.2 * cm, fill=1, stroke=0)
        canv.restoreState()

    def _body(self, canv, doc):
        canv.saveState()
        canv.setStrokeColor(RULE)
        canv.line(MARGIN, 1.4 * cm, PAGE_W - MARGIN, 1.4 * cm)
        canv.setFont("Body", 8.5)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN, 1.0 * cm, self.doc_title)
        canv.drawRightString(PAGE_W - MARGIN, 1.0 * cm, f"Page {doc.page}")
        canv.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in {"H1", "H2"}:
            level = 0 if flowable.style.name == "H1" else 1
            text = flowable.getPlainText()
            self._heading_no += 1
            key = f"h{self._heading_no}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))
