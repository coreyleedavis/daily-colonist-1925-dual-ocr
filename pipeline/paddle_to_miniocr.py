import json, os, sys, html, re

SCALE = 2.88125
SKIP_LABELS = {'number'}

def wrap_words(words, n_lines):
    """Distribute words into n_lines runs, balanced by character count."""
    if n_lines <= 1:
        return [words]
    total = sum(len(w) + 1 for w in words)
    per_line = total / n_lines
    lines, cur, cur_len = [], [], 0
    for w in words:
        cur.append(w); cur_len += len(w) + 1
        if cur_len >= per_line and len(lines) < n_lines - 1:
            lines.append(cur); cur, cur_len = [], 0
    if cur: lines.append(cur)
    return lines

def block_to_lines(block):
    text = (block.get('block_content') or '').strip()
    text = re.sub(r'</?(?:table|tr|td)[^>]*>', ' ', text)
    text = text.replace('\u2014', ' ').replace('\u2013', ' ')
    text = ''.join(c for c in text if ord(c) <= 0xFFFF and ord(c) != 0xFE0F).strip()
    if not text:
        return []
    x1, y1, x2, y2 = [c * SCALE for c in block['block_bbox']]
    h = y2 - y1
    explicit = [ln.strip() for ln in text.split('\n') if ln.strip()]
    # If explicit breaks give plausible line heights (30-140px at full res), trust them.
    if len(explicit) > 1 and 30 <= h / len(explicit) <= 140:
        line_groups = [ln.split() for ln in explicit]
        # join line-break hyphens: last word ends in letters+hyphen, next line
        # starts with a lowercase letter-fragment; keep the hyphen (six-\nroom
        # -> six-room). Conservative: any doubt, leave both fragments alone.
        for j in range(len(line_groups) - 1):
            a, b = line_groups[j], line_groups[j + 1]
            if (a and b and re.fullmatch(r"[A-Za-z][A-Za-z']*-", a[-1])
                    and re.fullmatch(r"[a-z][A-Za-z'-]*", b[0])):
                a[-1] = a[-1] + b.pop(0)
    else:
        # Reflowed block: synthesize line count from typical 1925 body line height (~62px).
        n_lines = max(1, round(h / 62))
        line_groups = wrap_words(text.split(), n_lines)
    band_h = h / len(line_groups)
    out = []
    for i, words in enumerate(line_groups):
        if words:
            out.append((words, x1, y1 + i * band_h, x2 - x1, band_h))
    return out

def words_in_line(words, lx, ly, lw, lh):
    total_chars = sum(len(w) for w in words) + max(0, len(words) - 1)
    out, cursor = [], lx
    for w in words:
        frac = (len(w) + 1) / total_chars
        ww = lw * frac
        out.append((w, cursor, ly, min(ww, lx + lw - cursor), lh))
        cursor += ww
    return out

def convert(json_path, page_id):
    j = json.load(open(json_path))
    parts = [f'<ocr><p xml:id="{page_id}">']
    for b in j['parsing_res_list']:
        if b['block_label'] in SKIP_LABELS:
            continue
        lines = block_to_lines(b)
        if not lines:
            continue
        parts.append('<b>')
        for words, lx, ly, lw, lh in lines:
            parts.append('<l>')
            for w, wx, wy, ww, wh in words_in_line(words, lx, ly, lw, lh):
                parts.append(f'<w x="{wx:.0f} {wy:.0f} {ww:.0f} {wh:.0f}">{html.escape(w)}</w> ')
            parts.append('</l>')
        parts.append('</b>')
    parts.append('</p></ocr>')
    return ''.join(parts)

if __name__ == '__main__':
    if len(sys.argv) > 4:
        globals()['SCALE'] = float(sys.argv[4])
    json_path, page_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    xml = convert(json_path, page_id)
    open(out_path, 'w').write(xml)
    print(f"Wrote {out_path} ({len(xml)} bytes)")
