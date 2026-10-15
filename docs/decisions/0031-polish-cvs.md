# 0031 — Polish CVs and Polish-language use

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #32

## Context
The CV parser (`jaa.importers.cv`, ADR 0008) recognized only English headings, role/degree/institution keywords, month names and "present" words, so a Polish CV imported only its contact details. The user targets the Polish market first (#1), and Polish CVs often mix languages: Polish headings with English content, or the reverse. #32 also asks which later features need Polish.

## Decision
- **One parser, two vocabularies.** English and Polish words live side by side in the same tables, so no language is detected or chosen, and a mixed-language CV needs no special handling. Polish adds headings (`Doświadczenie zawodowe`, `Wykształcenie`, `Umiejętności`, `Języki obce`, `Certyfikaty`, `Podsumowanie`/`O mnie`, `Dane kontaktowe`, and unsupported ones such as `Projekty`, `Wolontariat`, `Kursy`), role stems (`programist…`, `inżynier…`, `kierowni…`; feminine and inflected forms included), degrees (`licencjat`, `magister`, `mgr`, `inż.`, `doktor`, `matura`), institutions (`uniwersytet`, `politechnika`, `akademia`, `szkoła`, `liceum`, `technikum`), month abbreviations and their nominative, genitive and locative forms (`sty.`, `styczeń`, `stycznia`, `styczniu`), "present" words (`obecnie`, `aktualnie`, `nadal`, `do dziś`, `do teraz`), the range word `do`, a leading `od`/`from`, and `Życiorys` as a document title.
- **Matching runs on folded text**: lowercase with diacritics removed one character at a time (`Łódź` → `lodz`), so `DOŚWIADCZENIE`, `Doświadczenie` and `Doswiadczenie` are the same heading. Folding keeps the text's length, so a date found in folded text is cut out of the original with its diacritics intact.
- **"Role w Employer".** The Polish `w` splits an entry heading only when the part before it contains a role word. `w` is also part of names (`Bank Spółdzielczy w Przykładowie`), which are never split.
- **Language-independent keys where the slug allows.** Slugs already fold Polish letters (`ą→a`, `ł→l`, `ż/ź→z`). A language item that is a bare language name, optionally after `Język`, is keyed by its English name (`angielski`, `Język angielski` and `English` are all `languages.english`). The value keeps the wording the CV used. The name table moved from `jaa.matching.match` to `jaa.core.languages`, so matching and import use the same one. Keys that cannot be shared: entries are keyed by employer or institution, so they match across languages only when the CV names them the same way (a translated university name gives a different key). Polish legal forms stay in the key (`…-sp-z-o-dot-o`), like `Ltd` in English ones. Titles, degrees and descriptions are written text, so a Polish and an English CV give conflicting values for the same key. Review handles that as it does any re-import (ADR 0006).
- **The RODO consent clause** ("Wyrażam zgodę na przetwarzanie…", "Zgodnie z art. 6…", or an English "I consent to the processing of my personal data", with or without a `Klauzula` heading) is reported as `consent_clause_not_imported`, not misread as part of the previous section. It is not a profile fact; generated CVs use the clause the user gives `jaa cv build --consent-file`.
- Two small fixes made along the way also apply to English CVs. A heading line ending in a legal form's full stop (`… sp. z o.o.`, `… Ltd.`) is not treated as prose. A document title (`Curriculum Vitae`, `Życiorys`) before the name is skipped instead of using up the name check.

**Which later features need Polish**, with what they have now:
- **LaTeX/PDF output (T08):** done before this ADR. `--language pl` gives Polish section labels, dates and `babel` (hyphenation patterns), and the fonts cover Polish letters (ADR 0012, 0024, 0030).
- **Requirement extraction and matching (T12):** done before this ADR. Polish headings and language names (ADR 0015).
- **Application question classification (T14):** done before this ADR. Topic patterns cover Polish wordings (ADR 0017).
- **LinkedIn archive import:** not needed. The archive's column names and dates are English whatever language the user's LinkedIn interface is in. Content in Polish goes through the same slugs.
- **CLI, UI and messages** stay English. Translating the interface is out of scope.

## Consequences
- Adding a language means adding table rows, with tests. Like heading and slug rules (ADR 0008), the vocabulary decides keys, so changes should be deliberate.
- Short Polish month abbreviations overlap common words (`sie`), but only count next to a four-digit year.
- Roman-numeral months (`IX 2019`) and the `r.` year suffix are not recognized. Such lines are reported as not imported.
- Polish CVs with a translated university or company name get different entry keys from the English CV.
