# This script scans '/mnt/data/TEST_pdf' for PDFs, randomly extracts clean text snippets
# (200–500 chars), filters out any snippets that *look* sensitive, and writes up to
# 250 entries to '/mnt/data/NEW.json' in your dataset schema.
#
# If the folder or PDFs are missing, it will explain that in the output cell.

import os, re, json, random, sys
from pathlib import Path

BASE_DIR = Path("TEST_pdf")
OUT_PATH = Path("NEW.json")
TARGET_COUNT = 250
MIN_LEN = 200
MAX_LEN = 500

# Sensitivity heuristics (skip any snippet that hits these so resulting entries can be sensitive:false)
PATTERNS = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # Email
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),                         # IPv4
    re.compile(r"\bhttps?://\S+", re.I),                                 # URLs
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                 # AWS Access Key ID
    re.compile(r"\bghp_[A-Za-z0-9]{10,}\b"),                             # GitHub PAT-ish
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=]+", re.I),                    # Bearer tokens
    re.compile(r"(?i)BEGIN [A-Z ]*PRIVATE KEY"),                         # Private keys
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"),               # password=
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                               # Long digit sequences (credit-card-ish)
]

def looks_sensitive(s: str) -> bool:
    for pat in PATTERNS:
        if pat.search(s):
            return True
    return False

def cleanup_text(t: str) -> str:
    # Collapse whitespace; keep basic punctuation
    t = re.sub(r"\s+", " ", t).strip()
    # Remove excessive control chars
    t = t.replace("\x00", "")
    return t

def chunk_text_to_range(t: str, min_len=MIN_LEN, max_len=MAX_LEN):
    """
    Yield chunks between min_len and max_len, trying to cut at sentence boundaries.
    """
    t = cleanup_text(t)
    if not t:
        return
    # Split by sentence-ish punctuation, but keep delimiters
    parts = re.split(r"([\.!?])", t)
    # Re-stitch while controlling length
    buf = ""
    for i in range(0, len(parts), 2):
        segment = parts[i]
        punct = parts[i+1] if i+1 < len(parts) else ""
        candidate = (buf + segment + punct).strip()
        if len(candidate) < min_len:
            buf = candidate + " "
            continue
        if len(candidate) <= max_len:
            yield candidate.strip()
            buf = ""
        else:
            # If too long, cut at a space near max_len
            cut = candidate.rfind(" ", 0, max_len)
            if cut < min_len:  # no good space, hard cut
                cut = max_len
            yield candidate[:cut].strip()
            # Start new buffer with remainder
            buf = candidate[cut:].strip() + " "
    # If leftover buffer is within range, yield it
    final = buf.strip()
    if min_len <= len(final) <= max_len:
        yield final

# PDF extraction backends (try several)
def extract_text_pdf(path: Path) -> str:
    # Try PyMuPDF
    try:
        import fitz  # PyMuPDF
        text = []
        with fitz.open(path) as doc:
            for page in doc:
                text.append(page.get_text("text"))
        return "\n".join(text)
    except Exception:
        pass
    # Try pdfminer.six
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        return pdfminer_extract(str(path))
    except Exception:
        pass
    # Try PyPDF2
    try:
        import PyPDF2
        text = []
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for p in range(len(reader.pages)):
                page = reader.pages[p]
                text.append(page.extract_text() or "")
        return "\n".join(text)
    except Exception:
        pass
    return ""

def collect_pdf_files(base: Path):
    if not base.exists():
        return []
    return [p for p in base.rglob("*.pdf") if p.is_file()]

pdfs = collect_pdf_files(BASE_DIR)

result = []
problems = []

if not pdfs:
    problems.append(f"No PDFs found in {BASE_DIR}. Please upload PDFs to that folder or specify the correct path.")
else:
    random.shuffle(pdfs)
    # First pass: gather candidate chunks from random PDFs until we hit TARGET_COUNT
    for pdf in pdfs:
        if len(result) >= TARGET_COUNT:
            break
        try:
            raw = extract_text_pdf(pdf)
        except Exception as e:
            problems.append(f"Failed to read {pdf.name}: {e}")
            continue
        raw = cleanup_text(raw)
        if not raw or len(raw) < 50:
            continue
        # Derive chunks
        chunks = list(chunk_text_to_range(raw))
        random.shuffle(chunks)
        for ch in chunks:
            if len(result) >= TARGET_COUNT:
                break
            if looks_sensitive(ch):
                continue  # skip anything that may include sensitive content
            result.append({
                "comments": ch,
                "sensitive": False,
                "type": "none",
                "extracted_data": []
            })

# Save output (even if empty, so user can inspect)
with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

# Summarize outcome for the user
print(f"PDF directory: {BASE_DIR}")
print(f"PDFs discovered: {len(pdfs)}")
print(f"Entries written: {len(result)} (target {TARGET_COUNT})")
if problems:
    print("\nNotes:")
    for p in problems[:10]:
        print(f"- {p}")
if len(problems) > 10:
    print(f"... and {len(problems)-10} more issues omitted for brevity.")
print(f"\nOutput file: {OUT_PATH}")
