"""`jaa cv tailor …`: tailored CVs for one job, reviewed edit by edit (T13, ADR 0016).

jaa proposes only reordering and emphasis, backed by the job's match. The user (or Claude,
through a plan file) can add hides and rewrites; every edit is checked against confirmed facts
and blocked when its wording is not backed, unless the user approves it here. CV text is
printed for the user, never logged.
"""

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.apply import print_conflicts
from jaa.cli.common import console, err_console, settings_from
from jaa.cli.cv import (
    LanguageOption,
    PaperOption,
    database,
    outline,
    read_consent,
    report_build,
)
from jaa.core.errors import TailoringError
from jaa.core.models import JsonObject, JsonValue, TemplateKind
from jaa.documents.content import CvLanguage
from jaa.documents.engine import find_engine
from jaa.documents.pipeline import BuildOptions
from jaa.documents.tailoring import Change, Check, EditKind, plan_from_json
from jaa.storage import applications as tracker
from jaa.storage import documents as artifacts
from jaa.storage import tailoring as service
from jaa.storage.preferences import paper_size
from jaa.storage.profile import active_version_id
from jaa.storage.templates import resolve_template

tailor_app = typer.Typer(
    help="Tailor your CV for one job: review each change and its evidence, then build it.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals hold CV contents
)

MAX_PLAN_BYTES: Final = 1_000_000
JobArg = Annotated[int, typer.Argument(help="Job id, see `jaa jobs list`.")]
RevisionOpt = Annotated[
    int | None, typer.Option("--revision", "-r", help="Revision to use (default: the latest).")
]


def _status(check: Check) -> Text:
    if not check.issues:
        return Text("ok", style="green")
    messages = "; ".join(i.message for i in check.issues)
    if check.approved and check.approvable:
        return Text(f"approved: {messages}", style="yellow")
    style = "red"
    hint = "" if check.approvable else " (cannot be approved)"
    return Text(f"blocked{hint}: {messages}", style=style)


def _change_text(kind: EditKind, change: Change | None, target_text: str | None) -> Text:
    if change is None:
        return Text(target_text or "-", style="dim")
    if kind is EditKind.HIDE:
        return Text(f"hide: {change.before or ''}")
    if kind is EditKind.MOVE:
        return Text(f"{change.before} → {change.after}")
    if kind is EditKind.EMPHASIZE:
        return Text(f"bold: {change.after or ''}")
    return Text(f"{change.before or ''}\n→ {change.after or ''}")


def _facts(ids: tuple[int, ...]) -> str:
    return ", ".join(f"#{i}" for i in ids) or "-"


def _show(evaluated: service.Evaluated, active_version: int | None, *, full: bool) -> None:
    job, row, plan, result = (
        evaluated.job,
        evaluated.row,
        evaluated.plan,
        evaluated.result,
    )
    console.print(Text(f"Tailored CV for job #{job.id}: {job.title} at {job.employer}"))
    blocked = sum(c.blocked for c in (*result.edit_checks, *result.paragraph_checks))
    console.print(
        f"Revision {row.revision} ({row.origin.value}), profile version "
        f"{row.profile_version_id}, {len(plan.edits)} edit(s), {blocked} blocked."
    )
    if active_version != row.profile_version_id:
        console.print(
            "[yellow]Your profile changed since this revision. It still builds from profile "
            f"version {row.profile_version_id}; run `jaa cv tailor propose {job.id}` or "
            f"`edit` to tailor the current profile.[/yellow]"
        )
    if not plan.edits:
        console.print("No changes: the tailored CV is your baseline CV.")
    else:
        changes = {c.number: c for c in result.changes}
        table = Table("#", "Edit", "Target", "Change", "Facts", "Why", "Status", show_lines=True)
        for number, (edit, check) in enumerate(
            zip(plan.edits, result.edit_checks, strict=True), start=1
        ):
            table.add_row(
                str(number),
                edit.kind.value,
                Text(edit.target),
                _change_text(edit.kind, changes.get(number), edit.text),
                _facts(check.fact_ids),
                Text(edit.reason or ""),
                _status(check),
            )
        console.print(table)
    if plan.cover_letter:
        console.print("[bold]Cover letter draft[/bold]")
        for paragraph, check in zip(plan.cover_letter, result.paragraph_checks, strict=True):
            console.print(Text(f"{check.number}. {paragraph.text}"))
            console.print(
                Text(f"   facts {_facts(check.fact_ids)} · "), _status(check), sep="", end="\n"
            )
    if full:
        console.print("[bold]Tailored CV[/bold]")
        outline(result.content, CvLanguage.EN)
    if result.blocked:
        console.print(
            f"Blocked edits are not applied. Change them with `jaa cv tailor edit {job.id}`, "
            f"or approve them with `jaa cv tailor approve {job.id} --edit N` (or `--paragraph N`)."
        )
    else:
        console.print(f"Build it with `jaa cv tailor build {job.id}`.")
        if plan.cover_letter:
            console.print(f"Build the cover letter with `jaa cv tailor letter {job.id}`.")


@tailor_app.command()
def propose(ctx: typer.Context, job_id: JobArg) -> None:
    """Propose a tailored CV for a job: reorder and emphasize what its match found."""
    with database(ctx) as conn:
        evaluated = service.propose(conn, job_id)
        active = active_version_id(conn)
        conflicts = tracker.find_application_conflicts(conn, job_id)
    _show(evaluated, active, full=False)
    print_conflicts(conflicts)


@tailor_app.command()
def show(
    ctx: typer.Context,
    job_id: JobArg,
    revision: RevisionOpt = None,
    full: Annotated[bool, typer.Option("--full", help="Also print the tailored CV.")] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the plan as JSON, to edit with `--file`.")
    ] = False,
) -> None:
    """Show every change against your baseline CV, with the facts behind it."""
    with database(ctx) as conn:
        evaluated = service.evaluate(conn, job_id, revision)
        active = active_version_id(conn)
    if as_json:
        print(json.dumps(evaluated.plan.to_json(), ensure_ascii=False, indent=2))  # noqa: T201 - machine-readable output
        return
    _show(evaluated, active, full=full)


def _read_plan(path: Path) -> JsonValue:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise typer.BadParameter(f"not a file: {resolved}", param_hint="--file")
    if resolved.stat().st_size > MAX_PLAN_BYTES:
        raise typer.BadParameter("plan file is larger than 1 MB", param_hint="--file")
    return _parse(resolved.read_text(encoding="utf-8"))


def _parse(text: str) -> JsonValue:
    try:
        data: JsonValue = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TailoringError(f"the plan is not valid JSON: {exc}") from exc
    return data


_EDIT_HELP: Final = """\
// Tailoring plan. Edit kinds: hide, move (needs "position"), emphasize, rewrite (needs "text").
// Targets: headline, summary, experience.<id>, education.<id>,
//   experience.<id>.description[<n>], skills.<key>, certifications.<key>.
// A rewrite or cover letter paragraph must cite confirmed facts in "fact_ids" that back its
// wording. Lines starting with // are ignored.
"""


def _edit_in_editor(text: str) -> str | None:
    """Open `text` in the user's editor ($VISUAL, $EDITOR, else vi). None if it fails.

    The file holds CV text, so it lives in a private temporary folder and is removed after.
    """
    command = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    with tempfile.TemporaryDirectory(prefix="jaa-") as folder:
        path = Path(folder) / "plan.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            finished = subprocess.run([*command, str(path)], check=False)  # noqa: S603 - the user's own editor
        except OSError as exc:
            err_console.print(f"[red]Cannot start the editor:[/red] {escape(str(exc))}")
            return None
        if finished.returncode != 0:
            err_console.print("[red]The editor exited with an error; nothing saved.[/red]")
            return None
        return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


@tailor_app.command()
def edit(
    ctx: typer.Context,
    job_id: JobArg,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="Plan JSON to save (default: open your editor)."),
    ] = None,
) -> None:
    """Edit the plan (hide, move, emphasize, reword; cover letter) and save a new revision."""
    with database(ctx) as conn:
        if file is not None:
            data = _read_plan(file)
        else:
            current = service.evaluate(conn, job_id) if service.latest(conn, job_id) else None
            plan: JsonObject = current.plan.to_json() if current else {"edits": []}
            text = _EDIT_HELP + json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
            edited = _edit_in_editor(text)
            if edited is None or edited == text:
                console.print("No changes.")
                return
            data = _parse(_strip_comments(edited))
        evaluated = service.save_edited(conn, job_id, plan_from_json(data))
        active = active_version_id(conn)
    _show(evaluated, active, full=False)


@tailor_app.command()
def approve(
    ctx: typer.Context,
    job_id: JobArg,
    edits: Annotated[
        list[int] | None, typer.Option("--edit", "-e", help="Edit number to approve.")
    ] = None,
    paragraphs: Annotated[
        list[int] | None,
        typer.Option("--paragraph", "-p", help="Cover letter paragraph number to approve."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Approve blocked edits whose wording is not backed by the facts they cite."""
    edits, paragraphs = edits or [], paragraphs or []
    if not edits and not paragraphs:
        raise typer.BadParameter("name at least one --edit or --paragraph")
    with database(ctx) as conn:
        current = service.evaluate(conn, job_id)
        checks = [c for c in current.result.edit_checks if c.number in edits] + [
            c for c in current.result.paragraph_checks if c.number in paragraphs
        ]
        for check in checks:
            console.print(Text(f"#{check.number}: "), _status(check), sep="")
        if not yes and not typer.confirm(
            "These go on your CV although your confirmed facts do not back them. Approve?"
        ):
            raise typer.Exit(code=1)
        evaluated = service.approve(conn, job_id, edits=edits, paragraphs=paragraphs)
        active = active_version_id(conn)
    _show(evaluated, active, full=False)


@tailor_app.command()
def build(
    ctx: typer.Context,
    job_id: JobArg,
    revision: RevisionOpt = None,
    template: Annotated[
        str | None,
        typer.Option(
            "--template",
            "-t",
            help="Template id (default: your default), see `jaa templates list`.",
        ),
    ] = None,
    language: LanguageOption = CvLanguage.EN,
    paper: PaperOption = None,
    photo: Annotated[
        Path | None, typer.Option("--photo", help="JPEG or PNG photo to show in the header.")
    ] = None,
    consent_file: Annotated[
        Path | None,
        typer.Option("--consent-file", help="UTF-8 text of your consent clause."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Folder for the PDF (default: data dir/documents)."),
    ] = None,
    open_pdf: Annotated[bool, typer.Option("--open", help="Open the PDF when done.")] = False,
) -> None:
    """Build the tailored CV PDF for a job and link it to the job and profile version."""
    settings = settings_from(ctx)
    consent = read_consent(consent_file) if consent_file else None
    with database(ctx) as conn:
        row = service.get_revision(conn, job_id, revision)
        if active_version_id(conn) != row.profile_version_id:
            console.print(
                f"[yellow]Building from profile version {row.profile_version_id}, "
                "not your current profile.[/yellow]"
            )
        chosen_template = resolve_template(conn, template)
        engine = find_engine()
        console.print(
            f"Building revision {row.revision} for job #{job_id} with "
            f"{chosen_template.version_label} and {engine.name}…"
        )
        built = artifacts.build_tailored_cv(
            conn,
            job_id,
            revision=row.revision,
            template=chosen_template,
            engine=engine,
            output_dir=output or settings.documents_dir,
            options=BuildOptions(
                language=language,
                paper=paper_size(conn, paper),
                photo=photo,
                consent_text=consent,
            ),
        )
    console.print(f"{built.changes} change(s) applied.")
    report_build(built.output, built.pdf.id)
    if open_pdf:
        typer.launch(str(built.output.pdf))


@tailor_app.command()
def letter(
    ctx: typer.Context,
    job_id: JobArg,
    revision: RevisionOpt = None,
    template: Annotated[
        str | None,
        typer.Option(
            "--template",
            "-t",
            help="Cover letter template id (default: your default), see `jaa templates list`.",
        ),
    ] = None,
    language: LanguageOption = CvLanguage.EN,
    paper: PaperOption = None,
    recipient: Annotated[
        list[str] | None,
        typer.Option(
            "--recipient",
            help="A recipient line, repeated for each line: name first, then the address "
            "(default: the job's employer and location).",
        ),
    ] = None,
    opening: Annotated[
        str | None,
        typer.Option("--opening", help='Your greeting, e.g. "Dear Hiring Team," (default: none).'),
    ] = None,
    closing: Annotated[
        str | None,
        typer.Option("--closing", help='Your closing, e.g. "Kind regards," (default: none).'),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Folder for the PDF (default: data dir/documents)."),
    ] = None,
    open_pdf: Annotated[bool, typer.Option("--open", help="Open the PDF when done.")] = False,
) -> None:
    """Build the cover letter PDF for a job from the plan's checked paragraphs."""
    settings = settings_from(ctx)
    with database(ctx) as conn:
        row = service.get_revision(conn, job_id, revision)
        if active_version_id(conn) != row.profile_version_id:
            console.print(
                f"[yellow]Building from profile version {row.profile_version_id}, "
                "not your current profile.[/yellow]"
            )
        chosen_template = resolve_template(conn, template, TemplateKind.COVER_LETTER)
        engine = find_engine()
        console.print(
            f"Building the cover letter of revision {row.revision} for job #{job_id} with "
            f"{chosen_template.version_label} and {engine.name}…"
        )
        built = artifacts.build_cover_letter(
            conn,
            job_id,
            revision=row.revision,
            template=chosen_template,
            engine=engine,
            output_dir=output or settings.documents_dir,
            language=language,
            paper=paper_size(conn, paper),
            recipient=tuple(recipient or ()),
            opening=opening,
            closing=closing,
        )
    console.print(f"{built.paragraphs} paragraph(s).")
    report_build(built.output, built.pdf.id, "Cover letter")
    if open_pdf:
        typer.launch(str(built.output.pdf))


@tailor_app.command("list")
def list_revisions(ctx: typer.Context, job_id: JobArg) -> None:
    """List a job's tailoring revisions."""
    with database(ctx) as conn:
        rows = service.revisions(conn, job_id)
    if not rows:
        err_console.print(
            escape(f"No tailored CV for job {job_id} yet. Run `jaa cv tailor propose {job_id}`.")
        )
        return
    table = Table("Revision", "Created (UTC)", "Origin", "Profile version", "Edits", "Blocked")
    for row in rows:
        edits = row.plan.get("edits")
        table.add_row(
            str(row.revision),
            f"{row.created_at:%Y-%m-%d %H:%M}",
            row.origin.value,
            str(row.profile_version_id),
            str(len(edits) if isinstance(edits, list) else 0),
            "yes" if row.blocked else "no",
        )
    console.print(table)
