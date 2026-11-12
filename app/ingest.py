"""Load the manuals, split them into chunks and index them in a vector store.

Pipeline:  files --> clean text --> split by heading --> split by size
           --> add a context header to each chunk --> embed --> vector store

Why chunk at all? An LLM prompt has limited room, and retrieval works best on
small focused passages. A chunk of around 700 characters usually holds one topic
(for example, one error-code table), so retrieving it brings in the relevant facts
and little noise.

Every chunk carries a `manual_id`, so a question can be answered from one manual
only (the chat page uses this after a user drops in their own manual).

Manuals can come in almost any document format (app/formats.py turns each into text);
PDFs are read here, and their scanned pages go through OCR.
"""

from __future__ import annotations

import hashlib
import io
import re
import statistics
from collections import defaultdict
from pathlib import Path, PurePosixPath

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.formats import (  # noqa: F401  (ManualError and TOO_MUCH_TEXT are re-exported: callers import them from here)
    KNOWN_SUFFIXES,
    LABELS,
    MAX_MANUAL_CHARS,
    OCR_MAX_PAGES,
    TOO_MUCH_TEXT,
    ManualError,
    Progress,
    convert,
    ocr_pdf_pages,
)

HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
SUPPORTED_SUFFIXES = KNOWN_SUFFIXES   # for picking manuals out of a folder; uploads are recognised by content
MAX_PDF_PAGES = 400
# Pages are parsed even when they give no text (drawing commands only), so the work is capped too:
# about 15 times the content of a realistic 400-page manual.
MAX_PDF_CONTENT_BYTES = 50_000_000
MAX_FILENAME_CHARS = 120
PASSWORD_PROTECTED = "This PDF is password-protected. Remove the password and upload it again."


def linearize_markdown_tables(text: str, limit: int | None = None) -> str:
    """Turn each table row into a self-describing line.

    | Code | Meaning              |        Code: E21; Meaning: Water is not draining
    |------|----------------------|  -->
    | E21  | Water is not draining|

    A chunk boundary can separate a row from its header row, which leaves bare
    cells like "| E21 | Water is not draining |". Repeating the column names in
    every row keeps each line meaningful for both the retriever and the LLM.

    Repeating long column names can multiply a wide table's size many times, so `limit`
    is checked as the text grows, not only before.
    """
    lines = text.splitlines()
    out: list[str] = []
    size = 0

    def add(line: str) -> None:
        nonlocal size
        out.append(line)
        size += len(line) + 1
        if limit is not None and size > limit:
            raise ManualError(TOO_MUCH_TEXT, status=413)

    i = 0
    while i < len(lines):
        is_table_start = (
            lines[i].strip().startswith("|") and i + 1 < len(lines) and TABLE_SEPARATOR_RE.match(lines[i + 1].strip())
        )
        if not is_table_start:
            add(lines[i])
            i += 1
            continue
        headers = [h.strip() for h in lines[i].strip().strip("|").split("|")]
        i += 2
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            add("- " + "; ".join(f"{h}: {c}" for h, c in zip(headers, cells)))
            i += 1
    return "\n".join(out)


# A section number may come first ("3.2 Installation", "1 Safety"), never a numbered step ("1. Open the
# tap"); then a letter of any script ("Sécurité"), which _looks_like_heading wants in upper case.
PDF_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)+\s+|\d+\s+(?=[^\W\d_]))?[^\W\d_][\w/&()'’\- ]{1,60}$")
COLUMN_GAP_RE = re.compile(r"(?<=\S) {3,}(?=\S)")  # a run of 3+ spaces between two pieces of text
# Titles that PDF tools fill in by themselves; they say nothing about the product.
GENERATOR_TITLE_RE = re.compile(r"^(untitled\b|microsoft (word|powerpoint|excel) - )|\.(docx?|pptx?|indd|pdf)$", re.I)


def _pdf_page_text(page) -> str:
    """Text of one PDF page with table rows kept together.

    Plain extraction puts every table cell on its own line, which separates an error
    code from its meaning. Layout mode keeps each row on one line; runs of 3+ spaces
    between columns are turned into " | " so a row reads "F07 | Drain pump blocked | ...".
    A page set in two text columns is read one column after the other instead, because
    joining its rows would mix two unrelated topics on every line.
    """
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # layout mode is newer and stricter; fall back to plain text
        text = page.extract_text() or ""
    lines = text.splitlines()
    offset = _right_column_offset(lines)
    if offset is not None:
        lines = _read_columns(lines, offset)
    return "\n".join(COLUMN_GAP_RE.sub(" | ", line.strip()) for line in lines).strip()


def _right_column_offset(lines: list[str]) -> int | None:
    """Where the right-hand column starts, if layout mode shows a two-column page.

    Rows of a two-column page have one wide gap that ends at the same character offset,
    near the middle of the page, with prose on its left. Table rows look different:
    several gaps per row, or short cells such as "F07" on the left.
    """
    gapped = [(line, gaps) for line in lines if (gaps := list(COLUMN_GAP_RE.finditer(line)))]
    single = [(line, gaps[0]) for line, gaps in gapped if len(gaps) == 1]
    if len(single) < 3:
        return None
    ends = [gap.end() for _, gap in single]
    offset = max(ends, key=lambda end: sum(abs(e - end) <= 3 for e in ends))
    left_parts = [line[: gap.start()].strip() for line, gap in single if abs(gap.end() - offset) <= 3]
    width = max(len(line.rstrip()) for line in lines)
    if len(left_parts) * 2 <= len(gapped) or not 0.35 <= offset / width <= 0.65:
        return None
    if statistics.median(len(part) for part in left_parts) < 30:
        return None
    # Text columns wrap sentences, so many left-hand lines start in lower case; the cells of a
    # two-column table ("Problem | What to do") each start a new sentence with a capital.
    return offset if sum(part[:1].islower() for part in left_parts) * 3 >= len(left_parts) else None


def _read_columns(lines: list[str], offset: int) -> list[str]:
    """All left-column lines, then all right-column lines. A row that is empty on one side
    becomes a blank line on that side, so each column's headings still follow a blank line."""
    left, right = [], []
    for line in lines:
        split = next((gap for gap in COLUMN_GAP_RE.finditer(line) if abs(gap.end() - offset) <= 3), None)
        if split:
            left.append(line[: split.start()])
            right.append(line[split.end() :])
        elif line.strip() and len(line) - len(line.lstrip()) >= offset - 3:
            left.append("")
            right.append(line)
        else:
            left.append(line)
            right.append("")
    return left + [""] + right


def _looks_like_heading(line: str) -> bool:
    match = PDF_HEADING_RE.match(line)
    return (
        match is not None and len(line.split()) <= 6 and not line.endswith((".", ":", ","))
        and "|" not in line and line[len(match.group(1) or ""):][:1].isupper()
    )


BARE_NUMBER_RE = re.compile(r"^(\d+)\s+")
# "12 Months", "60 cm", "230 Volt", "3 May 2024": a number followed by a unit or a month is a value, not
# a chapter. Months are whole words ("1 Mains connection" is a chapter); one- and two-letter units that
# are also words ("a", "in", "m") count only when nothing but the unit follows ("16 A", "40 in").
UNIT_OR_MONTH_RE = re.compile(
    r"^\d+\s+(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|"
    r"nov(ember)?|dec(ember)?)\b|"
    r"^\d+\s+(mm|cm|km|inch(es)?|ft|kg|lbs?|oz|ml|kw|kwh|ma|hz|bar|psi|rpm|°\w*|volts?|watts?|amps?|"
    r"sec(onds?)?|min(utes?)?|hours?|days?|weeks?|months?|years?|percent)\b|"
    r"^\d+\s+(a|g|h|in|l|m|v|w)\.?$", re.I)


def _numbered_heading_ok(filled: list[str], k: int) -> bool:
    """A heading numbered without a full stop ("1 Safety") is a chapter only when it is not a value
    ("12 Months") and not one of a run of steps: "2 Remove the filter" right after "1 ..." or right before
    "3 ..." (wrapped step text is already joined into one line). A chapter has text between its number
    and the next. `filled` are the page's non-empty lines; `k` is this line's index."""
    match = BARE_NUMBER_RE.match(filled[k])
    if not match:
        return True
    if UNIT_OR_MONTH_RE.match(filled[k]):
        return False
    number = int(match.group(1))
    # Only steps numbered the same way ("2 Remove") count: a chapter may open with, or follow, a "1." list.
    neighbours = filled[max(0, k - 1):k] + filled[k + 1:k + 2]
    return not any(re.match(rf"^({number - 1}|{number + 1})\s", line) for line in neighbours)


def _pdf_sections(text: str, page_number: int) -> list[tuple[str, str]]:
    """Split a page into (section label, text) using short title-like lines after a blank
    line as headings, e.g. "Error Codes (page 3)". Falls back to "Page N".

    A heading followed straight away by another heading (often the product name on a cover)
    becomes the first line of the next section, so the index still contains it.
    """
    sections: list[tuple[str, str]] = []
    current, heading, buffer, previous_blank = f"Page {page_number}", None, [], True
    lines = [line.strip() for line in text.splitlines()]
    filled = [s for s in lines if s]
    position = {}                                   # line index -> index among the non-empty lines
    for i, s in enumerate(lines):
        if s:
            position[i] = len(position)
    for i, stripped in enumerate(lines):
        if previous_blank and _looks_like_heading(stripped) and _numbered_heading_ok(filled, position[i]):
            body = "\n".join(buffer).strip()
            if body:
                sections.append((current, body))
            current, buffer = f"{stripped} (page {page_number})", ([heading] if heading and not body else [])
            heading = stripped
        else:
            buffer.append(stripped)
        previous_blank = not stripped
    if "\n".join(buffer).strip():
        sections.append((current, "\n".join(buffer).strip()))
    return sections


# First lines of page 1 that say nothing about the product, so they never become a manual's title.
# Typeset PDFs write "OWNER’S MANUAL" with a curly apostrophe.
FRONT_MATTER_RE = re.compile(
    r"^((table of )?contents|index|english|deutsch|fran[cç]ais|espa[nñ]ol|warning|caution|"
    r"(important )?safety( instructions| information)?|use (and|&) care (guide|manual)|"
    r"installation (manual|guide|instructions)|quick (start|reference) (guide|manual|instructions)|"
    r"(user|owner[’']?s|instruction|operating|service)[’']?s? (manual|guide|instructions)"
    r"( (and|&) installation (instructions|guide))?|instructions( for use)?|read (this|these) (first|instructions))$",
    re.I)


def _cover_line(text: str) -> str | None:
    """The first line of a page when it looks like a heading, e.g. the product name on page 1.
    A brand logo read as text ("LG") is too short to name a manual, and a numbered heading
    ("1 Safety") is a section, not a name."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    long_enough = len(first) >= 8 or len(first.split()) >= 2
    return first if (long_enough and _looks_like_heading(first) and not first[:1].isdigit()
                     and not FRONT_MATTER_RE.match(first)) else None


WRAPPED_END_RE = re.compile(r"[.!?:;]$")
# Lettered and roman-numbered items ("a)", "iv.") and the bullets that Word-made PDFs keep as text ("o", "•")
LIST_MARKER_RE = re.compile(r"^([a-z]{1,2}[.)]|[ivx]{1,4}[.)]|[•▪◦·-])\s")   # "o " bullets: see join_wrapped_lines


def join_wrapped_lines(text: str) -> str:
    """Join lines that a page layout wrapped in mid-sentence: a line continues the one before when it
    starts in lower case and the one before has no closing punctuation ("replace the filter" +
    "approximately every 6 months"). A hyphen that splits a word at the line end is removed.
    Table rows (" | "), list items and blank lines are left alone, so rows and sections stay as they were."""
    out: list[str] = []
    raw = [line.strip() for line in text.splitlines()]
    for i, stripped in enumerate(raw):
        previous = out[-1] if out else ""
        # "o " is a bullet in PDFs made from Word only within a run of them; elsewhere it is a word
        # ("o" means "or" in Spanish, Italian and Portuguese) and the line is a wrapped one.
        # A bullet's text starts with a capital ("o Inlet hose", even when it wraps); "o tambor" is a word.
        o_bullet = stripped.startswith("o ") and (stripped[2:3].isupper() or any(
            raw[j].startswith("o ") for j in (i - 1, i + 1) if 0 <= j < len(raw)))
        continues = (
            stripped[:1].islower() and previous and " | " not in previous and " | " not in stripped
            and not WRAPPED_END_RE.search(previous) and not LIST_MARKER_RE.match(stripped) and not o_bullet
        )
        if continues and re.search(r"[a-z]-$", previous):
            out[-1] = previous[:-1] + stripped
        elif continues:
            out[-1] = f"{previous} {stripped}"
        else:
            out.append(stripped)
    return "\n".join(out)


def _usable_pdf_title(pdf_title: str, filename: str) -> bool:
    return (
        len(pdf_title) > 2 and not GENERATOR_TITLE_RE.search(pdf_title)
        and pdf_title.lower() != PurePosixPath(filename).stem.lower()
    )


def _first_heading(text: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    return match.group(1) if match else None


MODEL_NUMBER_RE = re.compile(r"[A-Za-z]{1,5}-?\d[\w-]*")   # "manual2" stays a word: at most 5 letters first


def _name_words(filename: str) -> list[str]:
    """The words of a file name: split at underscores and spaces, and at a hyphen before a word
    ("dw80-manual"), but not inside a model number ("LW-9")."""
    return [w for w in re.split(r"[_\s]+|-(?=[A-Za-z]{3,})", Path(filename).stem.strip()) if w]


# Names that cameras, phones and scanners give files by themselves: not model numbers.
DEVICE_NAME_RE = re.compile(
    r"(scan|img|imgp|dsc|dscf|dscn|dcim|cimg|pict|gopr|hpim|mvimg|pano|sam|doc|book|file|page|image|photo|pic|pxl|p)"
    r"[-_]?\d+", re.I)


def is_model_number_name(filename: str) -> bool:
    """True for a file named by a model number alone, such as "LFXS26973S.pdf" (not "IMG_2041.jpg")."""
    words = _name_words(filename)
    stem = Path(filename).stem.strip()
    return (len(words) == 1 and MODEL_NUMBER_RE.fullmatch(words[0]) is not None
            and DEVICE_NAME_RE.fullmatch(stem) is None)


def pretty_name(filename: str) -> str:
    """A readable name from a file name: "washer_manual.pdf" -> "Washer manual". A model number
    such as "lfxs24623.pdf" is shown as one, in capitals: "LFXS24623", and "wm7000_user_guide.pdf"
    becomes "WM7000 user guide"."""
    words = [w.upper() if MODEL_NUMBER_RE.fullmatch(w) else w.replace("-", " ") for w in _name_words(filename)]
    name = " ".join(" ".join(words).split())
    return name[:1].upper() + name[1:] if name else "Uploaded manual"


def clean_filename(filename: str) -> str:
    """The bare name of an uploaded file, at most 120 characters, with its suffix kept.

    Browsers send a bare name, but a hand-made request can send a path, control characters
    or a 4 KB name. The name is shown to users and quoted to the model, never used as a path.
    """
    name = PurePosixPath(re.sub(r"[\x00-\x1f\x7f]", "", filename or "").replace("\\", "/")).name.strip()
    if len(name) > MAX_FILENAME_CHARS:
        suffix = PurePosixPath(name).suffix[:10]
        name = name[: MAX_FILENAME_CHARS - len(suffix)] + suffix
    return name


def manual_suffix(filename: str) -> str:
    """The one rule for a manual's file type, used both to read and to save it."""
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def make_manual_id(filename: str, data: bytes) -> str:
    """A stable id built from the file name and its content hash, e.g. 'lumina-lw-9-3fa2c19b0d'.
    The same file uploaded twice gets the same id; the user's file name never becomes a path."""
    slug = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(filename.replace("\\", "/")).stem.lower()).strip("-")[:40] or "manual"
    return f"{slug}-{content_digest(data)}"


def builtin_manual_id(filename: str) -> str:
    """A built-in manual's id: its file name stem as a slug ('Bosch WAX32 Manual.pdf' becomes
    'bosch-wax32-manual'), so it always fits the manual_id pattern that POST /v1/chat accepts.
    Stems that already fit, like those of the bundled manuals, are kept unchanged."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", Path(filename).stem.lower()).strip("-_")[:80].strip("-_")
    return slug or "manual"


def _page_has_marks(page) -> bool:
    """True if a page without text still draws something, so it is worth reading with OCR: an image
    (a scanned page's whole content can be the 30 bytes "q 595 0 0 842 0 0 cm /Im0 Do Q"), or many
    drawing commands (text turned into outlines). A blank page has (almost) no content."""
    contents = page.get_contents()   # a stream object is falsy when its dictionary is empty: test for None
    data = contents.get_data() if contents is not None else b""
    return len(data) > 100 or re.search(rb"\bDo\b|\bBI\b", data) is not None


def _parse_pdf(data: bytes, filename: str, manual_id: str, title: str | None,
               ocr_max_pages: int = OCR_MAX_PAGES, progress: Progress = None) -> list[Document]:
    """A PDF's pages: the text layer where there is one, OCR for scanned pages."""
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, FileNotDecryptedError

    if not data.startswith(b"%PDF-"):
        raise ManualError("This file is not a valid PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        # "Restrict editing" PDFs are encrypted with an empty user password: every viewer
        # opens them without asking, so they must open here too.
        if reader.is_encrypted and not reader.decrypt(""):
            raise ManualError(PASSWORD_PROTECTED)
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise ManualError(f"This PDF has more than {MAX_PDF_PAGES} pages, which is the limit.")
        texts: dict[int, str] = {}
        scanned: list[int] = []
        total = content_bytes = 0
        for number, page in enumerate(reader.pages, start=1):
            if progress and number % 10 == 1:
                progress("reading", number - 1, page_count)
            contents = page.get_contents()
            content_bytes += len(contents.get_data()) if contents is not None else 0
            if content_bytes > MAX_PDF_CONTENT_BYTES:
                raise ManualError("This PDF is too large to read here.", status=413)
            text = _pdf_page_text(page)
            total += len(text)  # checked page by page, so a flood of text stops early
            if total > MAX_MANUAL_CHARS:
                raise ManualError(TOO_MUCH_TEXT, status=413)
            if text.strip():
                texts[number] = text
            elif _page_has_marks(page):
                scanned.append(number)
        pdf_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except ManualError:
        raise
    except DependencyError as exc:
        raise ManualError(
            "This PDF is encrypted, and the server is missing the 'cryptography' package needed to read it. "
            "Install it with: pip install -r requirements.txt"
        ) from exc
    except FileNotDecryptedError as exc:
        raise ManualError(PASSWORD_PROTECTED) from exc
    except Exception as exc:  # pypdf raises many types for damaged files
        raise ManualError("This PDF could not be read. It may be damaged.") from exc

    skipped: list[int] = []
    if len(scanned) > ocr_max_pages:
        if not texts:
            raise ManualError(
                f"This PDF has {len(scanned)} scanned pages; text recognition is limited to {ocr_max_pages} "
                "pages per manual. Split the PDF into parts and upload each one.")
        # A long manual with a text layer and many picture pages (photos, exploded views) is still
        # indexed: its text pages, and as many picture pages as OCR may read. The rest are reported.
        scanned, skipped = scanned[:ocr_max_pages], scanned[ocr_max_pages:]
    if scanned:  # scanned pages (images only): read them with OCR
        try:
            recognised = ocr_pdf_pages(data, scanned, progress)
        except ManualError:
            raise
        except Exception as exc:
            raise ManualError("The scanned pages of this PDF could not be read. It may be damaged.") from exc
        for number, text in recognised.items():
            total += len(text)
            if total > MAX_MANUAL_CHARS:
                raise ManualError(TOO_MUCH_TEXT, status=413)
            if text.strip():
                texts[number] = text

    pages, cover = [], None
    for number in sorted(texts):
        if cover is None and number == min(texts):
            cover = _cover_line(texts[number])
        for section, section_text in _pdf_sections(join_wrapped_lines(texts[number]), number):
            pages.append(Document(page_content=section_text, metadata={
                "source": filename, "section": section, "page": number, "page_count": page_count,
                "ocr": number in scanned,
            }))
    if not pages:
        raise ManualError(
            "No text could be found in this PDF, not even by reading its pages as images (OCR). "
            "It may be blank, or the scan may be too faint or blurry."
            if scanned else "No text could be found in this PDF. It may be blank."
        )
    title = title or (pdf_title if _usable_pdf_title(pdf_title, filename) else None) or _fallback_title(filename, cover)
    for doc in pages:
        doc.metadata.update(title=title, manual_id=manual_id, kind="pdf", format="pdf", ocr_pages=len(scanned),
                            ocr_skipped_pages=len(skipped))
    return pages


def _fallback_title(filename: str, cover: str | None) -> str:
    """A file named by its model number ("LFXS26973S.pdf") names the manual better than the cover's
    first line ("French Door Refrigerator"), unless that line names the model too ("LFXS26973S French
    Door Refrigerator"); otherwise the cover line beats the file name."""
    def letters_and_digits(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    if is_model_number_name(filename):
        model = letters_and_digits(Path(filename).stem)
        if cover and model in letters_and_digits(cover):
            return cover
        return pretty_name(filename)
    return cover or pretty_name(filename)


def _paged_documents(converted, filename: str, manual_id: str, title: str | None) -> list[Document]:
    """Photos, scans and XPS files: one text per page, split into sections like PDF pages."""
    pages, total = [], 0
    for number, text in converted.pages:
        total += len(text)
        if total > MAX_MANUAL_CHARS:
            raise ManualError(TOO_MUCH_TEXT, status=413)
        for section, section_text in _pdf_sections(join_wrapped_lines(text), number):
            pages.append(Document(page_content=section_text, metadata={
                "source": filename, "section": section, "page": number, "page_count": converted.page_count,
                "ocr": converted.ocr_pages > 0,
            }))
    cover = _cover_line(converted.pages[0][1]) if converted.pages else None
    title = title or converted.title or _fallback_title(filename, cover)
    for doc in pages:
        doc.metadata.update(title=title, manual_id=manual_id, kind="pdf", format=converted.kind,
                            ocr_pages=converted.ocr_pages)
    return pages


def parse_manual(data: bytes, filename: str, *, manual_id: str, title: str | None = None,
                 ocr_max_pages: int = OCR_MAX_PAGES, progress: Progress = None) -> list[Document]:
    """Read one manual, in any supported format, from bytes into LangChain Documents.

    PDFs (and PDFs found inside other files) keep their page structure; photos and scans are read
    with OCR; every other format arrives as Markdown-style text from app/formats.py.
    Raises ManualError with a user-facing message for files that cannot be used.
    """
    title = (title or "").strip() or None
    if progress:
        progress("reading", 0, 0)
    converted = convert(data, filename, ocr_max_pages=ocr_max_pages, progress=progress)
    if converted.pdf is not None:
        documents = _parse_pdf(converted.pdf, filename, manual_id, title, ocr_max_pages, progress)
        for doc in documents:
            doc.metadata["format"] = converted.kind     # "pdf", or "iwork" for a Pages file's PDF preview
        return documents
    if converted.pages:
        return _paged_documents(converted, filename, manual_id, title)

    text = HTML_COMMENT_RE.sub("", converted.markdown)
    if not text.strip():
        raise ManualError("This file is empty.")
    if len(text) > MAX_MANUAL_CHARS:
        raise ManualError(TOO_MUCH_TEXT, status=413)
    usable = converted.title if converted.title and _usable_pdf_title(converted.title, filename) else None
    title = title or usable or _first_heading(text) or pretty_name(filename)
    metadata = {"source": filename, "title": title, "manual_id": manual_id, "kind": "markdown",
                "format": converted.kind, "ocr_pages": 0}
    if converted.page_count:
        metadata["page_count"] = converted.page_count   # slides of a presentation
    return [Document(page_content=linearize_markdown_tables(text, MAX_MANUAL_CHARS), metadata=metadata)]


def format_label(kind: str | None) -> str | None:
    return LABELS.get(kind) if kind else None


def load_manuals(manuals_dir: Path) -> list[Document]:
    """Read every manual in the folder (any supported format). Each file's id is a slug of its name."""
    docs: list[Document] = []
    names: dict[str, str] = {}
    for path in sorted(manuals_dir.iterdir()):
        if path.name.startswith(".") or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        manual_id = builtin_manual_id(path.name)
        if manual_id in names:  # two files would silently merge into one manual
            raise ValueError(f"{names[manual_id]} and {path.name} in {manuals_dir} both get the manual id "
                             f"'{manual_id}'. Rename one of them.")
        names[manual_id] = path.name
        docs.extend(parse_manual(path.read_bytes(), path.name, manual_id=manual_id))
    if not docs:
        raise FileNotFoundError(f"No manuals found in {manuals_dir}")
    return docs


def split_documents(docs: list[Document], chunk_size: int, chunk_overlap: int) -> list[Document]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "title"), ("##", "section")], strip_headers=True
    )
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],  # prefer paragraph, then line, then sentence breaks
    )

    chunks: list[Document] = []
    for doc in docs:
        inherited = {k: v for k, v in doc.metadata.items() if k not in {"title", "section"}}
        if doc.metadata.get("kind") == "pdf":
            sections = [Document(page_content=doc.page_content, metadata=dict(doc.metadata))]
        else:
            sections = header_splitter.split_text(doc.page_content)
            for s in sections:
                s.metadata.update(inherited)

        for section in size_splitter.split_documents(sections):
            title = doc.metadata.get("title") or section.metadata.get("title") or doc.metadata["source"]
            heading = section.metadata.get("section", "")
            section.metadata.update({**inherited, "title": title, "section": heading})
            # Contextual chunk header: "E21" alone does not say which appliance it
            # belongs to, so every chunk starts with the manual title and section.
            header = f"[{title}" + (f" — {heading}]" if heading else "]")
            section.page_content = f"{header}\n{section.page_content.strip()}"
            chunks.append(section)

    counters: dict[str, int] = defaultdict(int)
    for chunk in chunks:  # numbered within each manual
        manual_id = chunk.metadata.get("manual_id", "")
        chunk.metadata["chunk_id"] = counters[manual_id]
        counters[manual_id] += 1
    return chunks


def chunk_ids(chunks: list[Document]) -> list[str]:
    return [f"{c.metadata['manual_id']}:{c.metadata['chunk_id']}" for c in chunks]


def build_vector_store(chunks: list[Document], embeddings: Embeddings) -> InMemoryVectorStore:
    """Embed every chunk and keep the vectors in memory.

    InMemoryVectorStore is enough for a few hundred chunks. For thousands of
    documents you would switch to FAISS, Chroma, pgvector, etc. The rest of the
    code would not change, because they all share LangChain's VectorStore API.
    """
    store = InMemoryVectorStore(embeddings)
    if chunks:
        store.add_documents(chunks, ids=chunk_ids(chunks))
    return store
