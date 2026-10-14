# 0010 — LinkedIn data archive import

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #8

## Context
T07 imports the user's own LinkedIn data archive (SPEC §5). LinkedIn is an import source, not a scraping target: we may not fetch profile URLs, automate LinkedIn pages, or ask for passwords, cookies or tokens. The archive is a ZIP of CSV files. LinkedIn decides which files it contains and can change their names and columns at any time, and a full archive also holds messages, connections and ad data that a CV never needs. The archive comes from outside the app, so it must be inspected safely. The user must also be able to delete the archive after import, and that choice must be recorded on the `ImportRun`.

## Decision
- **Input:** one ZIP archive, or one or more CSV files from it (`jaa import linkedin <zip | csv…>`). Run without arguments, the command prints download instructions. The type is detected from content (ZIP magic) and, for CSVs, from the `.csv` extension. Other files are refused.
- **Archive safety** (`jaa.importers.archive`): nothing is extracted to disk. All member names are validated first. If any member has an absolute path, a drive letter, backslashes or `..` parts, or is a symbolic link, the **whole archive is refused** (`UnsafeArchiveError`), because a genuine export never contains one. The archive is capped at 2 GB and 10,000 members. A recognized member is read into memory with a 10 MB cap that ignores the size the archive declares, and is refused if its compression ratio exceeds 200 (ZIP bomb). Encrypted members are refused.
- **Recognized files only.** Files are matched by base name in any folder, ignoring case, spaces and underscores. Their required columns are checked, and a file whose format changed is reported as `unexpected_format`, not guessed at. Imported: `Profile`, `Email Addresses`, `PhoneNumbers`, `Positions`, `Education`, `Skills`, `Languages`, `Certifications`. Known profile sections we do not import yet (Projects, Honors, Courses, Volunteering, …) are reported as `section_not_imported` warnings. Every other member is **never opened** and is listed by name in `summary.skipped_files`. These files are not issues, so a full archive is not `partial` because of them.
- **Field keys match the CV importer** (ADR 0008), so the same employer imported from a CV and from LinkedIn meets on the same key. Identical values are skipped, and different values become review conflicts (ADR 0006). Keys: `identity.name`, `headline`, `summary`, `contact.{email,phone,location}`, `contact.links.<kind>`, `experience.<employer>.{employer,title,dates,location,description}`, `education.<school>.{institution,degree,dates,details,activities}`, `skills.<slug>`, `languages.<slug>`, `certifications.<slug>` (with `authority` in its value). A repeated employer gets `-<start date>`, as in CVs. An empty "Finished On" on a position means current, as LinkedIn shows it. Dates that do not parse are left out with a warning.
- **Data minimization:** Profile.csv's address, birth date, zip code, maiden name, messenger and Twitter handles, and industry are not imported. The warning names those columns, never their values. Only the primary email and the first phone number are imported. The others are counted, not quoted.
- **Provenance:** location `<file> row N`, the raw cell as `value_raw` and excerpt, and the import time on the run. Confidence is 0.9 for structured fields (0.95 for email) and 0.8 for free text we split into lines. Everything is stored `unreviewed`.
- **Retention:** `--delete-source` or `--keep-source`. If neither is given, the CLI asks, and the default is keep. The choice is stored in `ImportRun.retention`. Deletion happens only after the facts are stored, only for a run that did not fail, and only if every file still has the SHA-256 it had at import; otherwise nothing is deleted. `source_deleted_at` records when the files were deleted.
- **Fallbacks:** a LinkedIn "Save to PDF" profile goes through `jaa import cv`. Pasted sections go through the new `jaa import text` (stdin or a UTF-8 file), which feeds `parse_cv`. The text is not kept anywhere; the run stores only its hash.

## Consequences
- If LinkedIn renames files or columns, we report them as skipped or as `unexpected_format` and import nothing from them. The fix is a table change in `jaa.importers.linkedin`.
- Rich media and other non-CSV members are never read, so a large archive costs little more than reading its central directory.
- Messages and connections are never opened, so their contents cannot leak into facts, issues or logs.
