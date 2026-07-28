import json, os, subprocess, sys, tempfile, time, zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed

HOME = os.path.expanduser('~')
JP2Z = f"{HOME}/dailycolonist-1925-jp2z"
IMG_ROOT = f"{HOME}/colonist-images"
WORKERS = 10

def decode_one(args):
    issue, name, idx, zpath = args
    page = f"{issue}_p{idx:03d}"
    png = f"{IMG_ROOT}/{issue}/{page}.png"
    if os.path.exists(png) and os.path.getsize(png) > 1_000_000:
        return (issue, page, "skip")
    try:
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zpath) as z:
                src = z.extract(name, td)
            r = subprocess.run(["opj_decompress", "-i", src, "-o", png],
                               capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(png):
                return (issue, page, f"FAIL: {r.stderr[:150]}")
        return (issue, page, "ok")
    except Exception as e:
        return (issue, page, f"FAIL: {e}")

def main():
    issues = [l.strip() for l in open(f"{HOME}/ids-1925.txt") if l.strip()]
    tasks = []
    for issue in issues:
        zpath = f"{JP2Z}/{issue}/{issue}_jp2.zip"
        if not os.path.exists(zpath):
            print(f"MISSING ZIP: {issue}"); continue
        os.makedirs(f"{IMG_ROOT}/{issue}", exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            jp2s = sorted(n for n in z.namelist() if n.lower().endswith('.jp2'))
        for i, name in enumerate(jp2s, 1):
            tasks.append((issue, name, i, zpath))
    print(f"{len(tasks)} pages across {len(issues)} issues, {WORKERS} workers")
    t0, done, fails = time.time(), 0, []
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed(ex.submit(decode_one, t) for t in tasks):
            issue, page, status = fut.result()
            done += 1
            if status.startswith("FAIL"):
                fails.append((page, status))
            if done % 200 == 0:
                rate = done / (time.time() - t0)
                print(f"{done}/{len(tasks)} ({rate:.1f} p/s, "
                      f"ETA {(len(tasks)-done)/rate/3600:.1f}h), fails: {len(fails)}",
                      flush=True)
    print(f"DONE: {done} pages, {len(fails)} failures in {(time.time()-t0)/3600:.1f}h")
    for p, s in fails: print(" ", p, s)

if __name__ == '__main__':
    main()
