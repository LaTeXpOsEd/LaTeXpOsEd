#!/usr/bin/env python3
"""
json_stats.py — quick stats + validation for classification JSON.

Accepts:
- JSON array:        [ {obj}, {obj}, ... ]
- Single JSON object: {obj}
- NDJSON:            one {obj} per line

Schema (expected keys per record):
{
  "comments": str,
  "flagged": bool,
  "classification": str,            # may be "a,b" for multi-label
  "extracted_data": [               # list of { "value": str, "label": str }
    { "value": str, "label": str }, ...
  ]
}

Outputs (to stdout):
- Totals and by-category counts
- Flagged True/False counts
- Avg, min, max comment length
- extracted_data label frequencies
- Validation report (how many issues + first N examples)

Usage:
  python json_stats.py --input test.json
  python json_stats.py --input test.ndjson
"""

import argparse
import json
import sys
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

ALLOWED_CLASSES = {
    "credentials",
    "network_identifiers",
    "pii",
    "peerreview",
    "conflict",
    "none",
}

XML_SAFE_LABEL = re.compile(r"^[a-z0-9_]+$", re.I)  # optional light sanity for labels

def load_any(path: str) -> List[Dict[str, Any]]:
    """Load JSON array, single object, or NDJSON."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
        if not text:
            return []
        # try JSON (array or object)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass
    # fallback: NDJSON
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # skip malformed lines but note it
                out.append({"__parse_error__": True, "raw": line})
    return out

def parse_classification(raw: Any) -> List[str]:
    """Normalize classification to a list of lowercased labels (allow comma-separated)."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    parts = [p.strip().lower() for p in raw.split(",")]
    out = []
    seen = set()
    for p in parts:
        p = p.replace("network_identifiers:", "network_identifiers")
        p = re.sub(r"[^a-z_]", "", p)
        if p and p != "none" and p in ALLOWED_CLASSES and p not in seen:
            seen.add(p)
            out.append(p)
    return out

def validate_record(rec: Dict[str, Any], idx: int) -> List[str]:
    """Return a list of validation issues for a record."""
    issues = []
    if rec.get("__parse_error__"):
        issues.append("Record is not valid JSON (NDJSON line parse failed).")
        return issues

    # comments
    if "comments" not in rec:
        issues.append("Missing 'comments'.")
    elif not isinstance(rec["comments"], str):
        issues.append("'comments' must be a string.")
    elif not rec["comments"].strip():
        issues.append("'comments' is empty.")

    # flagged
    if "flagged" not in rec:
        issues.append("Missing 'flagged'.")
    elif not isinstance(rec["flagged"], bool):
        issues.append("'flagged' must be a boolean.")

    # classification
    if "classification" not in rec:
        issues.append("Missing 'classification'.")
    elif not isinstance(rec["classification"], str):
        issues.append("'classification' must be a string.")
    else:
        clabels = parse_classification(rec["classification"])
        raw = rec["classification"].strip().lower()
        # allow "none" (meaning no labels), else require allowed labels
        if raw != "none" and not clabels:
            issues.append(f"'classification' has no valid labels: {rec['classification']}")

    # extracted_data
    if "extracted_data" not in rec:
        issues.append("Missing 'extracted_data'.")
    elif not isinstance(rec["extracted_data"], list):
        issues.append("'extracted_data' must be a list.")
    else:
        for j, item in enumerate(rec["extracted_data"]):
            if not isinstance(item, dict):
                issues.append(f"extracted_data[{j}] must be an object.")
                continue
            if "value" not in item or "label" not in item:
                issues.append(f"extracted_data[{j}] missing 'value' or 'label'.")
                continue
            if not isinstance(item["value"], str) or not isinstance(item["label"], str):
                issues.append(f"extracted_data[{j}] 'value' and 'label' must be strings.")
                continue
            if not item["label"].strip():
                issues.append(f"extracted_data[{j}] 'label' is empty.")
            # optional: gentle label sanity (doesn't fail, just warns)
            if not XML_SAFE_LABEL.match(item["label"].replace(" ", "_")):
                issues.append(f"extracted_data[{j}] unusual label format: '{item['label']}'")

    return issues

def main():
    ap = argparse.ArgumentParser(description="Quick stats + validation for classification JSON.")
    ap.add_argument("--input", "-i", required=True, help="Path to JSON/NDJSON file (e.g., test.json).")
    ap.add_argument("--max-errors", type=int, default=20, help="Show up to this many example errors.")
    args = ap.parse_args()

    records = load_any(args.input)
    if not records:
        print("No records found.", file=sys.stderr)
        sys.exit(1)

    total = len(records)
    by_category = Counter()
    flagged_counts = Counter()
    comment_lengths = []
    exdata_label_freq = Counter()

    problems: List[Tuple[int, List[str]]] = []

    for i, rec in enumerate(records):
        # validation
        issues = validate_record(rec, i)
        if issues:
            problems.append((i, issues))

        # stats (best-effort even if issues exist)
        comments = rec.get("comments", "")
        if isinstance(comments, str):
            comment_lengths.append(len(comments))

        flagged = rec.get("flagged")
        if isinstance(flagged, bool):
            flagged_counts[str(flagged)] += 1

        # classification counts
        if isinstance(rec.get("classification"), str):
            raw = rec["classification"].strip().lower()
            labels = parse_classification(rec["classification"])
            if raw == "none" or not labels:
                by_category["none"] += 1
            else:
                for l in labels:
                    by_category[l] += 1
        else:
            by_category["__invalid__"] += 1

        # extracted_data label frequencies
        exd = rec.get("extracted_data", [])
        if isinstance(exd, list):
            for item in exd:
                if isinstance(item, dict) and isinstance(item.get("label"), str):
                    exdata_label_freq[item["label"]] += 1

    # Summary
    print("=== Summary ===")
    print(f"Total records: {total}")
    if comment_lengths:
        print(f"Comment length (chars): avg={sum(comment_lengths)/len(comment_lengths):.1f}, "
              f"min={min(comment_lengths)}, max={max(comment_lengths)}")
    print("Flagged counts:", dict(flagged_counts))
    print("By classification (label occurrences):")
    for label, cnt in sorted(by_category.items(), key=lambda x: (-x[1], x[0])):
        print(f"  - {label}: {cnt}")

    if exdata_label_freq:
        print("Extracted-data label frequencies:")
        for label, cnt in exdata_label_freq.most_common():
            print(f"  - {label}: {cnt}")

    # Validation report
    print("\n=== Validation ===")
    print(f"Records with issues: {len(problems)} / {total}")
    for idx, issues in problems[:args.max_errors]:
        print(f"  • Record #{idx}:")
        for msg in issues:
            print(f"     - {msg}")
    if len(problems) > args.max_errors:
        print(f"  ... and {len(problems) - args.max_errors} more.")

if __name__ == "__main__":
    main()
