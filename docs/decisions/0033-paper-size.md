# 0033 — Paper size per build

- **Status:** Accepted
- **Date:** 2026-10-16
- **Issue:** #92
- **Amends:** ADR 0029 (template contract), ADR 0030 (US Letter as a hand edit), ADR 0007 (preference catalogue)

## Context
Every shipped template was A4. US Letter, which employers in the United States and Canada expect, needed a copy of the template with `a4paper` changed to `letterpaper` in `\documentclass`, added with `jaa templates add` (ADR 0030). #41 asked for both sizes, and an uploaded template could fix its paper anywhere, so no build option could be relied on.

## Decision
- **`PaperSize` enum** (`a4`, `letter`) in `jaa.core.models`. Other sizes (legal, A5) are not offered: no CV convention uses them.
- **Where the size comes from:** a per-build override (`--paper` on `jaa cv build`, `jaa cv tailor build`, `jaa cv tailor letter`; `paper` on the `generate_cv_pdf` MCP tool), else the new preference `documents.paper_size` (a choice question at the end of onboarding, `jaa.storage.preferences.paper_size`), else A4. `jaa templates show --preview --paper` previews either size without a database.
- **Contract: the paper is a `\documentclass` option that jaa sets.** `jaa.documents.contract.set_paper` replaces the first paper option of the uncommented `\documentclass` (`a4paper`, `letterpaper`, `a5paper`, …), drops any other, or appends one when there is none. It runs in `fill_slots`, so CVs and cover letters, shipped and uploaded templates all go through it. No new slot: a slot line cannot sit inside `\documentclass[...]`, and the class option is what standard classes, moderncv and `geometry` already follow, so existing templates need no change.
- **Templates must not fix the paper elsewhere in the main file.** `paper_problems` (part of `check_template`, for shipped and uploaded templates) refuses paper names outside the class options, `paper=`/`paperwidth=`/`paperheight=`/`papersize=` options, assignments to `\paperwidth`, `\paperheight`, `\pdfpagewidth`, `\pdfpageheight` (or `\pagewidth`/`\pageheight`) and `\special{papersize…}`. Reading those dimensions is allowed. Stored templates are checked again when loaded, so a template added before this change that fixed its paper in `geometry` is refused with that problem until the user re-adds a fixed copy.
- **The build checks the PDF.** A template's own `.cls`/`.sty` files are not scanned (a class legitimately mentions `a4paper` in `\DeclareOption`), so `_compile_into` compares every page's MediaBox with the chosen size (±2 pt) and saves nothing when they differ, as it does for absolute file reads (ADR 0029).
- **Manifest:** every CV and cover letter manifest gains `"paper": "a4" | "letter"`. `generate_cv_pdf` returns `paper` too.
- Shipped templates keep `a4paper` in their source, so their SHA-256 stays meaningful; the `jaa-*` designs' header comment now says jaa sets the option.

## Consequences
- Every shipped CV and cover letter template is tested on both sizes with real builds (`tests/integration/test_paper_sizes.py`): page size, manifest and the text check.
- Onboarding has one more question; skipping it keeps A4.
- Margins are the template's: a Letter page is 6 mm wider and 18 mm shorter than A4, so a one-page A4 CV can run onto a second Letter page. The existing page-count warning reports it.
