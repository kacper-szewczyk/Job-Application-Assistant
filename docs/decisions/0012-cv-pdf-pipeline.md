# 0012 — CV PDF pipeline: LaTeX templates, engine and manifests

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #9

## Context
T08 turns the confirmed profile into a PDF CV (SPEC §7 Document generation). The CV must use only confirmed facts. It must be readable by applicant tracking systems (ATS), reproducible, and traceable to the facts and template that made it. It must also work in English and Polish, because the Polish market comes first (#1). The user's own CV uses moderncv (classic style), and the first template starts from it. Choosing, uploading and designing other templates are tracked in #40 and #41.

## Decision
- **Engine: Tectonic first, then XeLaTeX and LuaLaTeX.** `find_engine` takes `JAA_LATEX_ENGINE` (a name or path), else the first of `tectonic`, `xelatex`, `lualatex` on `PATH`. Tectonic is one binary with no TeX installation, and it fetches pinned packages on first use. Its bundle carries moderncv 2.3.1, so no class files are vendored. pdfLaTeX is not supported: it needs extra setup for full Unicode (Polish diacritics). When no engine is found, the CLI explains how to install one.
- **Safe compilation.** Shell escape is always off: `--untrusted` for Tectonic, `-no-shell-escape` for TeX. kpathsea runs in paranoid mode (`openin_any`/`openout_any=p`), `TEXINPUTS` is cleared, and each build runs in a private temporary folder inside the output folder. All fact text goes through `escape()`, so a fact can never become a LaTeX command. The engine's log quotes the CV, so it is shown to the user, never logged. On failure it is kept next to the output as an owner-only `.log`.
- **Template contract.** A template is a folder `jaa/documents/templates/<id>/` with `template.json` (id, version, name, family, description, license, source) and `template.tex`. The `.tex` has four slot lines, `%%jaa:babel%%`, `%%jaa:header%%`, `%%jaa:body%%` and `%%jaa:consent%%`, each exactly once; jaa replaces them with generated LaTeX. Templates are checked when loaded. The first template is `moderncv-classic` version 1 (`moderncv-classic@1`).
- **ATS-friendly layout.** Hyphenation is off, because split words break keyword matching. The phone, email and homepage icons are removed, because text extraction turns icon glyphs into stray characters. Links are shown as plain text. Bullets use `\textbullet`. Line breaks are allowed only where they keep words whole: after the dash in a date range, and after the hyphen in a double-barrelled name.
- **Content.** `build_content` is pure: it arranges confirmed facts into sections and never adds text that is not in a fact. Experience is one section, current entry first and then newest first. The CV has no custom grouping like "Leadership history" yet. Facts the template cannot show (for example interests) are listed to the user as not shown, never dropped silently.
- **Unreviewed facts go through review.** Before a build, `jaa cv build` lists unreviewed facts and lets the user confirm some through the normal review service. That creates a new profile version, and conflicts are sent to `jaa profile conflicts`. Facts left unreviewed are not used, and the manifest records their ids. `--confirmed-only` skips the question.
- **Languages.** `--language en|pl` sets babel, section headings, and the word for "present" in date ranges. Fact text is never translated.
- **Photo and consent clause come only from the user.** `--photo` takes a JPEG or PNG, checked by magic bytes and at most 10 MB. `--consent-file` takes the user's own clause (for example the Polish RODO "Oświadczenie"), UTF-8 and at most 10 kB. jaa never adds a default clause.
- **Verification.** After compiling, the PDF's text is extracted with pypdf and checked against every piece of fact text, after NFKC, dash and whitespace normalisation. Text that is missing is reported as an ATS risk. Overfull lines of 5pt or more and CVs over 2 pages are reported as layout warnings.
- **Output and manifest.** The PDF and its `.tex` are written owner-only as `cv-<UTC timestamp>-<template>-<lang>.{pdf,tex}` in the data dir's `documents/` folder, or in `--output`. Each build records two `DocumentArtifact` rows (`CV_PDF`, `CV_SOURCE`). The PDF row carries a manifest with:
  - template id, version and sha256;
  - engine name and version;
  - language;
  - profile version, used fact ids and excluded unreviewed fact ids;
  - whether a photo or consent clause was included;
  - paths, hashes and page count;
  - the text-check counts.
- **CI** installs a pinned Tectonic release, verified by sha256, and caches its package bundle. It sets `JAA_REQUIRE_LATEX=1`, so tests marked `latex` fail instead of skipping when no engine is available. Locally they skip without an engine.

## Consequences
- A CV can always be traced back to the exact template text and profile version that made it, and rebuilt from its `.tex`.
- The first Tectonic build needs network access to download packages (about 2 minutes). Later builds take about 2 seconds and work offline.
- A layout change to a template is a new template version, since the sha256 in old manifests must keep matching.
- Two-section experience grouping, interests and further templates need new content keys or template versions (#40, #41).
