import argparse, json, os, shutil, subprocess, sys, tempfile, time, zipfile
import cv2, requests
from paddleocr import PaddleOCRVL

HOME = os.path.expanduser('~')
JP2Z = f"{HOME}/dailycolonist-1925-jp2z"
IMG_ROOT = f"{HOME}/colonist-images"
OUT_ROOT = f"{HOME}/paddle-year"
OCR_DATA = f"{HOME}/solr-bridge/ocr-data/vlm"
QUARANTINE = f"{OUT_ROOT}/quarantine.jsonl"
SOLR = "http://localhost:8983/solr/colonist/update?commit=true"

def log_quarantine(issue, page, err):
    os.makedirs(OUT_ROOT, exist_ok=True)
    with open(QUARANTINE, 'a') as f:
        f.write(json.dumps({"issue": issue, "page": page, "err": str(err)[:300],
                            "t": time.strftime("%F %T")}) + "\n")

def decode_issue(issue):
    """Unzip + opj_decompress all pages -> full-res PNGs. Returns sorted page list."""
    img_dir = f"{IMG_ROOT}/{issue}"
    os.makedirs(img_dir, exist_ok=True)
    zp = f"{JP2Z}/{issue}/{issue}_jp2.zip"
    with zipfile.ZipFile(zp) as z:
        jp2s = sorted(n for n in z.namelist() if n.lower().endswith('.jp2'))
        pages = []
        with tempfile.TemporaryDirectory() as td:
            for i, name in enumerate(jp2s, 1):
                page = f"{issue}_p{i:03d}"
                png = f"{img_dir}/{page}.png"
                pages.append((page, png))
                if os.path.exists(png):
                    continue
                src = z.extract(name, td)
                r = subprocess.run(["opj_decompress", "-i", src, "-o", png],
                                   capture_output=True, text=True)
                if r.returncode != 0 or not os.path.exists(png):
                    raise RuntimeError(f"decode failed {name}: {r.stderr[:200]}")
    return pages

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    issues = [l.strip() for l in open(f"{HOME}/ids-1925.txt") if l.strip()]
    if args.only: issues = [args.only]
    if args.limit: issues = issues[:args.limit]

    os.makedirs(OCR_DATA, exist_ok=True)
    pipeline = PaddleOCRVL(vl_rec_backend="vllm-server",
                           vl_rec_server_url="http://localhost:8110/v1",
                           vl_rec_api_model_name="paddleocr-vl")

    for issue in issues:
        out_dir = f"{OUT_ROOT}/{issue}"
        done = f"{out_dir}/.done"
        if os.path.exists(done):
            print(f"{issue}: done, skipping", flush=True); continue
        os.makedirs(out_dir, exist_ok=True)
        t_issue = time.time()
        try:
            pages = decode_issue(issue)
        except Exception as e:
            log_quarantine(issue, "*", f"decode: {e}"); print(f"{issue}: DECODE FAILED"); continue

        docs, ok, failed = [], 0, 0
        for page, png in pages:
            desc_json = f"{out_dir}/{page}_described.json"
            xml_out = f"{OCR_DATA}/{page}.miniocr.xml"
            try:
                if not os.path.exists(desc_json):
                    big = cv2.imread(png)
                    if big is None: raise RuntimeError("cv2 failed to read png")
                    scale = big.shape[1] / 2560.0
                    small = f"{out_dir}/{page}_small.png"
                    if not os.path.exists(small):
                        s = 2560 / big.shape[1]
                        cv2.imwrite(small, cv2.resize(big, None, fx=s, fy=s,
                                                      interpolation=cv2.INTER_AREA))
                    pdir = f"{out_dir}/paddle_{page}"
                    os.makedirs(pdir, exist_ok=True)
                    raw_json = f"{pdir}/{page}_small_res.json"
                    if not os.path.exists(raw_json):
                        for res in pipeline.predict(small):
                            res.save_to_json(save_path=pdir)
                    r = subprocess.run([sys.executable,
                        f"{HOME}/solr-bridge/describe_images.py",
                        raw_json, png, desc_json, str(scale)],
                        capture_output=True, text=True, timeout=1200)
                    if r.returncode != 0: raise RuntimeError(f"describer: {r.stderr[-200:]}")
                if not os.path.exists(xml_out):
                    big_w = cv2.imread(png).shape[1]
                    r = subprocess.run([sys.executable,
                        f"{HOME}/solr-bridge/paddle_to_miniocr.py",
                        desc_json, page, xml_out, str(big_w / 2560.0)],
                        capture_output=True, text=True)
                    if r.returncode != 0: raise RuntimeError(f"convert: {r.stderr[-200:]}")
                docs.append({"id": f"{page}_paddle", "page_id": page,
                             "source": "paddleocr-vl",
                             "ocr_text": f"/ocr-data/vlm/{page}.miniocr.xml"})
                ok += 1
            except Exception as e:
                failed += 1
                log_quarantine(issue, page, e)
        if docs:
            r = requests.post(SOLR, json=docs)
            if r.status_code != 200:
                log_quarantine(issue, "*", f"solr index: {r.text[:200]}")
        with open(done, 'w') as f:
            f.write(json.dumps({"pages": len(pages), "ok": ok, "failed": failed,
                                "seconds": round(time.time() - t_issue)}))
        print(f"{issue}: {ok}/{len(pages)} pages ok, {failed} quarantined, "
              f"{time.time()-t_issue:.0f}s", flush=True)

if __name__ == '__main__':
    main()
