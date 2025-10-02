import os, json, re, time, csv
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from tqdm import tqdm
import pandas as pd  # kept for parity with original; not strictly required
from openai import OpenAI
from openai import APIError, RateLimitError, InternalServerError, BadRequestError

# ========= CONFIG=========
API_KEY        = "YOUR_API_KEY"
MODEL_NAME      = "openai/gpt-oss-20b"
INPUT_PATH      = "LLM-SecDB.json"          # your input file
OUTPUT_BASENAME = "./results/out"           # writes out.json and out.csv
TEMPERATURE     = 0.2
MAX_RETRIES     = 4
SLEEP_BACKOFF   = 2.0
SHOW_COMMENTS   = True                      # True to include raw comments in CSV/JSON
TEST_MODE_LIMIT = None                      # e.g., 10 for a quick smoke test
MAX_WORKERS     = 50  # thread count for parallel requests

# live SEM options
COUNT_NONE_AS_HIT = True  # treat none/none as a hit in running stats, like original
# ======================================


BASE_URL = "https://openrouter.ai/api/v1"

# Create results folder if needed
os.makedirs(os.path.dirname(OUTPUT_BASENAME), exist_ok=True)

print(f"[*] Model: {MODEL_NAME} is loaded")
print(f"[*] Input: {INPUT_PATH}")
print(f"[*] Parallel workers: {MAX_WORKERS}")

# ====================== PROMPT + PARSING ======================

GENERAL_PROMPT = """
You are a careful content reviewer for a scientific journal. You receive a single comment string (no other context).
Your task is to classify ONLY what is explicitly present in the comment text. Do not guess or infer hidden data.

## Taxonomy (multi-label allowed)
- credentials: Real authentication material (SECRETS) or payment authentication data (unmasked). Examples: passwords/passphrases, API keys/tokens, bearer tokens, client secrets, private keys, database connection strings with embedded passwords, payment card PAN, card expiry, CVV/CVC/CID.
- network_identifiers: System/network identifiers used to identify accounts or machines, not secrets. Examples: usernames, user IDs, IP addresses, hostnames, workstation IDs, MAC addresses, ports.
- pii: Personally identifiable information such as a person’s name, email address, phone number, or physical address.
- conflict: Explicit disagreement, debate, or argument **among co-authors themselves** (e.g., disputes about content, tone, style, or direction of the paper). Do NOT include disagreements with reviewers here
- peerreview: Content that is part of the formal review workflow (reviewer/AC/PC comments, meta-reviews, rebuttals, responses to reviewers, and camera-ready change requests). Also covers author planning/strategy about how to respond to reviewers, even if critical of them.
- none: No issues per the above categories.



## Key rules
1) Masked/redacted/placeholder values (e.g., "<***.***.***.***>", "X.X.X.X", "REDACTED") -> none, if no other sensitive datat is included.
2) Generic tool/log scaffolding alone (HTTP methods/paths/status, timestamps, user-agents like "curl/8.4") -> none, unless a user/account OR concrete asset ID /session_token is also present.
3) **Do NOT mark credentials** for mere identifiers. Usernames, IPs, hostnames, workstation IDs, ports, and URLs are **network_identifiers**, not credentials.
4) **Credentials requires explicit secrets or payment auth data.**
5) **Payment card data -> credentials.** If a name/email/phone is also present, include **pii** too.
6) **Emails**: email addresses are considered pii and may also serve as network_identifiers when used as account names. At a minimum, treat an email address as PII if it belongs to an identifiable individual, as opposed to a generic or shared address (e.g., info@ibm.com)
7) **Publication / Citation Information is not PII.** When names/titles appear purely as bibliographic metadata—e.g., author names, article titles, journal/conference names, DOIs, affiliations, volume/issue/pages, arXiv/ISBN/ISSN—classify as **none** (unless other sensitive indicators like personal emails/phones/addresses also appear).
8) **IPs, ports, or URLs** on their own — when they do not represent information leakage (e.g., values from RFC examples or documentation) — should not be classified as network_identifiers.
9) **URLs/Domains or file paths** that are non-sensitive should be ignored and classified as none, provided no other sensitive indicators are present (e.g session token).
10) Conflict applies only to explicit author–author disagreement. If text shows author disagreement with reviewers, classify as peerreview instead.
11) If nothing matches, return **none**.
12) Apply a category only if it is 100% certain from the text itself. If there is any doubt or ambiguity, do not apply that label. If no category can be applied with full certainty, return <xml>none</xml>.
13) Sensitive elements may be hidden inside LaTeX, code, other irrelevant text segment. Only classify when the sensitive content itself is explicit; do not confuse normal markup, equations, or citations with sensitive data.
14) Output format is STRICT: return only <xml>...</xml> with comma-separated labels, e.g. <xml>credentials,pii</xml> or <xml>none</xml>. No extra text.


Now analyze this comment:
"""

ALLOWED_LABELS = {
    "credentials",
    "network_identifiers",
    "pii",
    "conflict",
    "peerreview",
    "none",
}

XML_RE = re.compile(r"<xml>.*?</xml>", re.IGNORECASE | re.DOTALL)

def build_prompt(comment_text: str) -> str:
    return f"{GENERAL_PROMPT}\n---\n{comment_text}\n---"

def extract_xml_answer(text: str) -> Optional[str]:
    if not text:
        return None
    m = XML_RE.search(text)
    return m.group(0).strip() if m else None

def sanitize_xml(xml_text: Optional[str]) -> Optional[str]:
    if not xml_text:
        return None
    xml_text = xml_text.strip()
    if xml_text.lower().startswith("<xml>") and xml_text.lower().endswith("</xml>"):
        return xml_text
    # salvage if wrapper missing
    inner = re.sub(r"^`+|`+$", "", xml_text).strip()
    inner = re.sub(r"[^a-zA-Z0-9_,\-\s]", "", inner).strip()
    if inner:
        return f"<xml>{inner}</xml>"
    return None

def parse_labels_from_xml(xml_text: str) -> List[str]:
    # from <xml>a,b</xml> -> ["a", "b"] (normalized)
    content = xml_text.strip()[5:-6].strip()
    if not content:
        return []
    parts = [p.strip().lower() for p in content.split(",")]
    labels = []
    for p in parts:
        p = p.replace("network_identifiers:", "network_identifiers")
        p = re.sub(r"[^a-z_]", "", p)
        if not p:
            continue
        if p == "none":
            return []
        if p in ALLOWED_LABELS and p != "none":
            labels.append(p)
    # de-dup, preserve order
    seen, out = set(), []
    for l in labels:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def parse_ground_truth_labels(rec: Dict[str, Any]) -> List[str]:
    raw = rec.get("classification", "")
    if not isinstance(raw, str) or not raw.strip():
        return []
    parts = [p.strip().lower() for p in raw.split(",")]
    labels = []
    for p in parts:
        p = p.replace("network_identifiers:", "network_identifiers")
        p = re.sub(r"[^a-z_]", "", p)
        if p in ALLOWED_LABELS and p != "none":
            labels.append(p)
    seen, out = set(), []
    for l in labels:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def read_input_any(path: str) -> List[Dict[str, Any]]:
    # supports JSON array, single JSON object, or NDJSON
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass
    # NDJSON fallback
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(obj)
            except json.JSONDecodeError:
                continue
    return out

# Thread-local OpenAI client (safe for multithreading with the SDK)
_tls = threading.local()
def get_client() -> OpenAI:
    cli = getattr(_tls, "client", None)
    if cli is None:
        cli = OpenAI(base_url=BASE_URL, api_key=API_KEY)
        _tls.client = cli
    return cli

def ask_llm(comment_text: str) -> Tuple[Optional[str], Optional[str]]:
    prompt = build_prompt(comment_text)
    backoff = SLEEP_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = get_client().chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=TEMPERATURE,
            )
            content = resp.choices[0].message.content if resp.choices else ""
            xml = extract_xml_answer(content)
            xml = sanitize_xml(xml) or sanitize_xml(content)
            if not xml:
                return None, "No valid <xml>...</xml> returned."
            return xml, None
        except (RateLimitError, InternalServerError) as e:
            if attempt == MAX_RETRIES:
                return None, f"{type(e).__name__}: {e}"
            time.sleep(backoff); backoff *= 1.7
        except (APIError, BadRequestError) as e:
            return None, f"{type(e).__name__}: {e}"
        except Exception as e:
            if attempt == MAX_RETRIES:
                return None, f"UnexpectedError: {e}"
            time.sleep(backoff); backoff *= 1.7

def classify_record(idx_and_rec: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
    idx, rec = idx_and_rec
    comment = rec.get("comments", "")
    if not isinstance(comment, str) or not comment.strip():
        xml, err = "<xml>none</xml>", "Missing or empty 'comments'."
        pred_labels = []
    else:
        xml, err = ask_llm(comment)
        pred_labels = parse_labels_from_xml(xml) if xml else []
    return {
        "idx": idx,
        "xml": xml or "",
        "pred_labels": list(set(pred_labels)),
        "error": err,
        "_comment_length": len(comment) if isinstance(comment, str) else 0,
    }

# ====================== MAIN ======================

# Progress bar cleanup (optional, mirrors your original pattern)
tqdm._instances.clear()

records = read_input_any(INPUT_PATH)
if TEST_MODE_LIMIT is not None:
    records = records[:TEST_MODE_LIMIT]

if not records:
    raise SystemExit("No records found in input.")

# Phase 1: parallel classification WITH live SEM postfix
results = [None] * len(records)

# shared running metrics for live postfix
lock = threading.Lock()
sem_num_done = 0
sem_num_exact = 0
sem_num_hit_incl_none = 0
sem_pred_label_freq = Counter()  # live tally (pred only), for postfix

with tqdm(total=len(records), desc="Classifying (parallel)", ncols=150, dynamic_ncols=True, leave=False) as pbar:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut_to_idx = {ex.submit(classify_record, (i, rec)): i for i, rec in enumerate(records)}
        for fut in as_completed(fut_to_idx):
            i = fut_to_idx[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"idx": i, "xml": "<xml>none</xml>", "pred_labels": [], "error": f"WorkerError: {e}", "_comment_length": 0}
            results[i] = r

            # compute live metrics for this completed item
            with lock:
                pred_set = set(r["pred_labels"])
                gt_set   = set(parse_ground_truth_labels(records[i]))

                sem_num_done += 1
                if pred_set == gt_set:
                    sem_num_exact += 1

                inter = pred_set & gt_set
                hit_now = bool(inter) or (COUNT_NONE_AS_HIT and not pred_set and not gt_set)
                if hit_now:
                    sem_num_hit_incl_none += 1

                for l in pred_set:
                    sem_pred_label_freq[l] += 1

                running_exact = (sem_num_exact / sem_num_done) * 100.0
                running_hit   = (sem_num_hit_incl_none / sem_num_done) * 100.0

                postfix = {
                    "acc":   f"{running_exact:.3f}",
                    "hit":   f"{running_hit:.3f}",
                    "cred":  sem_pred_label_freq["credentials"],
                    "netid": sem_pred_label_freq["network_identifiers"],
                    "pii":   sem_pred_label_freq["pii"],
                    "conf":  sem_pred_label_freq["conflict"],
                    "prrev": sem_pred_label_freq["peerreview"],
                    "none":  sem_pred_label_freq["none"],  # typically 0; kept for parity
                }
                pbar.set_postfix(postfix)
                pbar.update(1)

# Phase 2: sequential aggregation, final metrics, and file outputs (same logic as original)
enriched: List[Dict[str, Any]] = []

num_done = 0
num_exact = 0
num_hit_incl_none = 0
num_hit_nonempty = 0

num_any_pred = 0
num_any_gt = 0
num_false_pos_records = 0
num_false_neg_records = 0

pred_label_freq = Counter()
gt_label_freq = Counter()
tp_label = Counter()
fp_label = Counter()
fn_label = Counter()

for i, rec in enumerate(records):
    r = results[i]
    xml = r["xml"]
    pred_labels = r["pred_labels"]
    err = r["error"]

    gt_labels = parse_ground_truth_labels(rec)
    pred_set, gt_set = set(pred_labels), set(gt_labels)

    num_done += 1
    if pred_set == gt_set:
        num_exact += 1

    inter = pred_set & gt_set
    hit_now = bool(inter) or (COUNT_NONE_AS_HIT and not pred_set and not gt_set)
    if hit_now:
        num_hit_incl_none += 1
    if inter:
        num_hit_nonempty += 1

    if pred_set: num_any_pred += 1
    if gt_set:   num_any_gt += 1
    if pred_set and not gt_set: num_false_pos_records += 1
    if gt_set and not pred_set: num_false_neg_records += 1

    for l in gt_set:   gt_label_freq[l] += 1
    for l in pred_set: pred_label_freq[l] += 1
    for l in inter: tp_label[l] += 1
    for l in pred_set - gt_set: fp_label[l] += 1
    for l in gt_set - pred_set: fn_label[l] += 1

    row = {
        **rec,
        "xml": xml or "",
        "pred_labels": list(pred_set),
        "gt_labels": list(gt_set),
        "error": err,
    }
    if not SHOW_COMMENTS:
        row["_comment_length"] = r["_comment_length"]
        row.pop("comments", None)
    enriched.append(row)

print(f"✅ Finished.")
print(f"Exact-match accuracy: {num_exact/num_done:.4f}")
print(f"At least one correct (incl. none/none): {num_hit_incl_none} / {num_done} ({num_hit_incl_none/num_done:.1%})")

# Optional diagnostics (toggle if you want them)
SHOW_NONEMPTY_COUNTS = False
if SHOW_NONEMPTY_COUNTS:
    print(f"Ground-truth with ≥1 label: {num_any_gt} ({num_any_gt/num_done:.1%})")
    print(f"Predictions with ≥1 label:  {num_any_pred} ({num_any_pred/num_done:.1%})")
    print(f"At least one correct (non-empty only): {num_hit_nonempty} ({num_hit_nonempty/num_done:.1%})")

print(f"False-positive records (pred≠∅ & gt=∅): {num_false_pos_records}")
print(f"False-negative records (gt≠∅ & pred=∅): {num_false_neg_records}")

print("\nBy-label counts (GT vs Pred, TP/FP/FN and precision/recall):")
labels_all = sorted(set(gt_label_freq) | set(pred_label_freq))
for l in labels_all:
    tp = tp_label[l]; fp = fp_label[l]; fn = fn_label[l]
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec  = tp / (tp + fn) if (tp + fn) else 0.0
    print(f" - {l:20s} GT={gt_label_freq[l]:4d}  Pred={pred_label_freq[l]:4d}  "
          f"TP={tp:4d}  FP={fp:4d}  FN={fn:4d}  P={prec:.2f} R={rec:.2f}")


# save outputs (model name appended; replace "/" or "\" with "_")
model_safe = re.sub(r"[\\/]", "_", MODEL_NAME)  # e.g., "qwen/qwen-2.5-7b-instruct" -> "qwen_qwen-2.5-7b-instruct"
base = f"{OUTPUT_BASENAME}_{model_safe}"
json_path = base + ".json"
csv_path  = base + ".csv"

with open(json_path, "w", encoding="utf-8") as f:
    json.dump(enriched, f, ensure_ascii=False, indent=2)

csv_cols = ["xml", "pred_labels", "gt_labels", "error"]
if SHOW_COMMENTS:
    csv_cols = ["comments"] + csv_cols
else:
    csv_cols = ["_comment_length"] + csv_cols

with open(csv_path, "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["index"] + csv_cols + ["exact_match"])
    for idx, rec in enumerate(enriched):
        exact = int(set(rec.get("pred_labels", [])) == set(rec.get("gt_labels", [])))
        writer.writerow([
            idx,
            *(rec.get(c, "") if not isinstance(rec.get(c, ""), list) else ",".join(rec.get(c, [])) for c in csv_cols),
            exact
        ])

print("📄 Wrote files:")
print(" -", json_path)
print(" -", csv_path)
