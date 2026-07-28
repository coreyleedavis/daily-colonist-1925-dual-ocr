import json, glob, os, re

CASES = [
    ("city council phrase", "battery_diffs_phrase_city_council.json", re.compile(r"city\s+council", re.I)),
    ("typewriter booby-trap", "battery_diffs_booby_trap_typewriter.json", re.compile(r"typewriter", re.I)),
]

def find_described(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    path = os.path.expanduser(f"~/paddle-year/{issue}/{page_id}_described.json")
    return path if os.path.exists(path) else None

def main():
    for label, fname, pattern in CASES:
        d = json.load(open(fname))
        ai_only = d["ai_only"]
        print(f"\n=== {label}: checking ALL {len(ai_only)} ai_only pages ===")
        label_counts = {}
        examples = []
        missing = 0
        for page_id in ai_only:
            path = find_described(page_id)
            if not path:
                missing += 1
                continue
            data = json.load(open(path))
            for block in data.get("parsing_res_list", []):
                content = block.get("block_content", "") or ""
                if pattern.search(content):
                    bl = block.get("block_label", "UNKNOWN")
                    label_counts[bl] = label_counts.get(bl, 0) + 1
                    if len(examples) < 8:
                        snippet = content[:140].replace("\n", " ")
                        examples.append((page_id, bl, snippet))
        print(f"  missing described.json: {missing}")
        print(f"  block_label breakdown of matches: {label_counts}")
        print(f"  sample matches:")
        for page_id, bl, snippet in examples:
            print(f"    [{bl:12}] {page_id}: {snippet}")

if __name__ == "__main__":
    main()
