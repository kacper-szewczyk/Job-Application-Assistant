"""`jaa answers …`: application questions per job and the saved answers reused for them (T14).

Questions come from the job's listing or are copied in by the user. `prepare` shows how each
would be answered and where the value comes from; `review` asks the user for every question
that needs input. Sensitive values are masked unless `--reveal` is given, and never logged.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.answers.classify import (
    AnswerAction,
    AnswerClass,
    Note,
    Provenance,
    Resolution,
    is_expired,
)
from jaa.answers.topics import is_explicit_key, question_key
from jaa.cli.common import MASK, console, err_console, open_existing, settings_from
from jaa.core.clock import utcnow
from jaa.core.errors import AnswerError, JaaError
from jaa.core.models import Answer, ReusePolicy
from jaa.storage import answers as service

answers_app = typer.Typer(
    help="Answer application questions and manage the answers jaa may reuse.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals hold answers, some of them sensitive
)

JobArg = Annotated[int, typer.Argument(help="Job id, see `jaa jobs list`.")]
KeyArg = Annotated[str, typer.Argument(help="Answer key, see `jaa answers list`.")]
RevealOpt = Annotated[bool, typer.Option("--reveal", help="Show sensitive values.")]
ExpiresOpt = Annotated[
    str | None,
    typer.Option(
        "--expires",
        help="Review date YYYY-MM-DD, or 'never' (not allowed for sensitive questions). "
        "Default: 180 days for sensitive questions, 365 days for others.",
    ),
]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]

_CLASS_LABELS: Final = {
    AnswerClass.PROFILE_BACKED: "profile",
    AnswerClass.REUSABLE_ANSWER: "saved answer",
    AnswerClass.INFERRED_SUGGESTION: "suggestion (inferred)",
    AnswerClass.UNANSWERED: "unanswered",
}
_ACTION_STYLES: Final = {
    AnswerAction.FILL: ("fill", "green"),
    AnswerAction.REVIEW: ("fill, review it", "yellow"),
    AnswerAction.ASK: ("needs your input", "red"),
}
_NOTES: Final = {
    Note.SENSITIVE: "sensitive: needs your explicit answer",
    Note.SAFE_REUSE: "you set this answer to be reused without asking",
    Note.EXPIRED: "review date passed: confirm it again",
    Note.UNCONFIRMED: "not confirmed",
    Note.ALWAYS_ASK: "you asked to be asked every time",
    Note.REVIEW_POLICY: "reused with review",
    Note.DIFFERENT_WORDING: "saved for a differently worded question",
    Note.UNREVIEWED_FACT: "from an unreviewed profile fact",
    Note.FROM_PREFERENCE: "inferred from your preferences",
    Note.NOT_AN_OPTION: "not one of the question's options",
    Note.UNKNOWN_QUESTION: "new question",
}
_POLICY_LABELS: Final = {
    ReusePolicy.REUSE: "reuse",
    ReusePolicy.REUSE_WITH_REVIEW: "reuse, review",
    ReusePolicy.ALWAYS_ASK: "always ask",
}


@contextmanager
def _database(ctx: typer.Context) -> Generator[sqlite3.Connection]:
    conn = open_existing(settings_from(ctx))
    try:
        yield conn
    except JaaError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _expiry(text: str | None) -> service.Expiry:
    if text is None:
        return service.DEFAULT
    if text.strip().casefold() == "never":
        return None
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise AnswerError("--expires must be YYYY-MM-DD or 'never'") from None
    return datetime.combine(day, time(), tzinfo=UTC)


def _policy(text: str) -> ReusePolicy:
    try:
        return ReusePolicy(text.strip().casefold().replace("-", "_"))
    except ValueError:
        options = ", ".join(p.value for p in ReusePolicy)
        raise AnswerError(f"policy must be one of {options}") from None


def _shown(value: str | None, *, sensitive: bool, reveal: bool) -> Text:
    if value is None:
        return Text("-", style="dim")
    if sensitive and not reveal:
        return Text(MASK)
    return Text(value)


def _source(p: Provenance) -> str:
    parts: list[str] = []
    if p.answer_id is not None:
        parts.append(f"answer #{p.answer_id}")
    parts += [f"fact #{i}" for i in p.fact_ids]
    if p.preference_key is not None:
        parts.append(f"preference {p.preference_key}")
    return ", ".join(parts) or "-"


def _notes(r: Resolution) -> str:
    return "; ".join(_NOTES[n] for n in r.notes)


def resolution_table(resolutions: list[Resolution], *, reveal: bool) -> Table:
    table = Table("#", "Question", "Answer", "Kind", "Source", "Confidence", "Use", show_lines=True)
    for number, r in enumerate(resolutions, start=1):
        label, style = _ACTION_STYLES[r.action]
        use = Text(label, style=style)
        if r.notes:
            use.append(f"\n{_notes(r)}", style="dim")
        question = Text(r.question.text + (" *" if r.question.required else ""))
        question.append(f"\n{r.question_key}", style="dim")
        table.add_row(
            str(number),
            question,
            _shown(r.value, sensitive=r.sensitive, reveal=reveal),
            _CLASS_LABELS[r.answer_class],
            _source(r.provenance),
            r.confidence.value if r.confidence else "-",
            use,
        )
    return table


@answers_app.command("prepare")
def prepare(ctx: typer.Context, job: JobArg, reveal: RevealOpt = False) -> None:
    """Show how each of a job's questions would be answered, and where each answer comes from."""
    with _database(ctx) as conn:
        resolutions = service.prepare_answers(conn, job)
    if not resolutions:
        console.print(
            f"Job #{job} has no application questions. Copy them from the application form "
            f'with `jaa answers add-question {job} "QUESTION"`.'
        )
        return
    console.print(resolution_table(resolutions, reveal=reveal))
    asks = sum(r.action is AnswerAction.ASK for r in resolutions)
    reviews = sum(r.action is AnswerAction.REVIEW for r in resolutions)
    console.print(
        f"{len(resolutions)} question(s): {asks} need your input, {reviews} to review. "
        "* = required."
    )
    if asks:
        console.print(f"Answer them with `jaa answers review {job}`.")
    if not reveal and any(r.sensitive and r.value for r in resolutions):
        console.print("Sensitive values are hidden; pass --reveal to show them.")


@answers_app.command("review")
def review(
    ctx: typer.Context,
    job: JobArg,
    everything: Annotated[
        bool, typer.Option("--all", help="Also go through answers that would be filled.")
    ] = False,
    reveal: RevealOpt = False,
) -> None:
    """Answer the questions of a job that need your input. Answers are saved for reuse."""
    with _database(ctx) as conn:
        resolutions = service.prepare_answers(conn, job)
        todo = [r for r in resolutions if everything or r.action is AnswerAction.ASK]
        if not todo:
            console.print("Nothing needs your input." if resolutions else "No questions.")
            return
        console.print(
            "Type an answer, 'y' to use the suggestion shown, or press Enter to skip. "
            "Answers are saved on this computer and reused as you set them."
        )
        saved = skipped = 0
        for number, r in enumerate(todo, start=1):
            console.print(Text(f"\n{number}/{len(todo)} {r.question.text}", style="bold"))
            if r.question.options:
                console.print(Text("  Options: " + " | ".join(r.question.options)))
            if r.value is not None:
                shown = _shown(r.value, sensitive=r.sensitive, reveal=reveal)
                console.print(
                    Text("  Suggestion: ")
                    + shown
                    + Text(f"  ({_CLASS_LABELS[r.answer_class]}, {_source(r.provenance)})")
                )
                if r.sensitive and not reveal:
                    console.print("  (hidden: run again with --reveal to see it before using it)")
            if r.notes:
                console.print(Text(f"  {_notes(r)}", style="dim"))
            text: str = typer.prompt(">", default="", show_default=False, prompt_suffix=" ")
            text = text.strip()
            if not text:
                skipped += 1
                continue
            if text.casefold() in ("y", "yes") and r.value is not None:
                service.accept_suggestion(conn, r, job)
            else:
                service.save_answer(conn, question_text=r.question.text, value=text, job_id=job)
            saved += 1
    console.print(f"\nDone: {saved} saved, {skipped} skipped.")
    console.print(f"Check the result with `jaa answers prepare {job}`.")


@answers_app.command("add-question")
def add_question(
    ctx: typer.Context,
    job: JobArg,
    text: Annotated[str, typer.Argument(help="The question, as the application form asks it.")],
    required: Annotated[
        bool, typer.Option("--required", help="The form marks the question as required.")
    ] = False,
) -> None:
    """Add a question copied from the employer's application form."""
    with _database(ctx) as conn:
        stored = service.add_job_question(conn, job, text, required=required)
    console.print(f"Added question #{stored.id} to job #{job}.")


@answers_app.command("remove-question")
def remove_question(
    ctx: typer.Context,
    job: JobArg,
    question_id: Annotated[int, typer.Argument(help="Question id, see `jaa answers questions`.")],
) -> None:
    """Remove a question you added. Questions from the listing cannot be removed."""
    with _database(ctx) as conn:
        service.remove_job_question(conn, job, question_id)
    console.print(f"Removed question #{question_id} from job #{job}.")


@answers_app.command("questions")
def questions(ctx: typer.Context, job: JobArg) -> None:
    """List a job's questions: from its listing, and those you added."""
    with _database(ctx) as conn:
        found = service.job_question_inputs(conn, job)
    if not found:
        console.print(f"Job #{job} has no application questions.")
        return
    table = Table("From", "Id", "Question", "Required")
    for q in found:
        table.add_row(
            "listing" if q.origin == "listing" else "you",
            q.origin_id or "-",
            Text(q.text),
            "yes" if q.required else "",
        )
    console.print(table)


def _expires_text(answer: Answer, now: datetime) -> Text:
    if answer.expires_at is None:
        return Text("never")
    style = "red" if is_expired(answer, now) else ""
    suffix = " (passed)" if is_expired(answer, now) else ""
    return Text(f"{answer.expires_at:%Y-%m-%d}{suffix}", style=style)


@answers_app.command("list")
def list_answers(ctx: typer.Context, reveal: RevealOpt = False) -> None:
    """List your saved answers with their reuse policy and review date."""
    with _database(ctx) as conn:
        current = service.current_answers(conn)
    if not current:
        console.print("No saved answers yet.")
        return
    now = utcnow()
    table = Table("Key", "Question", "Answer", "Reuse", "Confirmed", "Review by")
    table.columns[0].no_wrap = True  # keys are typed back into other commands
    for key, a in sorted(current.items()):
        table.add_row(
            Text(key),
            Text(a.question_text),
            _shown(str(a.value), sensitive=a.is_sensitive, reveal=reveal),
            _POLICY_LABELS[a.reuse_policy],
            f"{a.confirmed_at:%Y-%m-%d}" if a.confirmed_at else "-",
            _expires_text(a, now),
        )
    console.print(table)
    if not reveal and any(a.is_sensitive for a in current.values()):
        console.print("Sensitive values are hidden; pass --reveal to show them.")


@answers_app.command("show")
def show(ctx: typer.Context, key: KeyArg, reveal: RevealOpt = False) -> None:
    """Show every saved version of an answer and where its values came from."""
    with _database(ctx) as conn:
        history = service.answer_history(conn, key)
        sources = {a.id: service.answer_fact_ids(conn, a.id) for a in history if a.id}
    if not history:
        console.print(Text(f"No saved answer for {key}."))
        return
    console.print(Text(f"{key}: {history[-1].question_text}"))
    now = utcnow()
    for a in history:
        origin = Provenance(
            answer_id=a.source_answer_id,
            fact_ids=sources.get(a.id or 0, ()),
            preference_key=a.source_preference_key,
        )
        line = Text(f"#{a.id} {a.created_at:%Y-%m-%d %H:%M} UTC  {a.origin.value:<15} ")
        line += _shown(str(a.value), sensitive=a.is_sensitive, reveal=reveal)
        line += Text(f"  [{_POLICY_LABELS[a.reuse_policy]}; review by ")
        line += _expires_text(a, now)
        details = [f"from {_source(origin)}"] if not origin.is_empty() else []
        if a.job_id is not None:
            details.append(f"for job #{a.job_id}")
        line += Text("]" + (f" {', '.join(details)}" if details else ""))
        console.print(line)


@answers_app.command("set")
def set_answer(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="The question, as an application form asks it.")],
    value: Annotated[str, typer.Argument(help="Your answer.")],
    policy: Annotated[
        str | None,
        typer.Option(
            "--policy",
            help="always_ask, reuse_with_review or reuse. Default: your answer-reuse preferences.",
        ),
    ] = None,
    expires: ExpiresOpt = None,
    job: Annotated[
        int | None, typer.Option("--job", help="The job this answer is for, if any.")
    ] = None,
    yes: YesOpt = False,
) -> None:
    """Save an answer to a question for reuse."""
    with _database(ctx) as conn:
        chosen = _policy(policy) if policy else None
        key = question_key(question)
        if chosen is ReusePolicy.REUSE:
            _confirm_safe_reuse(key, yes=yes)
        stored = service.save_answer(
            conn,
            question_text=question,
            value=value,
            policy=chosen,
            expires_at=_expiry(expires),
            job_id=job,
        )
    console.print(Text(f"Saved answer #{stored.id} as {stored.question_key}."))


def _confirm_safe_reuse(key: str, *, yes: bool) -> None:
    if not is_explicit_key(key) or yes:
        return
    console.print(
        "[yellow]This is a sensitive question. With 'reuse', jaa fills in this answer for "
        "this exact question without asking you, until its review date.[/yellow]"
    )
    if not typer.confirm("Reuse it without asking?", default=False):
        raise typer.Exit(code=1)


@answers_app.command("reconfirm")
def reconfirm(ctx: typer.Context, key: KeyArg, expires: ExpiresOpt = None) -> None:
    """Confirm an answer is still right, starting a new review period."""
    with _database(ctx) as conn:
        stored = service.reconfirm_answer(conn, key, expires_at=_expiry(expires))
    review = f"{stored.expires_at:%Y-%m-%d}" if stored.expires_at else "never"
    console.print(Text(f"Confirmed {key} again; review by {review}."))


@answers_app.command("policy")
def set_policy(
    ctx: typer.Context,
    key: KeyArg,
    policy: Annotated[str, typer.Argument(help="always_ask, reuse_with_review or reuse.")],
    yes: YesOpt = False,
) -> None:
    """Change how a saved answer is reused."""
    with _database(ctx) as conn:
        chosen = _policy(policy)
        if chosen is ReusePolicy.REUSE:
            _confirm_safe_reuse(key, yes=yes)
        service.set_reuse_policy(conn, key, chosen)
    console.print(Text(f"{key}: {_POLICY_LABELS[chosen]}."))


@answers_app.command("forget")
def forget(ctx: typer.Context, key: KeyArg, yes: YesOpt = False) -> None:
    """Delete every saved version of an answer."""
    if not yes and not typer.confirm(f"Delete every saved version of {key}?", default=False):
        raise typer.Exit(code=1)
    with _database(ctx) as conn:
        removed = service.forget_answer(conn, key)
    console.print(Text(f"Deleted {removed} version(s) of {key}."))
