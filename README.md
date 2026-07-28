# Daily Colonist 1925 — Dual-Pipeline OCR Testbed (Phase 1)

Dual-pipeline OCR testbed for the 1925 *Daily Colonist* (6,647 newspaper pages): Tesseract 5 and the PaddleOCR-VL vision-language model on identical microfilm scans, indexed in Solr with word-level highlighting, compared side by side in a IIIF/Mirador viewer, plus AI-generated image descriptions that make photos, ads, illustrations, and other visual elements searchable.

**Institution:** University of Victoria Libraries
**Author:** Corey Davis
**License:** MIT License 

**In-depth documentation:** [How word-level bounding boxes are generated from the VLM's block-level output](docs/phase1-converter-technical-note.md) — the complete method, the rationale for each design decision, error characteristics, and annotated source.

> **AI-assistance disclosure:** substantial portions of this codebase were written in AI-pair-development sessions with Claude Fable (Anthropic), under a verify-before-acting protocol. All code was executed and verified by the human author.

---

## What this is

The same 6,647 pages of digitized microfilm (312 issues, the complete year 1925 of *The Daily Colonist*, Victoria, B.C.) were processed by two independent OCR pipelines:

- **Tesseract 5** (classical OCR): word-level bounding boxes and per-word confidence, but silent dropout of degraded regions.
- **PaddleOCR-VL** (a ~0.9B-parameter vision-language model, served via vLLM): markedly better transcription, but block-level geometry only and no confidence reporting. A second model (Qwen2.5-VL-7B) generated natural-language descriptions of every image region — 39,458 descriptions — making visual elements searchable.

Both outputs were converted to MiniOCR, indexed in Apache Solr with the [solr-ocrhighlighting](https://github.com/dbmdz/solr-ocrhighlighting) plugin, and served through a Flask comparison viewer (IIIF Image API via Cantaloupe, Mirador 3) that shows both arms' readings of any page side by side, with search hits highlighted on the page image.

**Key numbers**: Tesseract indexed 19.6M words with 748K unique forms; the VLM arm 26.8M words with 257K unique forms — at corpus scale, OCR noise appears as vocabulary inflation. For example, the query *railway* matches 731 pages in the Tesseract arm and 2,7xx in the VLM arm (a count later found to be partially inflated by image-description text — see `analysis/check_*.py`).

## What this is not

- **Not a turnkey package.** These are working scripts from a research project, with machine-specific paths (`~/solr-bridge/...`), no argument parsing beyond what the work needed, and no test suite. They are published for transparency and reuse of the *approach*, not `pip install`.
- **Not the corpus.** Page images are not included. The scans are publicly available in the Internet Archive's [dailycolonist collection](https://archive.org/details/dailycolonist) (item identifiers like `dailycolonist0125uvic_9`); `pipeline/decode_year.py` fetches and decodes them.

## Repository layout

```
pipeline/      the production pipeline, in run order
analysis/      corpus statistics and the findability investigations
experiments/   attempted approaches that did not ship (kept for the record)
demo/          the side-by-side comparison viewer (Flask, port 8888)
docs/          the converter technical note and project documentation
solr-config/   the Solr core configuration (schema + solrconfig) used
sample_data/   small data files (issue dates; sample model output)
```

## Prerequisites

- **Python 3.11** (the project used a micromamba env, `paddle_env`)
- **Tesseract 5.x** on PATH
- **Docker** for: Solr 9 + solr-ocrhighlighting plugin; Cantaloupe IIIF image server
- **A GPU host** for the VLM arm (the project used one NVIDIA RTX 6000 Ada, 48 GB) running:
  - PaddleOCR-VL served via vLLM (OCR arm)
  - Qwen2.5-VL-7B-Instruct served via vLLM (image descriptions)
- Python packages: `requests`, `flask`, `pillow`, `numpy`, plus `symspellpy` (experiments only)

Container launch recipes and port assignments are documented in `docs/`. Default ports: Solr 8983, Cantaloupe 8182, demo viewer 8888; VLM servers 8110 (OCR) and 8120 (describer). The `solr-config/` directory contains the actual core configuration used (schema, solrconfig, the 1925 synonyms file); note that the core additionally requires the solr-ocrhighlighting **plugin jar** in its `lib/` directory — a binary not committed here; download it from the [plugin's releases page](https://github.com/dbmdz/solr-ocrhighlighting/releases).

## The pipeline, in run order

Each script states its inputs and outputs at the top. Paths are the project's (`~/paddle-year/`, `~/tess5-1925-full/`, `~/solr-bridge/`); adapt before running.

| # | Script | What it does |
|---|--------|--------------|
| 1 | `pipeline/decode_year.py` | Fetches/decodes the year's JPEG 2000 scans to full-resolution PNGs, one directory per issue. |
| 2 | `pipeline/tesseract_ocr_year.py` | Runs Tesseract 5 over every page → per-page TSVs (word boxes + confidence). ~14 h CPU for the year. |
| 3 | `pipeline/run_year.py` | The VLM year driver: streams every page through the PaddleOCR-VL server, writes per-page JSON, and invokes the converter with each page's own coordinate scale (`page_width / 2560`). |
| 4 | `pipeline/describe_images.py` | Sends every image region the layout model found to Qwen2.5-VL → `*_described.json` (descriptions merged into the page JSON). OCR + descriptions together: ~30 h on one GPU for the year. |
| 5 | `pipeline/paddle_to_miniocr.py` | Converts a page's VLM JSON to MiniOCR, generating per-word boxes from block geometry. **Fully documented in `docs/phase1-converter-technical-note.md`** — the design rationale, the error characteristics, and the complete annotated source. |
| 6 | `pipeline/reconvert_year.py` | Re-runs the converter across the whole year (used after converter fixes; safe to re-run). |
| 7 | `pipeline/reindex_all.py` | Posts both arms' MiniOCR into the Solr core (documents carry a `source` field: `tesseract` / `paddleocr-vl`). |
| 8 | `pipeline/reindex_vlm.py` | Reindexes only the VLM arm (faster iteration after reconversion). |

## Analysis scripts

| Script | What it does |
|--------|--------------|
| `analysis/build_corpus_stats.py` | Computes the two-arm corpus statistics (word counts, unique forms, lexicon-recognition rates, top suspect forms) into the JSON the demo's statistics page serves. |
| `analysis/sample_extract.py` | Per-page extraction of both arms' text with lexicon analysis; used for sampled comparisons. |
| `analysis/query_battery.py` | Runs a battery of findability queries against both arms and tabulates pages-with-match. |
| `analysis/check_matches.py`, `check_all_matches.py`, `check_all_matches_v2.py`, `check_tess_only.py`, `check_tess_only_v2.py` | The investigation (July 10) into why the VLM arm's counts exceeded the Tesseract arm's: these scripts separate matches in printed text from matches occurring only in AI-generated image descriptions, and identified the description-conflation issue that Phase 2's architecture later corrected. Kept as the record of that finding. |

A note on the lexicon: `lexicon_1925.tsv` (word frequencies used for "recognized vocabulary" rates) was built from the Tesseract arm's own output. It therefore contains Tesseract's errors and slightly favors Tesseract in any rate computed against it. The statistics pages disclose this; treat the lexicon as a *comparable measuring stick across arms*, not an authority.

## Experiments (kept for the record)

Three approaches were tried and did not ship.

- **`experiments/chandra_ocr_year.py`, `chandra_one_page.py`** — an earlier attempt to use Chandra as the VLM arm. Whole-page decoding proved problematic, and an attempted repair using Surya for layout segmentation was unsuccessful on dense broadsheet (it segmented pages as a grid rather than by columns). The project moved to PaddleOCR-VL's integrated layout-then-recognize pipeline.
- **`experiments/rapl_sampler.py`** — a CPU-energy measurement tool (Intel RAPL counters) designed to run beside the Tesseract job and produce a measured energy figure for the classical arm. The protocol is sound and the tool works; no output from the production run survives. Rerunning a sampled hour under this tool would produce a measured figure.
- **`experiments/correct_text.py`, `correct_apply.py`** — an early post-OCR correction experiment (SymSpell against the 1925 lexicon) applied to VLM output. Abandoned; the project's direction moved from correcting transcripts toward re-reading and fusing them.

## The demo viewer

`demo/app.py` (Flask, port 8888) serves: full-text search across both arms with per-arm match counts and badges ("both / Tesseract only / AI only"); a side-by-side Mirador comparison view of any page with hits highlighted via IIIF; a publication-calendar browser; corpus statistics; and search tips (Lucene syntax: phrases, AND/OR/NOT, wildcards, fuzzy, date ranges). Requires the Solr core and Cantaloupe running with the year's images and indexes in place.

```bash
python3 demo/app.py   # serves http://localhost:8888
```

## Caveats

1. **Word boxes in the VLM arm are estimates.** The model reports block-level geometry only; per-word boxes are generated by proportional distribution (`docs/phase1-converter-technical-note.md` documents the method and its five error modes). Highlights may be imperfect; retrieval is unaffected.
2. **The VLM arm's indexed text includes AI-generated image descriptions.** This inflates its apparent match counts for some queries (discovered by `analysis/check_*.py`). Description text is labeled in the demo but shares the text index.
3. **No ground truth exists.** All quality measures are correlates (dictionary rates, confidence distributions, cross-arm comparison), not character-error rates.
4. **Paths, ports, and model endpoints are the project machine's.** Expect to edit constants before anything runs elsewhere.

## Acknowledgements

Scans digitized by the University of Victoria Libraries; hosted by the Internet Archive.
