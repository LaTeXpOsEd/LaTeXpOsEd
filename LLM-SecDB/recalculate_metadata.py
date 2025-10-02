# recalc_tokens_and_hash.py
import json, hashlib, sys, os
from typing import List, Dict, Any

# 1) BERT tokenizer (excludes [CLS]/[SEP] from the count)
try:
    from transformers import BertTokenizerFast
    tok = BertTokenizerFast.from_pretrained("bert-base-uncased")
except Exception as e:
    print("Failed to load bert-base-uncased tokenizer:", e)
    print("Install with: pip install transformers")
    sys.exit(1)

IN_PATH  = "LLM-SecDB.json"
OUT_PATH = "UPDATED.json"

def load_any_json(path: str) -> List[Dict[str, Any]]:
    # Supports JSON array or NDJSON
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):  return data
        if isinstance(data, dict):  return [data]
    except json.JSONDecodeError:
        pass
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # skip bad lines quietly
                continue
    return out

def bert_len(text: str) -> int:
    # exclude special tokens to match common “content-only” counts
    return len(tok(text, add_special_tokens=False)["input_ids"])

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def main():
    if not os.path.exists(IN_PATH):
        print(f"Input not found: {IN_PATH}")
        sys.exit(1)

    records = load_any_json(IN_PATH)
    if not records:
        print("No records found.")
        sys.exit(1)

    updated = []
    for rec in records:
        c = rec.get("comments", "")
        if not isinstance(c, str):
            c = "" if c is None else str(c)
        rec["token_length"] = bert_len(c)
        rec["sha_256"] = sha256_hex(c)
        updated.append(rec)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    print(f"✅ Wrote {OUT_PATH} with {len(updated)} records.")

if __name__ == "__main__":
    main()
