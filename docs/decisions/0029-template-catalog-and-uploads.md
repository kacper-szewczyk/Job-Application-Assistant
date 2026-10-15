# 0029 — Template catalog, uploaded templates and the template contract

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #40

## Context
T08 (ADR 0012) shipped one CV template, `moderncv-classic`, and #53 one cover letter template. #40 asks for a small catalog of shipped templates, for templates the user adds (a `.tex` file, a folder or a ZIP, or a template candidate from ADR 0009), for a documented contract that added templates are checked against, for a default template with a per-build override, and for safe compilation in which a template cannot read files outside its own folder. #41 will design new templates against the same contract.

## Decision
- **Catalog.** Two more moderncv styles ship: `moderncv-banking` and `moderncv-casual`, each version 1, with the same ATS choices as classic (no hyphenation, no icon glyphs). `oldstyle` and `fancy` were tried and left out: their narrow contact column makes URLs run into the margin. Each `template.json` records its license (template file MIT; the moderncv class is LPPL and comes with the engine, not with jaa).
- **The contract** lives in `jaa.documents.contract` and `docs/templates.md`: slot lines (each exactly once; `babel`, `header` and `letter` before `\begin{document}`, `body` and `consent` after), a `\documentclass`, and the moderncv macros the generated LaTeX calls. A template whose class is not moderncv is family `custom` and must define those macros itself; definitions are found with a static scan of all its text files. Shipped templates are checked against the same contract when loaded.
- **Uploaded templates** are read by `jaa.importers.template` (ZIPs through `jaa.importers.archive`, so nothing is extracted; folders skip symlinks and hidden files) and stored by `jaa.storage.templates` in a new table `user_templates` (migration 0015): `files` is JSON (`{"text": …}` or `{"base64": …}` per path), so templates travel with export and backup and survive the user moving their files. Ids are `user-<name>`, so they can never shadow a shipped template. Rows are immutable; adding changed files under the same id stores the next version, identical files are recognized. The template's SHA-256 covers the main file name and all files, and goes into every manifest with id, version and a new `uploaded` flag. Limits: 100 files, 5 MB, an allow-list of text, image and font types, UTF-8 text only.
- **Validation before compiling.** `jaa templates check` and `jaa templates add` print every problem and warning; `add` refuses a template with problems. A stored template is checked again whenever it is used, so a stricter contract applies to old templates too.
- **File access.** Tectonic `--untrusted` only turns off shell escape: in testing it read `\input{/abs/path}` and `\input{../x}` without complaint. So uploaded templates are checked statically: absolute, `~` and `..` paths, Lua, `\write18`, `\ShellEscape`, `\openout` and piped `\input` are refused; `\csname`, `\catcode` and `\scantokens`, which can hide a file name, are warnings. After every build, a Tectonic report of reading an absolute path fails the build and no PDF is saved. XeLaTeX and LuaLaTeX keep kpathsea's paranoid mode (ADR 0012).
- **Default and override.** `template_defaults` (append-only, newest row per kind wins) holds the user's default; without one the shipped defaults apply. `jaa cv build`, `jaa cv tailor build|letter` and the MCP tool `generate_cv_pdf` take an optional `--template`/`template` (`id` or `id@version`) and otherwise use the default. Deleting an added template that is the default records a row resetting it to the shipped default.
- **Previews** are built on demand from synthetic content (`jaa.documents.sample`, English and Polish) with `jaa templates show ID --preview`, into `previews/` in the data dir. They are not recorded as documents and never use the user's profile.
- **CLI** `jaa templates list|show|check|add|default|delete`; `jaa cv templates` prints the same list. `default` and `delete` record the user's choice or delete their data, so they are user-only in the skill and denied to Claude (ADR 0020). `check`, `add` and `show --preview` are not.

## Deviations from #40
- **No preview images in the package.** The issue asks for a preview image per template. A shipped PNG would go stale with every engine or moderncv update and adds binary files to the wheel; `show --preview` renders the current template instead. #41 can still add images to its documentation.
- **File access is checked, not sandboxed.** The issue says templates must not read files outside their folder. Without an OS sandbox around Tectonic this is enforced by the static check plus Tectonic's absolute-path report, which does not cover a `..` path hidden behind `\csname`; such templates get a warning. Users are told to add templates only from sources they trust.

## Consequences
- Templates from #41 only need to meet the contract; a custom class defines the moderncv macros.
- A template made from the user's own CV (a candidate) stores their CV text in `user_templates.files`; it is covered by export, backup and `jaa templates delete`.
- There is no UI screen for templates yet.
