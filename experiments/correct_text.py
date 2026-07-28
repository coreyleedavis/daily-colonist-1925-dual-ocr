import json, os, re, sys, requests
from symspellpy import SymSpell, Verbosity

LEX = os.path.expanduser('~/solr-bridge/lexicon_1925.tsv')
sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
GOOD, FREQ = set(), {}
for line in open(LEX):
    w, c = line.rstrip('\n').split('\t'); c = int(c)
    FREQ[w] = c
    if c >= 3 and len(w) >= 3: GOOD.add(w)
    if c >= 25 and len(w) >= 3: sym.create_dictionary_entry(w, c)

CONF = {frozenset(p) for p in [('i','l'),('c','e'),('c','o'),('e','o'),('f','p'),
        ('f','t'),('v','c'),('u','n'),('u','v'),('h','b'),('m','n'),('i','j')]}

def norm(w):
    w = w.strip("'-")
    if w.lower().endswith("'s"): w = w[:-2]
    return w

def safe_shape(s, c):
    if s.replace('-','') == c.replace('-',''): return True
    if len(s) != len(c): return False
    diffs = [(a,b) for a,b in zip(s,c) if a != b]
    return 0 < len(diffs) <= 2 and all(frozenset(d) in CONF for d in diffs)

def suspects(text):
    seen = {}
    for m in re.finditer(r"[A-Za-z][A-Za-z'-]{3,24}", text):
        w = norm(m.group(0)); lw = w.lower()
        if len(lw) < 4 or lw in GOOD or lw in seen: continue
        cands = sym.lookup(lw, Verbosity.CLOSEST, max_edit_distance=2)
        cands = [c.term for c in cands[:5] if c.term != lw]
        if cands:
            seen[lw] = (m.group(0), cands, text[max(0,m.start()-60):m.end()+60])
    return list(seen.values())

PROMPT = """You are correcting OCR errors in text from The Daily Colonist, a newspaper published in Victoria, British Columbia, Canada, in 1925.

Relevant context: The text uses 1920s Canadian English, with British spellings (colour, honour, centre) and period conventions (to-day, to-morrow, per cent). Prices are in dollars and cents; measurements are imperial. The paper covers Victoria and Vancouver Island: local place names (Esquimalt, Saanich, Oak Bay, Nanaimo, Cadboro Bay), local businesses, shipping news (CPR steamships, schooners), and British Empire news are common. Many words that look unusual are real: period brand names, local businesses, and surnames.

A suspect word from this text is given below with its surrounding context and a list of candidate corrections drawn from words attested in this newspaper.

Rules:
- Answer with EXACTLY one candidate from the list, or UNCHANGED.
- Corpus frequency is given for each candidate: prefer candidates that fit the context AND are well-attested.
- If the suspect could plausibly be a surname, business name, or place name not in the candidate list, answer UNCHANGED.
- Never choose a candidate that changes the meaning; when in doubt, UNCHANGED.

Context: ...{ctx}...
Suspect word: {word}
Candidates (with frequency in this newspaper): {cands}
Answer:"""

def ask(word, cands, ctx):
    cands_fq = ", ".join(f"{c} ({FREQ.get(c, 0)})" for c in cands)
    r = requests.post("http://localhost:8120/v1/chat/completions", json={
        "model": "describer", "max_tokens": 8, "temperature": 0.0,
        "messages": [{"role": "user", "content":
            PROMPT.format(ctx=ctx, word=word, cands=cands_fq)}]},
        timeout=120)
    return r.json()["choices"][0]["message"]["content"].strip().strip('.').lower()

def run(json_path, max_blocks=None):
    j = json.load(open(json_path))
    blocks = j['parsing_res_list'][:max_blocks] if max_blocks else j['parsing_res_list']
    audit = []
    for b in blocks:
        for word, cands, ctx in suspects(b.get('block_content') or ''):
            lw = norm(word).lower()
            if (len(cands) == 1 and FREQ.get(cands[0], 0) >= 50
                    and FREQ.get(lw, 0) == 0 and safe_shape(lw, cands[0])):
                audit.append((word, cands[0], "AUTO"))
                print(f"{word!r:26} -> {cands[0]:18} [AUTO]")
                continue
            ans = ask(word, cands, ctx)
            verdict = ans if ans in [c.lower() for c in cands] else "UNCHANGED"
            audit.append((word, verdict, "LLM"))
            print(f"{word!r:26} -> {verdict:18} [LLM]  ({', '.join(c+'('+str(FREQ.get(c,0))+')' for c in cands[:3])})")
    auto = sum(1 for *_, t in audit if t == "AUTO")
    changed = sum(1 for _, v, _ in audit if v != "UNCHANGED")
    print(f"\n{len(audit)} suspects | {auto} AUTO | {changed} corrections | {len(audit)-changed} UNCHANGED")

if __name__ == '__main__':
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
