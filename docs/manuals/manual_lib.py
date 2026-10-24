"""Shared PDF builder for the per-role user manuals (SAMU Roster and SAMU Inventory).

Each manual is a list of blocks:
    ("h1", text) ("h2", text) ("p", text) ("bul", [text, ...]) ("steps", [text, ...])
    ("table", rows, widths) ("note", kind, text)        kind: tip | warn | ok
    ("fig", image_path, title, intro, [legend step, ...])   a screenshot with numbered orange markers + matching legend
Text may use the reportlab mini-markup (<b>, <i>) - escape a literal ampersand as &amp;.

Requires reportlab (pip install reportlab). Same file is kept in both repositories.
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
import json
import os

from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.lib.utils import ImageReader

INK = colors.HexColor("#14181A"); INK2 = colors.HexColor("#4A5257")
BRAND = colors.HexColor("#1F4FC4"); TINT = colors.HexColor("#EAF0FC")
GROUND = colors.HexColor("#F4F3EF"); RULE = colors.HexColor("#CFCBBF")
OK = colors.HexColor("#1E6B3C"); OKT = colors.HexColor("#E6F2EA")
WARN = colors.HexColor("#8A5200"); WARNT = colors.HexColor("#FBF1D6")

W, H = A4
LM = RM = 20 * mm
CW = W - LM - RM

body = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=15.5, textColor=INK, spaceAfter=6, alignment=TA_LEFT)
h1 = ParagraphStyle("h1", keepWithNext=1, fontName="Helvetica-Bold", fontSize=18, leading=23, textColor=INK, spaceAfter=6)
h2 = ParagraphStyle("h2", keepWithNext=1, fontName="Helvetica-Bold", fontSize=12.5, leading=17, textColor=BRAND, spaceBefore=10, spaceAfter=3)
cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=13, spaceAfter=0)
cellb = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
cellh = ParagraphStyle("cellh", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
bullet = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=2, spaceAfter=3)
step = ParagraphStyle("step", parent=bullet, leftIndent=20, bulletIndent=0)


def _rule(th=2.0):
    t = Table([[""]], colWidths=[CW], rowHeights=[1])
    t.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), th, INK)]))
    return t


def _table(rows, widths):
    data = [[Paragraph(str(c), cellh if r == 0 else (cellb if ci == 0 else cell)) for ci, c in enumerate(row)]
            for r, row in enumerate(rows)]
    t = Table(data, colWidths=[w * CW for w in widths], repeatRows=1)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.5, RULE), ("BACKGROUND", (0, 0), (-1, 0), INK),
          ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
          ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    st += [("BACKGROUND", (0, i), (-1, i), GROUND) for i in range(2, len(rows), 2)]
    t.setStyle(TableStyle(st))
    return t


def _note(kind, text):
    fg, bg, label = {"tip": (BRAND, TINT, "TIP"), "warn": (WARN, WARNT, "IMPORTANT"), "ok": (OK, OKT, "NOTE")}[kind]
    p = Paragraph(f'<font name="Helvetica-Bold" color="{fg.hexval().replace("0x", "#")}">{label}</font>  {text}', cell)
    t = Table([[p]], colWidths=[CW])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LINEBEFORE", (0, 0), (0, -1), 4, fg),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return KeepTogether([t, Spacer(1, 8)])


MARK = colors.HexColor("#E8590C")   # the colour of the numbered markers drawn on the screenshots


def _legend(steps, width):
    rows = [[Paragraph(f'<font color="white"><b>{i}</b></font>', ParagraphStyle("n", parent=cell, alignment=1, leading=12)), Paragraph(t, cell)]
            for i, t in enumerate(steps, 1)]
    t = Table(rows, colWidths=[7 * mm, width - 7 * mm])
    st = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE)]
    st += [("BACKGROUND", (0, i), (0, i), MARK) for i in range(len(rows))]
    t.setStyle(TableStyle(st))
    return t


def _figure(path, title, intro, steps):
    """The screenshot (bordered) and its legend. Portrait phone shots sit beside their legend; wide shots span the page."""
    iw, ih = ImageReader(path).getSize()
    parts = [Paragraph(f"<b>{title}</b>", ParagraphStyle("ft", parent=body, fontSize=11, spaceAfter=2, keepWithNext=1))]
    if intro:
        parts.append(Paragraph(intro, ParagraphStyle("fi", parent=body, fontSize=9.5, leading=13.5, textColor=INK2, spaceAfter=4, keepWithNext=1)))
    if ih / iw > 1.2:   # phone
        w = 58 * mm; h = w * ih / iw
        img = Table([[Image(path, width=w, height=h)]], colWidths=[w + 2])
        img.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, INK), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        side = Table([[img, _legend(steps, CW - w - 8 * mm)]], colWidths=[w + 8 * mm, CW - w - 8 * mm])
        side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        parts.append(side)
    else:
        h = CW * ih / iw
        img = Table([[Image(path, width=CW, height=h)]], colWidths=[CW])
        img.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, INK), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        parts += [img, Spacer(1, 4), _legend(steps, CW)]
    return KeepTogether(parts + [Spacer(1, 12)])


def load_figures(figures_json):
    """{(role, key): block} from the capture script's figures.json; images are resolved next to that file."""
    base = os.path.dirname(os.path.abspath(figures_json))
    out = {}
    for f in json.load(open(figures_json, encoding="utf-8")):
        path = os.path.join(base, f["file"])
        if os.path.isfile(path):
            out[(f["role"], f["key"])] = ("fig", path, f["title"], f.get("intro", ""), f.get("steps", []))
    return out


def figs(figures, role, *keys):
    """Blocks for the named figures of a role (silently skipping any that were not captured, with a warning)."""
    blocks = []
    for k in keys:
        b = figures.get((role, k)) or figures.get(("common", k))
        if b is None:
            print(f"  warning: no screenshot {role}/{k}")
        else:
            blocks.append(b)
    return blocks


def build_manual(path, app_name, role_title, intro, blocks, version="Version 1  -  24 October 2026"):
    def on_page(c, doc):
        c.saveState()
        c.setStrokeColor(RULE); c.setLineWidth(0.6); c.line(LM, 14 * mm, W - RM, 14 * mm)
        c.setFont("Helvetica", 8.5); c.setFillColor(INK2)
        c.drawString(LM, 9.5 * mm, f"{app_name} - User manual - {role_title}")
        c.drawRightString(W - RM, 9.5 * mm, f"Page {doc.page}")
        c.restoreState()

    def on_cover(c, doc):
        c.saveState()
        c.setFillColor(INK); c.rect(0, H - 105 * mm, W, 105 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 11); c.drawString(LM, H - 28 * mm, "SAMU  -  Rwanda Biomedical Centre")
        c.setFont("Helvetica-Bold", 32); c.drawString(LM, H - 52 * mm, app_name)
        c.setFont("Helvetica-Bold", 20); c.drawString(LM, H - 66 * mm, "User manual")
        c.setFont("Helvetica", 15); c.drawString(LM, H - 80 * mm, f"For the {role_title}")
        c.setFillColor(INK2); c.setFont("Helvetica", 9.5)
        c.drawString(LM, 14 * mm, f"{version}  -  This manual covers only what this role does.")
        c.restoreState()

    doc = BaseDocTemplate(path, pagesize=A4, leftMargin=LM, rightMargin=RM, topMargin=18 * mm, bottomMargin=20 * mm,
                          title=f"{app_name} - User manual - {role_title}", author="SAMU", subject=f"User manual for the {role_title}")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[Frame(LM, 20 * mm, CW, H - 130 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], onPage=on_cover),
        PageTemplate(id="page", frames=[Frame(LM, 20 * mm, CW, H - 38 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], onPage=on_page),
    ])

    chapters = [b[1] for b in blocks if b[0] == "h1"]
    story = [Paragraph("<b>About this manual</b>", ParagraphStyle("ab", parent=body, fontSize=12)), Paragraph(intro, body),
             Spacer(1, 6), Paragraph("<b>In this manual</b>", ParagraphStyle("ab2", parent=body, fontSize=12))]
    story += [Paragraph(c, step, bulletText=f"{i}.") for i, c in enumerate(chapters, 1)]
    story += [NextPageTemplate("page"), PageBreak()]
    first = True
    for blk in blocks:
        kind = blk[0]
        if kind == "h1":
            if not first:
                story.append(CondPageBreak(70 * mm))
            first = False
            story += [Paragraph(blk[1], h1), _rule(), Spacer(1, 8)]
        elif kind == "h2":
            story.append(Paragraph(blk[1], h2))
        elif kind == "p":
            story.append(Paragraph(blk[1], body))
        elif kind == "bul":
            story += [Paragraph(t, bullet, bulletText="•") for t in blk[1]] + [Spacer(1, 3)]
        elif kind == "steps":
            story += [Paragraph(t, step, bulletText=f"{i}.") for i, t in enumerate(blk[1], 1)] + [Spacer(1, 3)]
        elif kind == "table":
            story += [_table(blk[1], blk[2]), Spacer(1, 9)]
        elif kind == "note":
            story.append(_note(blk[1], blk[2]))
        elif kind == "fig":
            story.append(_figure(blk[1], blk[2], blk[3], blk[4]))
        else:
            raise ValueError(kind)
    doc.build(story)
    return path
