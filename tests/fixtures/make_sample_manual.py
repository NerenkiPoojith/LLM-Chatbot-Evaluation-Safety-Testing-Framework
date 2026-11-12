"""Generate the test PDF manual (tests/fixtures/lumina_lw9_manual.pdf).

A real, multi-page, text-based PDF for a fictional washing machine, laid out like a
manufacturer's manual: headings, paragraphs, an error-code table and a support section.
Every fact in it is invented, so tests can check answers against known values.

    .venv\\Scripts\\python.exe tests\\fixtures\\make_sample_manual.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent / "lumina_lw9_manual.pdf"
TITLE = "Lumina LW-9 Top-Load Washing Machine - User Manual"

SECTIONS = [
    ("Safety", [
        "Unplug the washing machine before cleaning or maintenance.",
        "Never open the lid while the drum is spinning. The lid lock releases 2 minutes after the cycle ends.",
        "Do not wash items stained with petrol, oil or solvents.",
    ]),
    ("Installation", [
        "Place the LW-9 on a level floor and adjust the four feet until it does not rock.",
        "Connect the inlet hose to a cold-water tap. Water pressure must be between 0.1 MPa and 0.8 MPa.",
        "The drain hose must be between 80 cm and 110 cm above the floor.",
    ]),
    ("Programs", [
        "Normal: everyday cotton, 9 kg maximum load, about 58 minutes.",
        "Delicate: silk and wool, 2 kg maximum load, about 40 minutes.",
        "Speed Wash: lightly soiled clothes, 3 kg maximum load, 22 minutes.",
        "Tub Clean: run once every 30 washes with the drum empty to keep the tub fresh.",
    ]),
    ("Child Lock", [
        "To turn on the child lock, press and hold the Start/Pause and Spin buttons together for 4 seconds. "
        "A key symbol appears on the display. Repeat to turn it off.",
    ]),
    ("Detergent", [
        "Use high-efficiency (HE) detergent only. Use about 35 ml for a full load of normally soiled laundry.",
        "Pour liquid fabric softener into the blue dispenser, never directly onto clothes.",
    ]),
    ("Cleaning the Lint Filter", [
        "Clean the lint filter once a month. Lift the filter out of the top of the agitator, "
        "rinse it under running water and push it back until it clicks.",
    ]),
]

ERRORS = [
    ["Code", "Meaning", "What to do"],
    ["F01", "No water is entering", "Check that the tap is open and the inlet hose is not kinked."],
    ["F07", "The drain pump is blocked", "Clean the pump filter behind the lower front panel."],
    ["F12", "The lid lock has failed", "Close the lid firmly. If F12 returns, call service."],
    ["UB", "Unbalanced load", "Spread the laundry evenly and restart the cycle."],
]


def build() -> Path:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, title=TITLE, author="Lumina Appliances (fictional)",
                            leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm)
    story = [Paragraph(TITLE, styles["Title"]), Spacer(1, 12),
             Paragraph("Model LW-9. Read this manual before using your washing machine.", styles["BodyText"]),
             Spacer(1, 18)]
    for i, (heading, paragraphs) in enumerate(SECTIONS):
        story.append(Paragraph(heading, styles["Heading2"]))
        story.extend(Paragraph(text, styles["BodyText"]) for text in paragraphs)
        story.append(Spacer(1, 10))
        if i == 2:
            story.append(PageBreak())

    story += [PageBreak(), Paragraph("Error Codes", styles["Heading2"])]
    table = Table(ERRORS, colWidths=[2 * cm, 5 * cm, 9 * cm])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story += [table, Spacer(1, 18)]
    story += [Paragraph("Warranty", styles["Heading2"]),
              Paragraph("The LW-9 has a 3-year warranty on parts and labour and a 12-year warranty on the motor.",
                        styles["BodyText"]),
              Spacer(1, 10),
              Paragraph("Customer Support", styles["Heading2"]),
              Paragraph("Lumina customer support: 1-800-555-0110, Monday to Friday, 8 am to 6 pm. "
                        "Email: help@lumina.example.com.", styles["BodyText"])]
    doc.build(story)
    return OUT


if __name__ == "__main__":
    print(f"Wrote {build()}")
