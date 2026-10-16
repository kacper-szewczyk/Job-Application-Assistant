"""`jaa cv …`: preview and build CV PDFs from the confirmed profile (T08, ADR 0012).

Only confirmed facts are used. Unreviewed facts are listed before a build and can be confirmed
there through the normal review workflow (which creates a new profile version); the rest are
left out and recorded as such in the manifest. CV text is printed for the user, never logged.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import MASK, console, err_console, open_existing, settings_from
from jaa.cli.templates import list_all, template_database
from jaa.core.errors import FactConflictError, JaaError, LatexCompileError, ReviewError
from jaa.core.models import ArtifactKind, FactState, PaperSize, ProfileFact
from jaa.documents.content import CvContent, CvLanguage, build_content, format_period
from jaa.documents.engine import find_engine
from jaa.documents.pipeline import BuildOptions, BuildOutput
from jaa.storage import documents as artifacts
from jaa.storage import profile as service
from jaa.storage.preferences import paper_size
from jaa.storage.repository import profile_facts
from jaa.storage.templates import resolve_template

cv_app = typer.Typer(
    help="Preview and build CV PDFs from your confirmed profile.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals hold CV contents
)

MAX_CONSENT_BYTES: Final = 10_000
MAX_SHOWN: Final = 70

LanguageOption = Annotated[
    CvLanguage, typer.Option("--language", "-l", help="Language of headings and dates.")
]
PaperOption = Annotated[
    PaperSize | None,
    typer.Option(
        "--paper",
        help="Paper size (default: your documents.paper_size preference, else a4).",
        case_sensitive=False,
    ),
]


@contextmanager
def database(ctx: typer.Context) -> Generator[sqlite3.Connection]:
    conn = open_existing(settings_from(ctx))
    try:
        yield conn
    except LatexCompileError as exc:
        err_console.print(f"[red]Build failed:[/red] {escape(str(exc))}")
        for problem in exc.problems:
            err_console.print(Text(f"  - {problem}"))
        raise typer.Exit(code=1) from exc
    except JaaError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()


def _confirmed(conn: sqlite3.Connection) -> tuple[int | None, list[ProfileFact]]:
    version = service.active_version_id(conn)
    if version is None:
        return None, []
    return version, [profile_facts.get(conn, i) for i in service.version_fact_ids(conn, version)]


def _shown(fact: ProfileFact) -> str:
    if fact.is_sensitive:
        return MASK
    text = " ".join(fact.value_raw.split())
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "…"


def _parse_selection(text: str, ids: list[int]) -> list[int]:
    text = text.strip().lower()
    if not text:
        return []
    if text == "all":
        return ids
    chosen: list[int] = []
    for part in text.replace(",", " ").split():
        if not part.isdigit() or int(part) not in ids:
            raise typer.BadParameter(f"{part!r} is not one of the listed fact ids")
        chosen.append(int(part))
    return chosen


def _review_unreviewed(conn: sqlite3.Connection) -> list[ProfileFact]:
    """Offer unreviewed facts for confirmation. Returns those still unreviewed afterwards."""
    pending = profile_facts.list(conn, state=FactState.UNREVIEWED)
    if not pending:
        return []
    console.print(
        f"[yellow]{len(pending)} unreviewed fact(s) are not in your confirmed profile.[/yellow]"
    )
    table = Table("ID", "Field", "Value")
    for fact in pending:
        table.add_row(str(fact.id), Text(fact.field_key), Text(_shown(fact)))
    console.print(table)
    ids = [f.id for f in pending if f.id is not None]
    while True:
        answer: str = typer.prompt(
            "Confirm which for this CV? (ids, 'all', or Enter for none)",
            default="",
            show_default=False,
        )
        try:
            chosen = _parse_selection(answer, ids)
            break
        except typer.BadParameter as exc:
            err_console.print(f"[red]{escape(str(exc))}[/red]")
    for fact_id in chosen:
        try:
            service.confirm_facts(conn, [fact_id])
        except FactConflictError:
            err_console.print(
                f"#{fact_id} conflicts with a confirmed value; resolve it with "
                "`jaa profile conflicts`. Left out."
            )
        except ReviewError as exc:
            err_console.print(f"#{fact_id}: {escape(str(exc))}. Left out.")
    return profile_facts.list(conn, state=FactState.UNREVIEWED)


def outline(content: CvContent, language: CvLanguage) -> None:
    def line(label: str, value: str | None) -> None:
        if value:
            console.print(Text(f"{label}: {value}"))

    line("Name", content.name)
    line("Headline", content.headline)
    line("Contact", " | ".join(x for x in (content.email, content.phone, content.location) if x))
    line("Links", " | ".join(url for _, url in content.links))
    line("Summary", content.summary)
    for title, entries in (("Experience", content.experience), ("Education", content.education)):
        if entries:
            console.print(f"[bold]{title}[/bold]")
        for entry in entries:
            period = format_period(entry.dates, language) or "no dates"
            parts = [p for p in (entry.role, entry.organization, entry.location) if p]
            console.print(Text(f"  {period}  {', '.join(parts)}"))
            for item in entry.items:
                console.print(Text(f"    • {item}"))
    line("Skills", ", ".join(content.skills))
    line(
        "Languages",
        ", ".join(f"{x.name} ({x.level})" if x.level else x.name for x in content.languages),
    )
    line("Certificates", ", ".join(c.name for c in content.certifications))


def report_unused(unused: tuple[ProfileFact, ...]) -> None:
    if unused:
        keys = ", ".join(sorted(f.field_key for f in unused))
        console.print(Text(f"Not shown by this template: {keys}", style="dim"))


@cv_app.command()
def preview(ctx: typer.Context, language: LanguageOption = CvLanguage.EN) -> None:
    """Show what a CV would contain, without building a PDF."""
    with database(ctx) as conn:
        version, facts = _confirmed(conn)
        pending = len(profile_facts.list(conn, state=FactState.UNREVIEWED))
    if not facts:
        console.print("No confirmed facts yet. Import a CV and confirm facts with `jaa profile`.")
        return
    result = build_content(facts)
    console.print(f"Profile version {version}, {len(result.content.fact_ids)} facts used.")
    outline(result.content, language)
    report_unused(result.unused)
    if pending:
        console.print(f"{pending} unreviewed fact(s) are left out; `jaa cv build` offers them.")


def read_consent(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise typer.BadParameter(f"not a file: {resolved}", param_hint="--consent-file")
    data = resolved.read_bytes()
    if len(data) > MAX_CONSENT_BYTES:
        raise typer.BadParameter("consent clause is longer than 10 kB", param_hint="--consent-file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise typer.BadParameter("consent file must be UTF-8 text") from exc


@cv_app.command()
def build(
    ctx: typer.Context,
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
        typer.Option(
            "--consent-file",
            help="UTF-8 text of your consent clause (e.g. the Polish RODO clause), "
            "printed at the end.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Folder for the PDF (default: data dir/documents)."),
    ] = None,
    confirmed_only: Annotated[
        bool,
        typer.Option("--confirmed-only", help="Do not offer unreviewed facts; use confirmed ones."),
    ] = False,
    open_pdf: Annotated[bool, typer.Option("--open", help="Open the PDF when done.")] = False,
) -> None:
    """Build a CV PDF from confirmed facts and record it with its manifest."""
    settings = settings_from(ctx)
    consent = read_consent(consent_file) if consent_file else None
    with database(ctx) as conn:
        chosen_template = resolve_template(conn, template)
        engine = find_engine()
        if not confirmed_only:
            _review_unreviewed(conn)
        if not _confirmed(conn)[1]:
            err_console.print(
                "No confirmed facts yet. Import a CV and confirm facts with `jaa profile`."
            )
            raise typer.Exit(code=1)
        console.print(f"Building with {chosen_template.version_label} and {engine.name}…")
        built = artifacts.build_profile_cv(
            conn,
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
    console.print(f"{built.facts_used} facts used, profile version {built.profile_version_id}.")
    report_build(built.output, built.pdf.id)
    report_unused(built.unused)
    if built.left_out:
        console.print(f"{len(built.left_out)} unreviewed fact(s) were left out.")
    if open_pdf:
        typer.launch(str(built.output.pdf))


def report_build(out: BuildOutput, document_id: int | None, what: str = "CV") -> None:
    """Where the PDF went, and the layout and text-extraction warnings."""
    console.print(
        f"[green]{what} written:[/green] {out.pdf} ({out.pages} page(s), document #{document_id})"
    )
    console.print(f"LaTeX source: {out.tex}")
    for warning in out.warnings:
        console.print(Text(f"Layout: {warning}", style="yellow"))
    if out.missing_text:
        console.print(
            "[yellow]Not found in the PDF's text (applicant tracking systems may miss it):[/yellow]"
        )
        for text in out.missing_text:
            console.print(Text(f"  - {text}"))


@cv_app.command("list")
def list_cvs(ctx: typer.Context) -> None:
    """List generated CVs and cover letters with their template and profile version."""
    with database(ctx) as conn:
        cvs = artifacts.list_pdfs(conn)
    if not cvs:
        console.print("No CVs built yet. Run `jaa cv build`.")
        return
    table = Table("ID", "Created (UTC)", "Kind", "Job", "Template", "Profile version", "File")
    for doc in cvs:
        exists = "" if Path(doc.path).is_file() else " (missing)"
        table.add_row(
            str(doc.id),
            f"{doc.created_at:%Y-%m-%d %H:%M}",
            "cover letter" if doc.kind is ArtifactKind.COVER_LETTER_PDF else "CV",
            str(doc.job_id or "-"),
            doc.template_version or "-",
            str(doc.profile_version_id or "-"),
            Text(doc.path + exists),
        )
    console.print(table)


@cv_app.command()
def templates(ctx: typer.Context) -> None:
    """List CV and cover letter templates (same as `jaa templates list`)."""
    with template_database(ctx, required=False) as conn:
        list_all(conn)
