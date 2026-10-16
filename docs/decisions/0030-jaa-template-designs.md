# 0030 — jaa's own CV and cover letter designs

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #41

## Context
#41 asks for 2–4 original CV designs beside the moderncv baseline: ATS-friendly, English and Polish, an optional photo and consent clause, A4 and US Letter, graceful page breaks, built against the template contract from #40 (ADR 0029), with redistributable fonts and packages, documented with previews.

## Decision
- **Three CV designs and one letter**, each version 1, family `custom` (they do not use moderncv): `jaa-minimal` (ATS-first single column, TeX Gyre Heros), `jaa-compact` (dense one-page senior layout, TeX Gyre Heros Condensed, contact block beside the name), `jaa-serif` (academic/long-form, TeX Gyre Pagella, dates and language names in a margin column) and `jaa-minimal-letter` (cover letter matching `jaa-minimal`).
- **Built on `article`, one file each.** Every design defines the contract's moderncv-compatible macros itself in `template.tex`, using only `fontspec`, `titlesec`, `needspace`, `etoolbox`, `xcolor` and `graphicx`. They pass the checks an uploaded template gets, without warnings (tested), so they also serve as examples for users writing their own.
- **ATS choices.** One reading order; no icons; no hyphenation; ragged lines; organizations printed as written instead of the `\textsc` the generator wraps them in (TeX Gyre's small caps, real or faked, extract as capitals with stray spaces: "SP . Z O.O."); **kerning off**, because pypdf split "Team" into "T eam" at Heros' and Pagella's strong kerns. The tests compare extracted text with whitespace collapsed but not removed, in reading order, so a split word fails.
- **Page breaks.** `needspace` before each heading (4–5 lines) and each entry (2–3 lines); bullet points are ordinary paragraphs, so long entries break between them. The consent clause is pushed to the bottom of the last page.
- **Fonts by file name** (`texgyreheros-regular.otf`, …), which Tectonic, XeLaTeX and LuaLaTeX all resolve from the TeX tree without a system font lookup.
- **Previews** are PNGs in `docs/images/templates/`, rendered from `jaa.documents.sample` (synthetic, English and Polish) with `jaa templates show --preview` and `pdftoppm`. They live in the documentation, not the package (ADR 0029). The sample's certificate date is now structured like a real fact, so it prints as `05.2022`.

## Deviations from #41
- **No two-column sidebar design.** The generated body is one stream of sections in a fixed order; a sidebar for skills and languages would need the template to collect and move sections, and two text columns are the layout ATS parsers most often read out of order. `jaa-compact` puts the contact details beside the name instead; the body stays one column.
- **US Letter is a one-line change, not a build option.** The contract has no paper slot, so each design is A4 and says in its first lines how to switch (`letterpaper` in `\documentclass`; geometry follows). A test builds every design that way. A `--paper` build option would need a contract change and is left for later. *(Superseded by ADR 0033: jaa now sets the paper option for each build.)*
- **Hyphenation is off**, so "correct hyphenation for both languages" means babel loads the right patterns, not that words are split: split words break ATS keyword matching (ADR 0012).

## Consequences
- The catalog now has six CV and two cover letter templates; the defaults stay `moderncv-classic` and `moderncv-letter`.
- A design change means a new template version and rebuilt preview images.
