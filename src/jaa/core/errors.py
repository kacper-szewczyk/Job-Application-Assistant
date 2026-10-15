"""Domain exceptions. CLI and MCP layers translate these into user-facing messages."""


class JaaError(Exception):
    """Base class for all expected application errors."""


class ConfigError(JaaError):
    """Invalid or unusable configuration, e.g. a bad data directory."""


class StorageError(JaaError):
    """Database problems: missing database, failed migration, constraint violations."""


class MigrationError(StorageError):
    """Migrations cannot be applied, e.g. an applied migration was edited or is unknown."""


class ExportFormatError(StorageError):
    """An export file is malformed or incompatible with this version."""


class NotFoundError(StorageError):
    """A requested record does not exist."""


class DeletionError(StorageError):
    """Something the user asked to delete cannot be deleted safely, e.g. a changed file."""


class RestoreError(StorageError):
    """A backup or export cannot be restored."""


class DatabaseNotEmptyError(RestoreError):
    """Restoring would replace records, and the user did not ask for that."""


class ReviewError(JaaError):
    """A profile review action is not allowed, e.g. an invalid state transition."""


class FactConflictError(ReviewError):
    """Confirming a fact would replace another confirmed value for the same field key."""


class PreferenceError(JaaError):
    """An unknown preference key, or a value that does not fit the question."""


class ImportSourceError(JaaError):
    """An import source cannot be used: missing, too large, unsupported or unreadable."""


class UnsafeArchiveError(ImportSourceError):
    """An archive is refused because a member could escape it or exhaust resources."""


class ConnectorError(JaaError):
    """A job source connector could not fetch or understand listings."""


class SourcePausedError(ConnectorError):
    """The source is paused, by its policy or by the user, so nothing may be fetched."""


class SourceAccessDeniedError(ConnectorError):
    """The source refused access (login wall, bot check, 401/403). Never retried or worked around.

    Callers pause the source when they see this (ADR 0011).
    """


class SourceUnavailableError(ConnectorError):
    """The source kept failing (network errors, 5xx, rate limiting) after the allowed retries."""


class ConnectorTargetError(ConnectorError):
    """The target (board, division, feed) is not one the connector can fetch. Nothing was sent."""


class SourceDataError(ConnectorError):
    """The source answered, but not with data the connector can use, or broke the policy limits."""


class SourceNotFoundError(SourceDataError):
    """The source answered 404: the board, feed or offer does not exist (any more)."""


class DocumentError(JaaError):
    """A document (CV PDF) cannot be rendered or built."""


class TemplateError(DocumentError):
    """A CV template is unknown, malformed, or does not provide what the renderer needs."""


class TailoringError(DocumentError):
    """A tailoring plan is malformed, blocked by its checks, or missing for a job (ADR 0016)."""


class LatexEngineNotFoundError(DocumentError):
    """No supported LaTeX engine is installed or configured."""


class LatexCompileError(DocumentError):
    """The LaTeX engine failed. `problems` holds user-facing explanations, one per problem.

    They may quote the CV source, so they are shown to the user, never logged.
    """

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems


class SearchError(JaaError):
    """A job search or saved search is invalid or unknown."""


class JobEntryError(JaaError):
    """A job the user adds by hand is incomplete or was already added (#46)."""


class MatchError(JaaError):
    """A job cannot be matched, e.g. because no profile facts are confirmed yet."""


class AnswerError(JaaError):
    """An application answer cannot be saved or used, e.g. an empty value or an unknown key."""


class ApplicationError(JaaError):
    """An application status change or submission record is not allowed (ADR 0018)."""


class DuplicateApplicationError(ApplicationError):
    """Another job that looks like the same role was already prepared or submitted.

    `job_ids` are the other jobs; the caller shows them and may retry with an override.
    """

    def __init__(self, message: str, job_ids: tuple[int, ...]) -> None:
        super().__init__(message)
        self.job_ids = job_ids
