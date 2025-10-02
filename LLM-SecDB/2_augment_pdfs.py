# This script:
# - Locates your original dataset JSON in /mnt/data (looks for a list of objects with a "comments" field).
# - Reads random PDFs from /mnt/data/TEST_pdf.
# - Extracts 200–500-char PDF snippets (avoiding sensitive-looking content).
# - For each dataset entry, inserts the entry's "comments" at a random position inside a random snippet.
# - Leaves all other fields ("sensitive", "type", "extracted_data") unchanged.
# - Writes the augmented dataset to /mnt/data/NEW_join.json.
#
# If no PDFs are found, it falls back to neutral filler text so the join still completes.
# If no input JSON is found, it will create a tiny demo using 3 entries from an inline example.

import os, re, json, random
from pathlib import Path

PDF_DIR = Path("TEST_pdf")
OUT_PATH = Path("AUGMENTED_dataset.json")
MAX_JOIN = None  # set to an int to limit how many records to process; None = all
MIN_LEN = 200
MAX_LEN = 500

# ---------- Sensitivity heuristics to keep random snippets neutral ----------
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
    t = re.sub(r"\s+", " ", t).strip()
    return t

def chunk_text_to_range(t: str, min_len=MIN_LEN, max_len=MAX_LEN):
    t = cleanup_text(t)
    if not t:
        return
    parts = re.split(r"([\.!?])", t)
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
            cut = candidate.rfind(" ", 0, max_len)
            if cut < min_len:
                cut = max_len
            yield candidate[:cut].strip()
            buf = candidate[cut:].strip() + " "
    final = buf.strip()
    if min_len <= len(final) <= max_len:
        yield final

# ---------- PDF extraction ----------
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

def get_random_clean_snippet(pdfs):
    random.shuffle(pdfs)
    for pdf in pdfs:
        raw = extract_text_pdf(pdf)
        if not raw or len(raw) < 50:
            continue
        chunks = [c for c in chunk_text_to_range(raw) if not looks_sensitive(c)]
        if not chunks:
            continue
        return random.choice(chunks), pdf.name
    # Fallback: neutral filler when no PDFs/snippets are usable
    filler = ("In the context of this document, the surrounding discussion focuses on the "
              "methodology, evaluation criteria, and limitations. The following excerpt is "
              "representative text used solely for formatting and consistency checks.")
    return filler, "<no-pdf>"

# ---------- Input dataset discovery ----------
def find_input_json():
    base = Path(".")
    candidates = []
    for p in base.glob("*.json"):
        if p.name in ("NEW_join.json", "NEW.json"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data and isinstance(data[0], dict) and "comments" in data[0]:
                candidates.append((p, len(data)))
        except Exception:
            continue
    if not candidates:
        return None
    # Prefer the largest dataset by item count
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]

IN_PATH = find_input_json()

# If no input file found, synthesize a tiny demo dataset from the user's message
if IN_PATH is None:
    demo = [
        {
            "comments": "Windows EventID 4625: An account failed to log on. User: bob_admin, Source IP: 203.0.113.15, Workstation: WS-DEV01",
            "sensitive": True,
            "type": "infrastructure_identifiers",
            "extracted_data": [
                {"value": "bob_admin", "label": "username"},
                {"value": "203.0.113.15", "label": "ip_address"},
                {"value": "WS-DEV01", "label": "workstation ID"}
            ]
        },
        {
            "comments": "nginx access: - - [16/Sep/2025:15:10:11 +0000] \"GET /api/v1/resources HTTP/1.1\" 200 1543 \"-\" \"Mozilla/5.0\"",
            "sensitive": False,
            "type": "none",
            "extracted_data": []
        },
        {
            "comments": "Application debug appended the secret in a sentence: 'The admin password (temporary) was Tempor@ry!Pwd2025 and should be rotated'",
            "sensitive": True,
            "type": "credentials",
            "extracted_data": [{"value": "Tempor@ry!Pwd2025", "label": "password"}]
        }
    ]
    input_data = demo
    input_name = "<inline-demo>"
else:
    with open(IN_PATH, "r", encoding="utf-8") as f:
        input_data = json.load(f)
    input_name = IN_PATH.name

# ---------- Build augmented dataset ----------
pdfs = collect_pdf_files(PDF_DIR)
augmented = []
used_sources = []

for idx, item in enumerate(input_data):
    if MAX_JOIN is not None and idx >= MAX_JOIN:
        break
    comments = item.get("comments", "")
    snippet, src = get_random_clean_snippet(pdfs) if pdfs else get_random_clean_snippet([])
    # Choose a random insertion point at a whitespace boundary
    spaces = [m.start() for m in re.finditer(r"\s", snippet)]
    if spaces:
        insert_pos = random.choice(spaces)
    else:
        insert_pos = random.randint(0, len(snippet))
    merged = (snippet[:insert_pos] + comments + snippet[insert_pos:]).strip()
    new_item = dict(item)  # shallow copy
    new_item["comments"] = merged
    augmented.append(new_item)
    used_sources.append(src)

with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(augmented, f, ensure_ascii=False, indent=2)

print(f"Input dataset: {input_name}")
print(f"Items processed: {len(augmented)} (of {len(input_data)})")
print(f"PDF directory: {PDF_DIR}")
print(f"PDFs discovered: {len(pdfs)}")
print(f"Sample of source PDFs used (first 10): {used_sources[:500]}")
print(f"\nOutput file: {OUT_PATH}")
