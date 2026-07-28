import json, os, re, sys
sys.path.insert(0, os.path.expanduser('~/solr-bridge'))
from correct_text import suspects, norm, safe_shape, ask, FREQ, GOOD

def match_case(orig, corr):
    if orig.isupper(): return corr.upper()
    if orig[0].isupper(): return corr[0].upper() + corr[1:]
    return corr

def apply_page(json_in, json_out, audit_path):
    j = json.load(open(json_in))
    audit = []
    for b in j['parsing_res_list']:
        text = b.get('block_content') or ''
        if not text: continue
        for word, cands, ctx in suspects(text):
            lw = norm(word).lower()
            if (len(cands) == 1 and FREQ.get(cands[0], 0) >= 50
                    and FREQ.get(lw, 0) == 0 and safe_shape(lw, cands[0])):
                verdict, tier = cands[0], "AUTO"
            else:
                ans = ask(word, cands, ctx)
                verdict = ans if ans in [c.lower() for c in cands] else None
                tier = "LLM"
            if verdict:
                fixed = match_case(word, verdict)
                text = re.sub(r'\b' + re.escape(word) + r'\b', fixed, text)
                audit.append({"page": os.path.basename(json_in),
                              "orig": word, "corr": fixed, "tier": tier})
        b['block_content'] = text
    json.dump(j, open(json_out, 'w'), indent=1)
    with open(audit_path, 'a') as f:
        for a in audit: f.write(json.dumps(a) + "\n")
    return len(audit)

if __name__ == '__main__':
    n = apply_page(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"{os.path.basename(sys.argv[1])}: {n} corrections applied")
