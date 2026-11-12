"""Generate the Lumina LW-9 test manual in every supported format (tests/fixtures/formats/).

The content is the same fictional washer manual as lumina_lw9_manual.pdf (make_sample_manual.py),
so every format can be checked with the same questions: what F07 means, how the child lock works,
how long the motor warranty is.

    .venv\\Scripts\\python.exe tests\\fixtures\\make_format_fixtures.py            # modern formats
    .venv\\Scripts\\python.exe tests\\fixtures\\make_format_fixtures.py --legacy   # + .doc .xls .ppt

The legacy files (.doc, .xls, .ppt, and a password-protected .docx) can only be written by Microsoft Office, so --legacy drives
Word, Excel and PowerPoint through PowerShell (Windows with Office only). They are checked in, so
the tests do not need Office.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "formats"
sys.path.insert(0, str(HERE))

from make_sample_manual import ERRORS, SECTIONS, TITLE  # noqa: E402

PDF = HERE / "lumina_lw9_manual.pdf"
WARRANTY = "The LW-9 has a 3-year warranty on parts and labour and a 12-year warranty on the motor."
SUPPORT = ("Lumina customer support: 1-800-555-0110, Monday to Friday, 8 am to 6 pm. "
           "Email: help@lumina.example.com.")
INTRO = "Model LW-9. Read this manual before using your washing machine."


def all_sections():
    """(heading, paragraphs) in reading order; "Error Codes" is a table, marked by None."""
    return [*SECTIONS, ("Error Codes", None), ("Warranty", [WARRANTY]), ("Customer Support", [SUPPORT])]


# --------------------------------------------------------------------------- office formats
def make_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.core_properties.title = TITLE
    document.add_heading(TITLE, 0)
    document.add_paragraph(INTRO)
    for heading, paragraphs in all_sections():
        document.add_heading(heading, 1)
        if paragraphs is None:
            table = document.add_table(rows=0, cols=3)
            table.style = "Table Grid"
            for row in ERRORS:
                cells = table.add_row().cells
                for cell, text in zip(cells, row):
                    cell.text = text
        else:
            for text in paragraphs:
                document.add_paragraph(text)
    document.save(path)


def make_pptx(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    deck.core_properties.title = TITLE
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = TITLE
    cover.placeholders[1].text = INTRO
    for heading, paragraphs in all_sections():
        if paragraphs is None:
            slide = deck.slides.add_slide(deck.slide_layouts[5])
            slide.shapes.title.text = heading
            table = slide.shapes.add_table(len(ERRORS), 3, Inches(0.5), Inches(1.6), Inches(9), Inches(3)).table
            for r, row in enumerate(ERRORS):
                for c, text in enumerate(row):
                    table.cell(r, c).text = text
        else:
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = heading
            body = slide.placeholders[1].text_frame
            body.text = paragraphs[0]
            for text in paragraphs[1:]:
                body.add_paragraph().text = text
    deck.save(path)


def make_xlsx(path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.properties.title = TITLE
    sheet = workbook.active
    sheet.title = "Error Codes"
    for row in ERRORS:
        sheet.append(row)
    warranty = workbook.create_sheet("Warranty")
    for row in (["Part", "Warranty"], ["Parts and labour", "3 years"], ["Motor", "12 years"]):
        warranty.append(row)
    workbook.save(path)


def make_csv(path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(ERRORS)


# --------------------------------------------------------------------------- text and web formats
def plain_text() -> str:
    lines = [TITLE, "", INTRO, ""]
    for heading, paragraphs in all_sections():
        lines += [heading, ""]
        if paragraphs is None:
            lines += ["   ".join(row) for row in ERRORS]
        else:
            lines += paragraphs
        lines.append("")
    return "\n".join(lines)


def html_page() -> str:
    body = [f"<h1>{escape(TITLE)}</h1>", f"<p>{escape(INTRO)}</p>"]
    for heading, paragraphs in all_sections():
        body.append(f"<h2>{escape(heading)}</h2>")
        if paragraphs is None:
            rows = "".join("<tr>" + "".join(f"<{'th' if i == 0 else 'td'}>{escape(c)}</{'th' if i == 0 else 'td'}>"
                                              for c in row) + "</tr>" for i, row in enumerate(ERRORS))
            body.append(f"<table>{rows}</table>")
        else:
            body += [f"<p>{escape(p)}</p>" for p in paragraphs]
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>" + escape(TITLE) + "</title>"
            "<style>body{font-family:sans-serif}</style><script>var tracking = 'not manual text';</script>"
            "</head><body><nav><a href='/'>Home</a> <a href='/shop'>Shop</a></nav><main>"
            + "".join(body) + "</main></body></html>")


def make_rtf(path: Path) -> None:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    parts = [r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0\fs22 ", r"{\b\fs32 " + esc(TITLE) + r"}\par\par ",
             esc(INTRO) + r"\par\par "]
    for heading, paragraphs in all_sections():
        parts.append(r"{\b\fs26 " + esc(heading) + r"}\par ")
        if paragraphs is None:
            for row in ERRORS:
                parts.append(r"\trowd\cellx1500\cellx4500\cellx9000 " +
                             "".join(r"\intbl " + esc(c) + r"\cell " for c in row) + r"\row ")
            parts.append(r"\pard\par ")
        else:
            parts += [esc(p) + r"\par " for p in paragraphs]
            parts.append(r"\par ")
    parts.append("}")
    path.write_text("".join(parts), encoding="ascii")


def make_mhtml(path: Path) -> None:
    message = MIMEMultipart("related", type="text/html")
    message["From"] = "<Saved by Microsoft Edge>"
    message["Subject"] = TITLE
    message["MIME-Version"] = "1.0"
    message.attach(MIMEText(html_page(), "html", "utf-8"))
    path.write_bytes(message.as_bytes())


def make_epub(path: Path) -> None:
    sections = all_sections()
    chapters = [sections[:4], sections[4:]]
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")   # stored, first, as EPUB requires
        epub.writestr("META-INF/container.xml",
                      '<?xml version="1.0"?><container version="1.0" '
                      'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                      '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                      '</rootfiles></container>')
        items, refs = [], []
        for n, chapter in enumerate(chapters, start=1):
            html = []
            for heading, paragraphs in chapter:
                html.append(f"<h2>{escape(heading)}</h2>")
                if paragraphs is None:
                    html.append("<table>" + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>"
                                                    for row in ERRORS) + "</table>")
                else:
                    html += [f"<p>{escape(p)}</p>" for p in paragraphs]
            epub.writestr(f"OEBPS/ch{n}.xhtml",
                          '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                          f"<head><title>Chapter {n}</title></head><body>{''.join(html)}</body></html>")
            items.append(f'<item id="ch{n}" href="ch{n}.xhtml" media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="ch{n}"/>')
        epub.writestr("OEBPS/content.opf",
                      '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                      'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                      f'<dc:identifier id="id">lumina-lw9</dc:identifier><dc:title>{escape(TITLE)}</dc:title>'
                      f'<dc:language>en</dc:language></metadata><manifest>{"".join(items)}</manifest>'
                      f'<spine>{"".join(refs)}</spine></package>')


# --------------------------------------------------------------------------- OpenDocument and XPS
ODF_HEAD = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
            'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" office:version="1.3">'
            '<office:body>')


def _odf_table(name: str) -> str:
    rows = "".join("<table:table-row>" + "".join(
        f'<table:table-cell office:value-type="string"><text:p>{escape(c)}</text:p></table:table-cell>' for c in row)
        + "</table:table-row>" for row in ERRORS)
    return f'<table:table table:name="{name}">{rows}</table:table>'


def _write_odf(path: Path, mimetype: str, body: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as odf:
        odf.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        odf.writestr("content.xml", ODF_HEAD + body + "</office:body></office:document-content>")
        odf.writestr("meta.xml",
                     '<?xml version="1.0" encoding="UTF-8"?><office:document-meta '
                     'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                     'xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
                     f"<dc:title>{escape(TITLE)}</dc:title></office:meta></office:document-meta>")
        odf.writestr("META-INF/manifest.xml",
                     '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                     'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
                     f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mimetype}"/>'
                     '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                     "</manifest:manifest>")


def make_odt(path: Path) -> None:
    body = [f'<text:p text:style-name="Title">{escape(TITLE)}</text:p>', f"<text:p>{escape(INTRO)}</text:p>"]
    for heading, paragraphs in all_sections():
        body.append(f'<text:h text:outline-level="1">{escape(heading)}</text:h>')
        body += [_odf_table("Errors")] if paragraphs is None else [f"<text:p>{escape(p)}</text:p>" for p in paragraphs]
    _write_odf(path, "application/vnd.oasis.opendocument.text", "<office:text>" + "".join(body) + "</office:text>")


def make_ods(path: Path) -> None:
    _write_odf(path, "application/vnd.oasis.opendocument.spreadsheet",
               "<office:spreadsheet>" + _odf_table("Error Codes") + "</office:spreadsheet>")


def make_odp(path: Path) -> None:
    pages = []
    for n, (heading, paragraphs) in enumerate(all_sections(), start=1):
        content = (f"<draw:frame>{_odf_table('Errors')}</draw:frame>" if paragraphs is None else
                   "<draw:frame><draw:text-box>" + "".join(f"<text:p>{escape(p)}</text:p>" for p in paragraphs)
                   + "</draw:text-box></draw:frame>")
        pages.append(f'<draw:page draw:name="page{n}"><draw:frame presentation:class="title"><draw:text-box>'
                     f"<text:p>{escape(heading)}</text:p></draw:text-box></draw:frame>{content}</draw:page>")
    _write_odf(path, "application/vnd.oasis.opendocument.presentation",
               "<office:presentation>" + "".join(pages) + "</office:presentation>")


def make_xps(path: Path) -> None:
    lines = plain_text().splitlines()
    pages = [lines[i:i + 40] for i in range(0, len(lines), 40)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as xps:
        xps.writestr("FixedDocumentSequence.fdseq",
                     '<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">'
                     '<DocumentReference Source="Documents/1/FixedDocument.fdoc"/></FixedDocumentSequence>')
        xps.writestr("Documents/1/FixedDocument.fdoc",
                     '<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">' + "".join(
                         f'<PageContent Source="Pages/{n}.fpage"/>' for n in range(1, len(pages) + 1))
                     + "</FixedDocument>")
        for n, page in enumerate(pages, start=1):
            glyphs = "".join(f'<Glyphs OriginX="96" OriginY="{120 + 18 * i}" FontRenderingEmSize="12" '
                             f'UnicodeString="{escape(line, quote=True)}" FontUri="/font.ttf"/>'
                             for i, line in enumerate(page) if line.strip())
            xps.writestr(f"Documents/1/Pages/{n}.fpage",
                         '<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06" Width="816" Height="1056">'
                         + glyphs + "</FixedPage>")


# --------------------------------------------------------------------------- scans and photos
def page_images(scale: float = 2.0):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(PDF))
    try:
        return [document[i].render(scale=scale).to_pil().convert("RGB") for i in range(len(document))]
    finally:
        document.close()


def make_images() -> None:
    pages = page_images()
    pages[2].save(OUT / "lumina.png")                                  # error codes, warranty, support
    photo = pages[1].rotate(90, expand=True)                           # child lock page, held sideways...
    exif = photo.getexif()
    exif[0x0112] = 6                                                   # ...with EXIF "rotate 90 to view"
    photo.save(OUT / "lumina_photo.jpg", quality=85, exif=exif)
    bilevel = [page.convert("L").point(lambda v: 255 if v > 160 else 0, mode="1") for page in pages]
    bilevel[0].save(OUT / "lumina_scan.tiff", save_all=True, append_images=bilevel[1:])   # a 3-page office scan
    pages[0].save(OUT / "lumina_scanned.pdf", save_all=True, append_images=pages[1:], resolution=144)
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        pages[2].save(OUT / "lumina.heic", quality=80)
    except Exception as exc:  # the HEIC encoder is optional
        print(f"skipped lumina.heic: {exc}")


# --------------------------------------------------------------------------- legacy Office formats
LEGACY_PS = r"""
$ErrorActionPreference = 'Stop'
$out = '{out}'
$word = New-Object -ComObject Word.Application; $word.Visible = $false; $word.DisplayAlerts = 0
try {{
  $d = $word.Documents.Open("$out\lumina.docx", $false, $true)
  $d.SaveAs2("$out\lumina.doc", 0)
  $d.SaveAs2("$out\lumina_locked.docx", 16, $false, "secret")   # password-protected
  $d.Close($false)
}}
finally {{ $word.Quit() }}
$excel = New-Object -ComObject Excel.Application; $excel.Visible = $false; $excel.DisplayAlerts = $false
try {{ $b = $excel.Workbooks.Open("$out\lumina.xlsx"); $b.SaveAs("$out\lumina.xls", 56); $b.Close($false) }}
finally {{ $excel.Quit() }}
$pp = New-Object -ComObject PowerPoint.Application
try {{ $p = $pp.Presentations.Open("$out\lumina.pptx", $true, $false, $false); $p.SaveAs("$out\lumina.ppt", 1); $p.Close() }}
finally {{ $pp.Quit() }}
"""


def make_legacy() -> None:
    script = LEGACY_PS.format(out=str(OUT))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=300)


def build(legacy: bool = False) -> list[Path]:
    OUT.mkdir(exist_ok=True)
    make_docx(OUT / "lumina.docx")
    make_pptx(OUT / "lumina.pptx")
    make_xlsx(OUT / "lumina.xlsx")
    make_csv(OUT / "lumina_errors.csv")
    (OUT / "lumina.txt").write_text(plain_text(), encoding="utf-8")
    (OUT / "lumina.html").write_text(html_page(), encoding="utf-8")
    make_rtf(OUT / "lumina.rtf")
    make_mhtml(OUT / "lumina.mhtml")
    make_epub(OUT / "lumina.epub")
    make_odt(OUT / "lumina.odt")
    make_ods(OUT / "lumina.ods")
    make_odp(OUT / "lumina.odp")
    make_xps(OUT / "lumina.xps")
    make_images()
    if legacy:
        make_legacy()
    return sorted(OUT.iterdir())


if __name__ == "__main__":
    for written in build(legacy="--legacy" in sys.argv):
        print(f"{written.stat().st_size:>9,}  {written.name}")
