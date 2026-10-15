-- Assisted-apply handoff and application tracker (T15, ADR 0018).
--
-- An application records what was used when the user submitted it: the link they applied at
-- (`source_url`), and a `handoff` snapshot (question keys with how each was answered and where
-- the value came from, document ids, overridden duplicate warnings). Values stay in `answers`
-- and `profile_facts`; the answer versions used are linked in `application_answers` and the
-- documents through `document_artifacts.application_id`.
ALTER TABLE applications ADD COLUMN source_url TEXT;
ALTER TABLE applications ADD COLUMN handoff TEXT NOT NULL DEFAULT '{}';

-- A recorded submission is a fact about the past: it cannot be edited, only deleted with the
-- application. `application_answers` has no insert guard, because importing an export inserts
-- the links after their submitted application.
CREATE TRIGGER applications_submission_immutable
BEFORE UPDATE OF submitted_at, submission_confirmed_by, source_url, handoff ON applications
WHEN OLD.submitted_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a recorded submission cannot be changed');
END;
