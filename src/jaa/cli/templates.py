"""`jaa templates …`: list, preview, check, add, choose and delete CV templates (#40, ADR 0029).

Shipped templates come with jaa; uploaded ones are stored in the database after passing the
template contract. Previews use synthetic content only, never the user's profile, and are not
recorded as documents. Template files can hold the user's own CV text, so they are never
printed or logged; only paths, ids and counts are.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jaa.cli.common import YesOpt, confirm_or_exit, console, err_console, settings_from
from jaa.core.errors import JaaError, LatexCompileError
from jaa.core.models import PaperSize, TemplateKind
from jaa.documents.content import CvLanguage
from jaa.documents.contract import TemplateCheck, TemplateFiles
from jaa.documents.engine import find_engine
from jaa.documents.latex import available_templates, get_template
from jaa.documents.pipeline import BuildOptions, BuildOutput, build_cover_letter, build_cv
from jaa.documents.sample import sample_cv, sample_letter
from jaa.importers.template import candidate_template, read_template
from jaa.storage import templates as service
from jaa.storage.db import open_database
from jaa.storage.repository import template_candidates

templates_app = typer.Typer(
    help="List, preview, add and choose CV and cover letter templates.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # locals can hold template files with CV text
)

KindOpt = Annotated[TemplateKind, typer.Option("--kind", "-k", help="CV or cover letter template.")]
SourceArg = Annotated[
    Path | None,
    typer.Argument(help="A .tex file, a folder or a .zip archive with the template."),
]
MainOpt = Annotated[
    str | None,
    typer.Option("--main", help="The main .tex file, when the folder or archive has several."),
]
CandidateOpt = Annotated[
    int | None,
    typer.Option("--candidate", help="Use a template candidate (`jaa import cv --as-template`)."),
]


@contextmanager
def template_database(
    ctx: typer.Context, *, required: bool = True
) -> Generator[sqlite3.Connection | None]:
    settings = settings_from(ctx)
    exists = settings.database_path.is_file()
    if required and not exists:
        err_console.print("[red]No database yet.[/red] Run `jaa init` first.")
        raise typer.Exit(code=1)
    conn = open_database(settings.database_path) if exists else None
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
        if conn is not None:
            conn.close()


def _connection(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    if conn is None:  # only when required=False
        raise typer.Exit(code=1)
    return conn


def list_all(conn: sqlite3.Connection | None) -> None:
    """Print shipped and uploaded templates, marking each kind's default."""
    defaults = {
        kind: service.default_template_id(conn, kind) if conn else service.SHIPPED_DEFAULTS[kind]
        for kind in TemplateKind
    }
    table = Table("ID", "Version", "Kind", "Origin", "Name", "License")
    rows: list[tuple[str, int, TemplateKind, str, str, str]] = [
        (t.id, t.version, t.kind, "shipped", t.name, t.license)
        for t in available_templates().values()
    ]
    if conn is not None:
        rows += [
            (t.template_id, t.version, t.kind, f"added ({t.origin.value})", t.name, "-")
            for t in service.latest_user_templates(conn)
        ]
    for template_id, version, kind, origin, name, license_ in rows:
        marked = template_id == defaults[kind].partition("@")[0]
        table.add_row(
            template_id + (" (default)" if marked else ""),
            str(version),
            kind.value.replace("_", " "),
            origin,
            Text(name),
            Text(license_),
        )
    console.print(table)
    console.print("Preview one with `jaa templates show ID --preview`.")


@templates_app.command("list")
def list_templates(ctx: typer.Context) -> None:
    """List shipped and added templates; the default of each kind is marked."""
    with template_database(ctx, required=False) as conn:
        list_all(conn)


def _report_preview(out: BuildOutput) -> None:
    console.print(f"[green]Preview written:[/green] {out.pdf} ({out.pages} page(s))")
    for warning in out.warnings:
        console.print(Text(f"Layout: {warning}", style="yellow"))
    if out.missing_text:
        console.print("[yellow]Not found in the PDF's text (an ATS risk):[/yellow]")
        for text in out.missing_text:
            console.print(Text(f"  - {text}"))


@templates_app.command()
def show(
    ctx: typer.Context,
    template_ref: Annotated[str, typer.Argument(metavar="ID", help="Template id, or id@version.")],
    kind: KindOpt = TemplateKind.CV,
    preview: Annotated[
        bool, typer.Option("--preview", help="Build a PDF from synthetic sample data.")
    ] = False,
    language: Annotated[
        CvLanguage, typer.Option("--language", "-l", help="Language of the preview.")
    ] = CvLanguage.EN,
    paper: Annotated[
        PaperSize,
        typer.Option("--paper", help="Paper size of the preview.", case_sensitive=False),
    ] = PaperSize.A4,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Folder for the preview (default: data dir)."),
    ] = None,
    open_pdf: Annotated[bool, typer.Option("--open", help="Open the preview when done.")] = False,
) -> None:
    """Describe a template and optionally build a preview from synthetic data."""
    settings = settings_from(ctx)
    with template_database(ctx, required=False) as conn:
        if conn is None:
            template = get_template(template_ref, kind)
        else:
            template = service.resolve_template(conn, template_ref, kind)
        console.print(Text(f"{template.version_label}: {template.name}", style="bold"))
        console.print(Text(f"Kind: {template.kind.value.replace('_', ' ')}"))
        console.print(Text(f"Family: {template.family.value}"))
        if template.description:
            console.print(Text(template.description))
        console.print(Text(f"License: {template.license}"))
        console.print(Text(f"SHA-256: {template.sha256}"))
        if template.uploaded:
            console.print(Text(f"Files: {', '.join(sorted(template.assets)) or 'main file only'}"))
        if not preview:
            return
        engine = find_engine()
        folder = output or settings.data_dir / "previews"
        console.print(f"Building a preview with {engine.name}…")
        if template.kind is TemplateKind.CV:
            out = build_cv(
                sample_cv(language),
                template=template,
                engine=engine,
                output_dir=folder,
                options=BuildOptions(language=language, paper=paper),
                profile_version_id=None,
            )
        else:
            out = build_cover_letter(
                sample_letter(language),
                template=template,
                engine=engine,
                output_dir=folder,
                language=language,
                profile_version_id=None,
                job_id=None,
                fact_ids=(),
                tailoring=None,
                paper=paper,
            )
    _report_preview(out)
    if open_pdf:
        typer.launch(str(out.pdf))


def _read(
    conn: sqlite3.Connection | None, source: Path | None, main: str | None, candidate: int | None
) -> TemplateFiles:
    if (source is None) == (candidate is None):
        raise typer.BadParameter("give either a file, folder or archive, or --candidate ID")
    if source is not None:
        return read_template(source, main=main)
    if conn is None or candidate is None:
        raise typer.BadParameter("--candidate needs a database; run `jaa init` first")
    return candidate_template(template_candidates.get(conn, candidate))


def _report_check(files: TemplateFiles, check: TemplateCheck) -> None:
    console.print(
        Text(
            f"Main file {files.main_file}, {len(files.texts)} text file(s), "
            f"{len(files.assets)} other file(s); family {check.family.value}"
            + (f" (\\documentclass{{{check.document_class}}})" if check.document_class else "")
        )
    )
    if files.skipped:
        console.print(Text(f"Skipped: {', '.join(files.skipped)}", style="dim"))
    for warning in check.warnings:
        console.print(Text(f"Warning: {warning}", style="yellow"))
    for problem in check.problems:
        console.print(Text(f"Problem: {problem}", style="red"))
    if check.ok:
        console.print("[green]The template meets the template contract.[/green]")
    else:
        console.print("See docs/templates.md for the slot lines and macros a template needs.")


@templates_app.command()
def check(
    ctx: typer.Context,
    source: SourceArg = None,
    kind: KindOpt = TemplateKind.CV,
    main: MainOpt = None,
    candidate: CandidateOpt = None,
) -> None:
    """Check a template against the template contract without adding it."""
    with template_database(ctx, required=False) as conn:
        files = _read(conn, source, main, candidate)
    result = files.check(kind)
    _report_check(files, result)
    if not result.ok:
        raise typer.Exit(code=1)


@templates_app.command()
def add(
    ctx: typer.Context,
    source: SourceArg = None,
    template_name: Annotated[
        str,
        typer.Option("--id", help="Id for the template; it is stored as user-<id>."),
    ] = "",
    kind: KindOpt = TemplateKind.CV,
    main: MainOpt = None,
    candidate: CandidateOpt = None,
    title: Annotated[str | None, typer.Option("--name", help="A display name.")] = None,
    description: Annotated[
        str, typer.Option("--description", help="What the template looks like.")
    ] = "",
) -> None:
    """Check a template and add it, or add a new version of one you added before."""
    if not template_name:
        raise typer.BadParameter("name the template with --id", param_hint="--id")
    with template_database(ctx) as conn:
        db = _connection(conn)
        files = _read(db, source, main, candidate)
        result = files.check(kind)
        _report_check(files, result)
        if not result.ok:
            err_console.print("[red]Not added.[/red] Fix the problems above and add it again.")
            raise typer.Exit(code=1)
        stored, created = service.add_user_template(
            db,
            files,
            name=template_name,
            kind=kind,
            title=title,
            description=description,
            template_candidate_id=candidate,
        )
    if created:
        console.print(f"[green]Added {stored.version_label}.[/green]")
    else:
        console.print(f"These files are already stored as {stored.version_label}.")
    console.print(
        f"Preview it with `jaa templates show {stored.template_id} --preview`, or build with "
        f"`jaa cv build --template {stored.template_id}`."
    )


@templates_app.command()
def default(
    ctx: typer.Context,
    template_ref: Annotated[str, typer.Argument(metavar="ID", help="Template id, or id@version.")],
    kind: KindOpt = TemplateKind.CV,
) -> None:
    """Choose the template used when a build names none."""
    with template_database(ctx) as conn:
        row = service.set_default_template(_connection(conn), template_ref, kind)
    console.print(
        f"Default {row.kind.value.replace('_', ' ')} template: {row.template_id}. "
        "`--template` still overrides it for one build."
    )


@templates_app.command()
def delete(
    ctx: typer.Context,
    template_id: Annotated[str, typer.Argument(metavar="ID", help="An added template's id.")],
    yes: YesOpt = False,
) -> None:
    """Delete every version of a template you added. PDFs built with it are kept."""
    with template_database(ctx) as conn:
        db = _connection(conn)
        versions = service.user_templates(db, template_id)
        if not versions:
            err_console.print(f"[red]Error:[/red] no added template {escape(template_id)!r}")
            raise typer.Exit(code=1)
        confirm_or_exit(
            f"Delete {template_id} ({len(versions)} version(s))? This cannot be undone.", yes=yes
        )
        count = service.delete_user_template(db, template_id)
    console.print(f"Deleted {count} version(s) of {template_id}.")
