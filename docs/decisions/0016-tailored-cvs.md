# 0016 — Tailored CVs: checked edits, evidence and approval

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #14

## Context
T13 makes a CV for one saved job. SPEC §6 allows tailoring to select, reorder and emphasize true experience, but not to add unsupported claims or change dates or meaning. Every edit must link to the confirmed facts behind it, and edits without evidence are blocked unless the user approves them. The user must see every change against the baseline CV, edit it and build again. The PDF must be linked to the job and the profile version. A cover letter draft is optional and follows the same rules. T12 (ADR 0015) stores which facts back each of a job's requirements.

## Decision
- **A plan of edits on the baseline CV.** The baseline is what `build_content` makes from the confirmed facts (ADR 0012). A plan (`jaa.documents.tailoring.Plan`) is a list of edits plus optional cover letter paragraphs. It is pure data, applied by the pure function `tailor`.
- **Four edit kinds, on a fixed set of targets.**
  - `hide`: the summary, an experience or education entry, a bullet, a skill or a certificate.
  - `move`: a bullet within its entry, or a skill within the skills, to a position counted from 1.
  - `emphasize`: a bullet or a skill, set in bold. The renderer adds `\textbf` only for emphasized items, so baseline CVs render as before and the template version does not change.
  - `rewrite`: new wording for the headline, the summary or a bullet.

  Targets are addresses: `headline`, `summary`, `experience.<id>`, `education.<id>`, `experience.<id>.description[<n>]` (counting the baseline bullets), `skills.<key>` and `certifications.<key>`. Names, contact details, employers, titles, institutions, degrees and dates have no address, so no edit can change them. Entries cannot be reordered, which keeps the timeline intact.
- **Evidence.** Each edit is linked to its target's own facts plus the facts it cites (`fact_ids`). Hiding, moving and emphasizing only rearrange true content, so the target's fact is enough. A rewrite is checked by wording:
  - Its *claims* are numbers, words with capitals (other than a sentence's first word), words with `+` or `#`, and words from the job's listed skills and languages.
  - Every claim must appear in the target's current text or in a cited fact (folded like matching, ADR 0015).
  - A cover letter paragraph must cite at least one fact, and its claims must appear in the cited facts.
- **Issues and approval.**
  - Edits with an unknown target, a kind the target does not allow, a missing position, empty text or a cited fact that does not exist are blocked. They cannot be approved.
  - Unsupported claims, no evidence, and cited facts that are only unreviewed are blocked too. The user can approve these with `jaa cv tailor approve`, after a confirmation prompt that shows the issues.
  - An approval is stored as the fingerprint (sha256) of the edit's content, so changing the edit drops its approval. A plan file cannot carry approvals: unknown fields, `approved` included, are rejected.
  - Blocked edits are not applied, and a plan with blocked edits or paragraphs does not build.
- **Proposals.** `jaa cv tailor propose` uses the job's latest match. If there is none, or it is outdated (ADR 0015), the job is matched first. The proposal only reorders and emphasizes:
  - skills that meet requirements move to the front and are set in bold, required ones first;
  - bullets that mention a met requirement move to the top of their entry;
  - moves that would change nothing are left out.

  It never hides or rewords; those are the user's choices. Only confirmed facts are used, so a proposal never contains a blocked edit.
- **Storage.** Migration 0009 adds `cv_tailorings`, made append-only by a trigger. Each proposal, edit or approval adds a revision (`origin` = `proposed`, `edited` or `approved`). A revision stores:
  - the job, revision number, profile version and match it was based on;
  - the plan as JSON;
  - the issue codes per edit (codes only, no text);
  - the approval fingerprints, the `blocked` flag and the method (`tailoring-v1`).

  A revision is always evaluated against its own profile version, so it rebuilds the same CV after the profile changes. The CLI says when the profile has changed since.
- **Review in the CLI.**
  - `jaa cv tailor show` lists each edit with its target, before and after, facts, reason and status (ok, approved or blocked with the reason). `--full` prints the tailored CV, `--json` the plan.
  - `jaa cv tailor edit` opens the plan in `$VISUAL`/`$EDITOR`, in an owner-only temporary folder that is removed afterwards. `--file` reads a plan file instead, which is how Claude will hand plans over (T16/T17).
- **Build.** `jaa cv tailor build` uses the ADR 0012 pipeline. The files are named `cv-<time>-job<id>-…`. The `DocumentArtifact` rows get the job id and the revision's profile version. The manifest adds `job_id` and a `tailoring` object with the revision, method, match id, approved edit numbers and the applied changes (before, after and facts of each). Unreviewed facts that no edit cites are recorded as excluded.
- **Cover letter.** Paragraphs are stored, checked and shown with the plan, but not built into a PDF yet. A cover letter template and PDF are a follow-up issue.
- Logs carry job ids, revision numbers, edit counts and the blocked flag, never edit text.

## Consequences
- The wording check catches new numbers, names, technologies and job keywords. It cannot catch every change of meaning in ordinary words, for example "helped" turned into "led". The user still reviews every change, and the change list shows before and after.
- Bullet addresses count the baseline bullets of one profile version. A plan edited after the profile changed may point at different bullets; the check reports targets that no longer exist, and the change list shows the text.
- The planned MCP tools `draft_tailored_cv` and `generate_cv_pdf` (T16) can wrap `propose`/`save_edited` and `build` without new rules.
