-- Uploaded CV and cover letter templates, and the default template choice (#40, ADR 0029).

-- files is JSON text {POSIX path relative to the main file's folder: {"text": source} or
-- {"base64": data}}; main_file is one of its keys. A row is a snapshot: adding changed files
-- under the same template_id stores a new version, so manifests that name id@version keep
-- matching content_sha256. Only template_candidate_id may change (set to NULL when the
-- candidate is deleted). Rows are deleted only when the user deletes the template.
CREATE TABLE user_templates (
    id                    INTEGER PRIMARY KEY,
    template_id           TEXT NOT NULL CHECK (template_id LIKE 'user-%'),
    version               INTEGER NOT NULL CHECK (version >= 1),
    kind                  TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    family                TEXT NOT NULL CHECK (family IN ('moderncv', 'custom')),
    main_file             TEXT NOT NULL,
    files                 TEXT NOT NULL,
    content_sha256        TEXT NOT NULL,
    origin                TEXT NOT NULL CHECK (origin IN ('file', 'folder', 'zip', 'candidate')),
    template_candidate_id INTEGER REFERENCES template_candidates (id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (template_id, version)
);

CREATE TRIGGER user_templates_immutable
BEFORE UPDATE OF template_id, version, kind, name, description, family, main_file, files,
    content_sha256, origin, created_at
ON user_templates
BEGIN
    SELECT RAISE(ABORT, 'uploaded templates are immutable; add a new version instead');
END;

-- Append-only: the newest row per kind is the default. template_id names a shipped template
-- or a user_templates.template_id (no foreign key, since shipped templates are not rows).
CREATE TABLE template_defaults (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
    template_id TEXT NOT NULL,
    set_at      TEXT NOT NULL
);
CREATE INDEX template_defaults_kind ON template_defaults (kind, id);

CREATE TRIGGER template_defaults_immutable BEFORE UPDATE ON template_defaults
BEGIN
    SELECT RAISE(ABORT, 'template defaults are append-only');
END;
