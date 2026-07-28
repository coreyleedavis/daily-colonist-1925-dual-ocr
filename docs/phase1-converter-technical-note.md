# Word Geometry Generation in the Phase 1 PaddleOCR-VL → MiniOCR Converter

**Technical note — *The Daily Colonist* 1925 search project, Phase 1**
**Subject:** `paddle_to_miniocr.py` (~90 lines; complete listing in Appendix A)

---

## Summary

This script converts the output of a vision-language OCR model (PaddleOCR-VL) into MiniOCR, the XML format read by the project's Solr search index. The conversion exists to solve one mismatch: the search index requires a position box for every individual word so that matches can be highlighted on the page image, but the model reports only one box per block — a paragraph, headline, or table — with the block's text as a single string. The script therefore generates word positions that the model never produced.

For each block, it works in four steps:

1. **Select.** Every block is converted except printed page numbers, which would add noise to numeric searches.
2. **Clean.** The block's text is stripped of HTML table markup, em/en dashes are replaced with spaces, and characters that cannot occur in a 1925 newspaper (emoji-range Unicode) are removed.
3. **Reconstruct lines.** The block's words are arranged into lines. If the model's own line breaks imply physically plausible line heights (30–140 pixels), they are used; otherwise a line count is estimated from the block's height at 62 pixels per line, the measured line height of Colonist body text, and words are divided evenly among the lines by character count. Words split across lines by a printed hyphen are rejoined.
4. **Place words.** Each line's width is divided among its words in proportion to their character counts, as if all characters were equal width, and each word is written out with the resulting box.

The generated positions are estimates, not measurements. They are accurate to roughly one word-width in ordinary body text and degrade predictably in display type (Section 5 catalogues the five error modes). The errors affect only where the highlight is drawn on the page image — every word is indexed and findable regardless. The design consistently prefers finding text over placing it precisely.

---

## 1. The problem this converter solves

The project's search index is Apache Solr with the **solr-ocrhighlighting** plugin (dbmdz, Munich Digitization Centre, Bavarian State Library) [1]. The plugin returns pixel coordinates for matched words, so search results can be displayed as highlight rectangles on the page image in a IIIF viewer. This capability has a strict input requirement, stated in the plugin's installation documentation: OCR documents must be supplied "in hOCR, ALTO or MiniOCR formats, with **at least page-, and word-level segmentation**" [2]. Without word-level boxes, highlighting is not possible.

PaddleOCR-VL, the vision-language model that produces the AI arm's transcription, does not emit word geometry. Its output unit is the **block**: a JSON record per detected layout region carrying a label (`text`, `title`, `table`, `image`, `number`, …), the region's transcribed text as one string, and a single bounding rectangle for the whole region. This converter bridges the gap between the plugin's per-word requirement and the model's per-block output. Its function is to generate per-word geometry from block-level data: for every word, a position that is correct at the block level, approximately correct at the line level, and estimated at the word level. The synthesized boxes are estimates by construction; this note documents exactly how they are estimated and why each estimation choice was made.

This requirement is common. Any pipeline pairing a region-level recognizer with a word-level display or search layer faces the same gap, and interpolating finer geometry from coarser geometry is an established practice in digitization work. This document provides a precise account of how that interpolation is done here.

## 2. The input contract

Per page, the converter reads one PaddleOCR-VL JSON file. The relevant structure:

```json
{
  "width": 2560,
  "height": 3275,
  "parsing_res_list": [
    {
      "block_label": "text",
      "block_content": "The barometer remains stationary over this Province...",
      "block_bbox": [508, 930, 840, 1199],
      "block_id": 7
    }
  ]
}
```

Three properties of this contract drive the design:

- **Coordinates live in a model-native space**, 2,560 pixels wide with height scaled proportionally (the example page is 2560 × 3275 for a source scan of roughly 7,376 × 9,437). The converter must rescale into the full-resolution pixel space of the served page images, because the index's coordinates are consumed directly by the viewer against those images.
- **`block_content` is a single string containing the block's entire text.** Beyond ordinary words, four kinds of content appear in it, and the converter must handle each:
    - **Newline characters.** The model often reproduces the visual line breaks it sees on the page, so a multi-line paragraph arrives with `\n` between lines. Sometimes it does not, and a paragraph arrives as one long line.
    - **HTML table markup.** For blocks labeled `table`, the model transcribes the table's structure as literal HTML: `<table><tr><td>…`. These tags are part of the string, not separate metadata.
    - **Em and en dashes** (— and –), which 1925 typesetting uses heavily as separators, e.g. "VANCOUVER, June 16.—Rains…".
    - **Rare invalid characters.** The model occasionally emits characters that no 1925 newspaper contains: characters outside Unicode's Basic Multilingual Plane (code points above U+FFFF, such as emoji and rare symbols) and the emoji variation selector U+FE0F (an invisible character that modifies how a preceding symbol displays). These are generation artifacts — the model's output vocabulary covers many languages and symbol sets, and it occasionally produces tokens from parts of that vocabulary that have no business in this text.
- **`block_bbox` is `[x0, y0, x1, y1]`** — corner coordinates, not x/y/width/height.

## 3. The output contract

The target is **MiniOCR**, the plugin's own minimal format [3]:

```xml
<ocr>
  <p xml:id="page_identifier">
    <b>
      <l><w x="50 50 100 100">A</w> <w x="150 50 100 100">Line</w></l>
    </b>
  </p>
</ocr>
```

`<p>` is a page, `<b>` a block, `<l>` a line, and `<w x="x y w h">` a word with its box. The plugin accepts three input formats: hOCR, ALTO, and MiniOCR. MiniOCR was chosen for two reasons.

First, simplicity of generation. hOCR and ALTO are full document-description standards with many elements and optional attributes; writing a correct emitter for either means making decisions about attributes this project does not need and getting namespace and structural details exactly right. In MiniOCR, a word is completely described by one element: four numbers (x, y, width, height) and the word's text. The entire emitter for this project is about ten lines of code, and there are no optional attributes to get wrong.

Second, search-time speed. The plugin re-reads the OCR file every time it builds a highlighted result, so the cost of parsing the format is paid on every search, not once at indexing. The plugin maintainer's benchmarks show MiniOCR highlighting is roughly 25% faster than ALTO and 50% faster than hOCR [3]. Across 6,647 pages and millions of words, with every search paying this cost, the faster format is the correct default.

## 4. The pipeline, stage by stage

The converter is four conceptual stages: select, sanitize, reconstruct lines, distribute words. Each is shown with its verbatim code and its rationale.

### 4.1 Block selection

```python
SKIP_LABELS = {'number'}
...
for b in j['parsing_res_list']:
    if b['block_label'] in SKIP_LABELS:
        continue
```

**Decision: index everything except page-number blocks.** The exclusion list has exactly one entry: `number`. The layout model applies this label to printed page numbers — the small "12" or "TWELVE" that appears at the top of every page. These are excluded because indexing them would be harmful: page numbers occur on every page, so a search for any small number would match nearly the whole year, and none of those matches would tell the searcher anything.

Every other block type is included: body text, headlines, tables, and image regions that carry text. The reasoning is that the two kinds of error this decision can produce are not equally bad. Including a block that should have been left out produces a false match — an inconvenience the searcher can recognize and dismiss by looking at the page. Excluding a block that should have been included makes its text unfindable, and the searcher never learns it existed. Newspaper digitization research supports weighting the errors this way: what damages research use is mainly content that cannot be found, not extra matches that must be skimmed past [4]. When in doubt, this converter indexes.

### 4.2 Text sanitization

```python
text = (block.get('block_content') or '').strip()
text = re.sub(r'</?(?:table|tr|td)[^>]*>', ' ', text)
text = text.replace('\u2014', ' ').replace('\u2013', ' ')
text = ''.join(c for c in text if ord(c) <= 0xFFFF and ord(c) != 0xFE0F).strip()
```

Three cleanups, each answering a specific observed artifact of generative OCR output:

- **Table markup stripped to spaces.** In `table`-labeled blocks the model transcribes structure as literal HTML. Left in place, the tags would be indexed as tokens (`td`, `tr` becoming searchable "words") and would appear inside highlight snippets. Replacing with spaces preserves cell-content word boundaries while deleting the scaffolding.
- **Em and en dashes become spaces.** In 1925 newspaper typesetting the em dash is a common separator ("VANCOUVER, June 16.—Rains…"). Converting to a space guarantees the tokens on either side separate cleanly regardless of how the downstream analyzer treats punctuation, so "16.—Rains" cannot fuse into an unsearchable token.
- **Invalid-character filter.** Two things are dropped: any character with a Unicode code point above U+FFFF, and the emoji variation selector U+FE0F. The reasoning has two parts.

    First, these characters cannot be correct. Unicode's first 65,536 code points (U+0000 through U+FFFF, called the Basic Multilingual Plane) cover every character that appears in an English-language 1925 newspaper, with a large margin. Characters above that range — emoji, rare historic scripts, specialized symbols — and U+FE0F, an invisible character whose only function is to change how an adjacent emoji displays, can only be generation mistakes. As noted in §2, the model's output vocabulary spans many languages and symbol sets, and it occasionally emits tokens from parts of that vocabulary that do not belong in this text.

    Second, keeping them carries risk further down the pipeline. Solr and the highlighting plugin are Java software, and Java represents text internally in UTF-16, an encoding in which every character above U+FFFF takes two storage units instead of one (a "surrogate pair"). Software that counts positions in text — which is exactly what a highlighting plugin does when it computes where a match starts and ends — must handle these two-unit characters specially, and mistakes in that handling are a well-known category of bug. Removing characters that are certainly wrong anyway eliminates the possibility of triggering such a bug. The filter costs nothing (no legitimate content is lost) and removes a class of failure that would be difficult to diagnose if it occurred.

### 4.3 Coordinate scaling

```python
SCALE = 2.88125
...
x1, y1, x2, y2 = [c * SCALE for c in block['block_bbox']]
```

and, in the CLI entry point:

```python
if len(sys.argv) > 4:
    globals()['SCALE'] = float(sys.argv[4])
```

**Decision: multiply all coordinates by one number, settable per page.**

The scaling problem is this. The model reports block positions in its own coordinate system: the page as the model saw it, resized to 2,560 pixels wide. The index needs positions in the coordinate system of the full-resolution page image, which for these scans is roughly 7,400–7,600 pixels wide. Converting between the two means multiplying every coordinate by the ratio between the two widths.

One multiplier works for both the horizontal and vertical coordinates because the model's resize preserves the page's proportions: the height is reduced by the same factor as the width. If the resize distorted the proportions, horizontal and vertical coordinates would need different multipliers; it does not, so a single number suffices.

The code ships with a default value, `SCALE = 2.88125`. This corresponds to a page 2,560 × 2.88125 = 7,376 pixels wide. But actual page widths vary — across the 1925 scans, from about 7,376 to about 7,590 pixels — so no single constant is correct for every page. For this reason the value can be supplied on the command line, and the year-long production run did exactly that: the driver script (`run_year.py`) computes each page's own ratio (`big_w / 2560.0`) and passes it as the converter's fourth argument, so every page was converted at its own measured scale. The constant serves only as a fallback for standalone invocations.

Supplying the correct per-page ratio matters more than anything else in this converter, because a scale error affects every box on the page and grows with distance from the origin. The arithmetic: if the multiplier is 2% too small, a box that should sit at x = 7,400 is placed at x ≈ 7,250 — about 150 pixels short. That single error is larger than any error introduced by the line reconstruction or word-placement steps that follow.

### 4.4 Line reconstruction: trust, then synthesize

A block is typically many lines tall, and a word box needs a vertical position, so words must first be assigned to lines. The converter uses a two-path strategy with an explicit plausibility gate:

```python
explicit = [ln.strip() for ln in text.split('\n') if ln.strip()]
# If explicit breaks give plausible line heights (30-140px at full res), trust them.
if len(explicit) > 1 and 30 <= h / len(explicit) <= 140:
    line_groups = [ln.split() for ln in explicit]
    ...
else:
    # Reflowed block: synthesize line count from typical 1925 body line height (~62px).
    n_lines = max(1, round(h / 62))
    line_groups = wrap_words(text.split(), n_lines)
```

**Path 1 — use the model's own line breaks, after checking them.**

The model often includes a newline character wherever a line ended on the printed page. When it does, those newlines are the best available information about line structure, because they come from the model's reading of the actual page image. But the model is not consistent: sometimes it emits a whole paragraph as one long line with no breaks, and sometimes it inserts more breaks than the page has lines. The converter therefore needs a test that separates trustworthy breaks from untrustworthy ones.

The test works as follows. If the block really has as many lines as the newlines claim, then each line occupies a predictable share of the block's height: the block height divided by the line count. The converter computes that implied per-line height and accepts the breaks only if it falls between 30 and 140 pixels.

Those two numbers come from the physical page. The scans are approximately 7,400 pixels across a broadsheet page about 17 inches wide, which works out to roughly 435 pixels per inch. Printers measure type in points, where one point is 1/72 of an inch — so at this resolution, one point is about 6 pixels. The bounds therefore correspond to line heights of about 5 points (30 px) and about 23 points (140 px). Five points is the line spacing of the smallest classified-ad type in the paper; 23 points is a large subheading. Any claimed line count that implies lines shorter or taller than that range describes something no 1925 newspaper printed, so the breaks are judged unreliable and the converter falls through to Path 2.

**Path 2 — estimate the line count from the block's height.**

When newlines are absent or failed the test above, the line count is estimated as the block's height divided by 62 pixels, rounded (`round(h / 62)`). The number 62 is an empirical constant: it is the typical line height of Colonist body text, measured from these scans. At this scan resolution, 62 pixels corresponds to line spacing of about 10 points, which is consistent with the small body type and tight line spacing characteristic of early-twentieth-century broadsheet newspapers [5].

With a line count in hand, the block's words must be divided among the lines. `wrap_words` does this by filling lines to an equal share of the block's total characters (`total_chars / n_lines`) rather than an equal share of its words. Characters are the right unit because a printed line holds a roughly fixed number of characters, while words vary in length — a line of ten short words and a line of five long ones occupy similar space:

```python
def wrap_words(words, n_lines):
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
```

One detail of the code needs explanation: the condition `len(lines) < n_lines - 1`. It stops the function from closing off a new line once all but the last of the planned lines exist, which forces any remaining words onto the final line. Without it, rounding in the character budget could produce one line more than planned. The guarantee matters because the next step assigns each line a vertical position by dividing the block's height into exactly `n_lines` equal bands — an extra, unplanned line would have no band to occupy.

**Assigning vertical positions.** Whichever path produced the lines, they are placed the same way. The block's height is divided into as many equal horizontal strips as there are lines (`band_h = h / len(line_groups)`), and line *i* is assigned strip *i*: its top edge at `y1 + i·band_h`, its height one strip, its width the full width of the block.

This placement assumes every line in the block is equally tall. That is not always true — line spacing varies within a block, for example around an embedded heading — but the converter has no information about individual line heights, so equal division is the only model available. The cost is bounded: a line can be displaced vertically by at most the accumulated difference between real and assumed spacing, which for ordinary text blocks is a fraction of one line height.

**Rejoining words split across lines.** Printed newspapers routinely break a word at the end of a line with a hyphen: `six-` at the end of one line, `room` at the start of the next. When the model preserves line breaks, such a word arrives as two fragments on two lines, and neither fragment alone is the word a person would search for. Path 1 therefore applies a repair. (Only Path 1 can: the repair needs to know where the printed line breaks were, and only on that path does the converter have them.)

```python
for j in range(len(line_groups) - 1):
    a, b = line_groups[j], line_groups[j + 1]
    if (a and b and re.fullmatch(r"[A-Za-z][A-Za-z']*-", a[-1])
            and re.fullmatch(r"[a-z][A-Za-z'-]*", b[0])):
        a[-1] = a[-1] + b.pop(0)
```

The rule: when the last word of a line consists of letters ending in a hyphen, and the first word of the next line is a fragment beginning with a lowercase letter, the two fragments are merged into one word — and the hyphen is kept: `six-` + `room` becomes `six-room`, not `sixroom`.

Keeping the hyphen is a choice between two kinds of split that the converter cannot tell apart:

- The printed word was a **genuine hyphenated compound**, and the typesetter broke it at its own hyphen: "six-room house." Here the correct restoration is `six-room`. Removing the hyphen would produce `sixroom`, a word that matches nothing.
- The printed word was an **ordinary word** the typesetter broke with an inserted hyphen: "rail-" / "way" for *railway*. Here the correct restoration is `railway`, and keeping the hyphen produces the imperfect `rail-way`.

Keeping the hyphen is right in the first case and tolerable in the second, because the search analyzer's handling of punctuation lets `rail-way` still match a search for *railway*. Removing the hyphen would be right in the second case but destructive in the first, since `sixroom` is unrecoverable by any search. The converter therefore always keeps it.

The two guard conditions limit the rule to clear cases: the first fragment must be letters followed by a hyphen (not a number, not stray punctuation), and the continuation must begin with a lowercase letter — because the continuation of a split word is lowercase, while a word that merely happens to start the next line, such as a new sentence or a proper name, is usually capitalized. Anything outside those conditions is left untouched; as the comment in the source puts it, "any doubt, leave both fragments alone." 

### 4.5 Horizontal distribution: the character-proportional model

Within a line, the width is divided among the words in proportion to character count:

```python
def words_in_line(words, lx, ly, lw, lh):
    total_chars = sum(len(w) for w in words) + max(0, len(words) - 1)
    out, cursor = [], lx
    for w in words:
        frac = (len(w) + 1) / total_chars
        ww = lw * frac
        out.append((w, cursor, ly, min(ww, lx + lw - cursor), lh))
        cursor += ww
    return out
```

The model: treat the line as if set in a uniform-width typeface, with one inter-word space per boundary. `total_chars` counts every letter plus `n−1` spaces; each word's share of the line width is `(len(word)+1)/total_chars` — its letters plus a trailing space — and words are laid left to right at the running cursor. Two deliberate details:

- **The trailing space is folded into each word's box** rather than left as inter-word gaps. For a highlighting application this is the preferable bias: boxes that extend into the following gap produce visually continuous highlights on multi-word matches, whereas boxes with gaps between them appear fragmented. The cost is that every box is approximately one character too wide on its right edge.
- **The last word also receives a space credit**, so the fractions sum to slightly more than 1 (`total+1`/`total`). The overrun is absorbed by the emission clamp `min(ww, lx + lw − cursor)`, which truncates the final box at the line's right edge rather than letting it protrude past the block. The asymmetry (clamp at the end, not proportional shrink throughout) keeps the arithmetic simple and confines the distortion to the last word's right edge — sub-pixel to a few pixels in practice.

Why divide by character count? The method treats every character as if it were the same width. That is false for printed type — an *i* is narrower than a *w*, and a capital *M* is wider than either — so the question is whether the errors this introduces are small enough to live with.

They are, for two reasons. First, the errors partly cancel. A word's predicted position depends on the total width of all the characters before it on the line, and in a line of ordinary text, narrow and wide characters are mixed throughout — so overestimates and underestimates of individual character widths largely offset each other by the time the running total reaches any given word. In practice, in an ordinary justified newspaper column, the method places each word within roughly one word-width of where it actually sits. Second, that accuracy is enough for the purpose. These positions exist to draw a highlight that shows a reader where on the page image a match is; a highlight that lands on or immediately beside the right word does that job. The positions are approximations for display, not measurements, and the situations where the approximation breaks down are known and described in §5.

### 4.6 Emission

```python
parts.append(f'<w x="{wx:.0f} {wy:.0f} {ww:.0f} {wh:.0f}">{html.escape(w)}</w> ')
```

Coordinates are rounded to whole pixels (`:.0f`) — sub-pixel precision would be false precision for synthesized geometry, and integer coordinates are read by the plugin as absolute pixels, matching the viewer's expectations. Word text is `html.escape`d (1925 advertisements contain literal `&` and occasionally `<` from OCR'd ornaments; unescaped they would break the XML). The **trailing space after each `</w>`** is required: the plugin derives token separation from inter-element whitespace, and its absence fuses adjacent words at index time. Structure follows the MiniOCR nesting exactly — one `<b>` per surviving block, one `<l>` per reconstructed line — so block and line boundaries remain visible to the plugin's snippet-passage logic.

## 5. Error characteristics

The generated boxes are wrong in five specific ways. Each error follows directly from one of the assumptions described above — where the assumption holds, the boxes are close; where it fails, they are off in a way that can be predicted from which assumption failed. Understanding the five modes is understanding the converter's limits.

**1. Words drift sideways within a line.**
Cause: the equal-character-width assumption (§4.5). The predicted position of a word depends on the estimated width of everything before it on the line, so estimation error accumulates from left to right — a word early in the line is placed accurately, a word at the end carries the sum of every width error before it. The drift is largest where real character widths differ most from uniform: lines set in ALL CAPITALS (capitals are wide) and lines mixing very wide and very narrow letters. In ordinary body text the drift stays within about one word-width; in display type it can reach several word-widths.

**2. A whole block's words sit on the wrong lines.**
Cause: an incorrect line-count estimate on the synthesized path (§4.4, Path 2). If `round(h / 62)` yields one line too many or too few, every line in the block shifts by one band — each word's highlight lands on the line above or below its true position, for the entire block. This is the most noticeable error when it occurs, because it displaces everything at once rather than nudging individual words. The plausibility test on Path 1 exists to keep blocks on the trusted-breaks path, where this error cannot happen, as often as possible.

**3. Words drift where the typesetter stretched the spaces.**
Cause: the fixed-space assumption (§4.5 counts exactly one space's width per word gap). Newspaper columns are justified — the typesetter widened or narrowed the spaces on each line to make the right edge align — so real gaps vary while the model's do not. Mid-line words drift by the accumulated difference. In body text the effect is small, a few pixels per gap.

**4. Headline boxes are far too wide.**
Cause: the assumption that text fills its line's full width (§4.5 distributes the entire line width among the words). A centered headline of three words occupies perhaps a third of its block's width, but the model hands the three words the whole width — each box roughly three times too wide, extending into the empty margins. This is the largest single error the converter produces. It was accepted because it is also the least harmful in use: a reader looking for a three-word headline does not need a precise box to find it.

**5. Loose blocks make loose words.**
Cause: the converter starts from the model's block rectangle and subdivides it. If that rectangle is itself larger than the printed region — the layout model drew it with margin — every word box inside inherits a share of the slack. The converter has no way to detect or correct this; it can only be as accurate as the rectangle it was given.

Two facts put these errors in proportion. First, none of them affect whether text can be *found*: the words are indexed identically regardless of where their boxes sit, so search results are complete either way — only the position of the yellow highlight on the page image is affected. Second, the limitation is disclosed to users: the project's search-tips page states that matches "may highlight imperfectly on the page image." Approximate highlighting was the accepted cost of making a block-level transcription searchable at the word level at all.

## 6. Summary of design decisions

| Decision | Choice | Governing rationale |
|---|---|---|
| Target format | MiniOCR | Minimal word contract; fastest highlighting of the three supported formats [3] |
| Block filter | exclude `number` only | Recall-maximizing; printed page numbers are repetitive noise |
| Sanitization | strip table tags, dashes→space, BMP filter | Prevent markup-as-tokens; guarantee token separation; UTF-16 toolchain robustness |
| Scaling | uniform scalar, CLI-overridable | Aspect-preserving model space; per-page ratios vary and dominate error budget |
| Line source | model's breaks, gated by 30–140px plausibility | Use visual evidence when physically credible; reject reflow artifacts |
| Line synthesis | `round(h/62)`, character-balanced fill | 62px ≈ period body leading at scan resolution; characters govern print occupancy |
| Hyphen repair | merge, keep hyphen, exclude ambiguous cases | Non-destructive; compounds searchable as printed |
| Word placement | character-proportional with space credit | Best zero-evidence model; continuous multi-word highlights |
| Rounding | whole pixels | No false precision on synthesized geometry |

## References

[1] dbmdz (Munich Digitization Centre, Bavarian State Library). *solr-ocrhighlighting*. https://github.com/dbmdz/solr-ocrhighlighting

[2] *Solr OCR Highlighting Plugin — Installation.* "OCR documents need to be in hOCR, ALTO or MiniOCR formats, with at least page-, and word-level segmentation." https://dbmdz.github.io/solr-ocrhighlighting/

[3] *Solr OCR Highlighting Plugin — Supported Formats.* MiniOCR specification and format performance comparison. https://dbmdz.github.io/solr-ocrhighlighting/wip/formats/

[4] Holley, R. "How Good Can It Get? Analysing and Improving OCR Accuracy in Large Scale Historic Newspaper Digitisation Programs." *D-Lib Magazine* 15(3/4), 2009.

[5] Hutt, A. *The Changing Newspaper: Typographic Trends in Britain and America 1622–1972.* Gordon Fraser, 1973. [Standard history of newspaper typography; verify specific body-size discussion and page reference in revision.]

---

## Appendix A: complete source

```python
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
```
