import base64, json, os, sys, time
import cv2, requests

SCALE = 2.88125
SERVER = "http://localhost:8120/v1/chat/completions"
PROMPT = ("This is an illustration or photograph from a 1925 Canadian newspaper "
          "(The Daily Colonist, Victoria BC). Describe it in 2-3 sentences for a "
          "search index: the subject, any product or activity shown, and transcribe "
          "any text visible within the image. Be factual and specific.{caption_hint}")

def contained(a, b, thresh=0.7):
    """Fraction of box a's area inside box b."""
    ax1, ay1, ax2, ay2 = a; bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    area = max(1, (ax2 - ax1) * (ay2 - ay1))
    return (ix * iy) / area >= thresh

def find_caption(img_bbox, blocks):
    """Printed caption = vision_footnote starting within 120px below the image."""
    x1, y1, x2, y2 = img_bbox
    for b in blocks:
        if b['block_label'] == 'vision_footnote':
            bx1, by1, bx2, by2 = b['block_bbox']
            if 0 <= by1 - y2 <= 120 and min(x2, bx2) - max(x1, bx1) > 0:
                return (b.get('block_content') or '').strip()
    return None

def describe(crop, caption):
    ok, buf = cv2.imencode('.png', crop)
    b64 = base64.b64encode(buf).decode()
    hint = f"\nThe printed caption below this image reads: \"{caption}\"" if caption else ""
    r = requests.post(SERVER, json={
        "model": "describer",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": PROMPT.format(caption_hint=hint)},
        ]}],
        "max_tokens": 250, "temperature": 0.1,
    }, timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def main(json_path, image_path, out_path):
    global SCALE
    j = json.load(open(json_path))
    blocks = j['parsing_res_list']
    image = cv2.imread(image_path)
    img_blocks = [b for b in blocks if b['block_label'] == 'image']

    kept, seen = [], []
    for b in sorted(img_blocks, key=lambda b: -(b['block_bbox'][2]-b['block_bbox'][0])
                                              *(b['block_bbox'][3]-b['block_bbox'][1])):
        if any(contained(b['block_bbox'], s) for s in seen):
            b['block_content'] = ''   # duplicate/nested: leave empty, stays out of index
            continue
        seen.append(b['block_bbox']); kept.append(b)

    print(f"{len(img_blocks)} image blocks, {len(kept)} after dedup")
    for i, b in enumerate(kept):
        x1, y1, x2, y2 = [int(c * SCALE) for c in b['block_bbox']]
        crop = image[max(0,y1):y2, max(0,x1):x2]
        if crop.size == 0 or min(crop.shape[:2]) < 40:
            continue
        caption = find_caption(b['block_bbox'], blocks)
        t0 = time.time()
        try:
            desc = describe(crop, caption)
            b['block_content'] = desc
            print(f"\n--- image {i} ({x2-x1}x{y2-y1}px, caption={'yes' if caption else 'no'}, {time.time()-t0:.1f}s) ---")
            print(desc)
        except Exception as e:
            print(f"image {i} FAILED: {str(e)[:150]}")

    json.dump(j, open(out_path, 'w'), indent=1)
    print(f"\nWrote {out_path}")

if __name__ == '__main__':
    if len(sys.argv) > 4:
        globals()['SCALE'] = float(sys.argv[4])
    main(*sys.argv[1:4])
