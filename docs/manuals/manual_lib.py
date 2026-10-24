"""Shared PDF builder for the per-role user manuals (SAMU Roster and SAMU Inventory).

Each manual is a list of blocks:
    ("h1", text) ("h2", text) ("p", text) ("bul", [text, ...]) ("steps", [text, ...])
    ("table", rows, widths) ("note", kind, text)        kind: tip | warn | ok
Text may use the reportlab mini-markup (<b>, <i>) - escape a literal ampersand as &amp;.

Requires reportlab (pip install reportlab). Same file is kept in both repositories.
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

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
        else:
            raise ValueError(kind)
    doc.build(story)
    return path
