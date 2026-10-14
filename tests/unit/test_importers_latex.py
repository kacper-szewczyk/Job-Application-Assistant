from pathlib import Path

import pytest

from jaa.core.models import FactCandidate
from jaa.importers.cv import ParsedCV, Section, parse_cv
from jaa.importers.latex import (
    LatexProject,
    LatexResult,
    decode_tex,
    extract_latex,
    is_latex,
    is_latex_fragment,
    read_latex_project,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "latex"


def project(path: Path) -> LatexProject:
    return read_latex_project(path, path.read_bytes())


def from_text(text: str, **extra: str) -> LatexResult:
    return extract_latex(LatexProject(main="cv.tex", files={"cv.tex": text, **extra}))


def body(text: str) -> list[str]:
    doc = f"\\documentclass{{article}}\n\\begin{{document}}\n{text}\n\\end{{document}}\n"
    return [line.text for line in from_text(doc).extracted.lines]


def codes(result: LatexResult) -> list[str]:
    return [i.code for i in result.extracted.issues]


def by_key(parsed: ParsedCV) -> dict[str, FactCandidate]:
    return {c.field_key: c for c in parsed.candidates}


# --- detection -------------------------------------------------------------------------------


def test_detection() -> None:
    assert is_latex(b"%cv\n\\documentclass{moderncv}")
    assert is_latex(b"\\begin{document}x\\end{document}")
    assert not is_latex(b"\\documentclass\x00binary")
    assert not is_latex(b"plain text CV")
    assert is_latex_fragment(b"\\section{Experience}\n\\cventry{a}{b}{c}{d}{e}{f}")
    assert not is_latex_fragment(b"plain text CV")


def test_decode_falls_back_to_latin1() -> None:
    assert decode_tex("Łódź".encode()) == ("Łódź", False)
    assert decode_tex("café".encode("latin-1")) == ("café", True)


# --- the sample CVs --------------------------------------------------------------------------


def test_moderncv_sample_produces_facts_and_sections() -> None:
    result = extract_latex(project(FIXTURES / "moderncv.tex"))
    parsed = parse_cv(result.extracted.lines)
    facts = by_key(parsed)

    assert result.document_class == "moderncv"
    assert facts["identity.name"].value_normalized == "Avery Synthetic"
    assert facts["contact.email"].value_normalized == "avery.synthetic@example.test"
    assert facts["contact.phone"].value_normalized == "+442079460958"
    assert facts["contact.links.github"].value_normalized == "https://github.com/avery-synthetic"
    assert facts["contact.links.linkedin"].value_normalized == (
        "https://linkedin.com/in/avery-synthetic"
    )
    widgets = "experience.example-widgets-ltd"
    assert facts[f"{widgets}.title"].value_normalized == "Senior Widget Engineer"
    assert facts[f"{widgets}.employer"].value_normalized == "Example Widgets Ltd"
    # Named \cventry arguments are more reliable than a heuristic split.
    assert facts[f"{widgets}.title"].confidence == 0.8
    assert facts[f"{widgets}.dates"].value_normalized == {
        "start": "2021-01",
        "end": None,
        "current": True,
    }
    assert facts[f"{widgets}.description"].value_normalized == [
        "Led the migration of widget pipelines to event streaming.",
        "Mentored four engineers.",
    ]
    assert facts["experience.example-gadgets.description"].value_normalized == [
        "Built gadget inventory APIs."
    ]
    uni = "education.university-of-exampleton"
    assert facts[f"{uni}.degree"].value_normalized == "BSc Computer Science"
    assert facts[f"{uni}.institution"].value_normalized == "University of Exampleton"
    assert {"skills.python", "skills.go", "skills.c-plus-plus", "skills.docker"} <= set(facts)
    assert facts["languages.german"].value_normalized == {"language": "German", "level": "B2"}

    assert parsed.headings == (
        ("Summary", Section.SUMMARY),
        ("Experience", Section.EXPERIENCE),
        ("Education", Section.EDUCATION),
        ("Skills", Section.SKILLS),
        ("Languages", Section.LANGUAGES),
        ("Projects", Section.UNSUPPORTED),
    )
    # Source lines in the .tex file are the provenance.
    assert facts["contact.email"].source_location == "line 8"
    assert facts[f"{widgets}.title"].source_location == "line 18 · Experience"


def test_moderncv_sample_reports_what_was_not_imported() -> None:
    result = extract_latex(project(FIXTURES / "moderncv.tex"))
    parsed = parse_cv(result.extracted.lines)
    assert codes(result) == [
        "latex_unknown_command",
        "latex_image_skipped",
        "latex_content_skipped",
    ]
    unknown = result.extracted.issues[0]
    assert "\\mystery" in unknown.message
    assert unknown.location == "line 33"
    messages = " ".join(i.message for i in parsed.issues)
    # Location, grade, headline and address are reported, and so is the Projects section.
    assert "Exampleton" in messages
    assert "First class" in messages
    assert "Backend Engineer" in messages
    assert "Projects" in messages


def test_awesome_cv_project_follows_includes_safely() -> None:
    proj = project(FIXTURES / "awesome" / "resume.tex")
    assert sorted(proj.files) == ["resume.tex", "resume/experience.tex", "resume/skills.tex"]
    assert [i.code for i in proj.issues] == ["latex_include_refused", "latex_include_missing"]

    result = extract_latex(proj)
    facts = by_key(parse_cv(result.extracted.lines))
    title = facts["experience.example-widgets-ltd.title"]
    assert title.value_normalized == "Senior Widget Engineer"
    assert title.source_location == "resume/experience.tex line 3 · Work Experience"
    assert facts["contact.links.website"].value_normalized == "https://avery.example.test"
    assert {"skills.python", "skills.go", "skills.rust"} <= set(facts)
    # The footer repeats the name and is layout, not content.
    assert sum(line.text == "Avery Synthetic" for line in result.extracted.lines) == 1


def test_include_outside_the_folder_is_never_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret.tex"
    secret.write_text("\\section{Skills}\nSecretSkill\n")
    cv_dir = tmp_path / "cv"
    cv_dir.mkdir()
    main = cv_dir / "cv.tex"
    main.write_text(
        "\\documentclass{article}\\begin{document}\n\\input{../secret}\n"
        f"\\input{{{secret}}}\n\\end{{document}}\n"
    )
    (cv_dir / "link.tex").symlink_to(secret)
    main.write_text(main.read_text().replace("\\end{document}", "\\input{link}\\end{document}"))

    proj = project(main)
    assert list(proj.files) == ["cv.tex"]
    assert [i.code for i in proj.issues] == ["latex_include_refused"] * 3
    assert "SecretSkill" not in " ".join(line.text for line in extract_latex(proj).extracted.lines)


def test_custom_macros_are_expanded() -> None:
    result = extract_latex(project(FIXTURES / "custom-macros.tex"))
    lines = result.extracted.lines
    assert [(line.text, line.is_bullet) for line in lines[3:7]] == [
        ("Example Corp | June 2020 – Present", False),
        ("Software Engineer | Remote", False),
        ("Built APIs with Flask", True),
        ("Cut costs by 20%", True),
    ]
    facts = by_key(parse_cv(lines))
    assert facts["identity.name"].value_normalized == "Jordan Example"
    assert facts["contact.links.linkedin"].value_normalized == (
        "https://linkedin.com/in/jordan-example"
    )
    assert facts["experience.example-corp.title"].value_normalized == "Software Engineer"
    assert {"skills.java", "skills.c-sharp", "skills.node-dot-js"} <= set(facts)
    assert codes(result) == []


# --- conversion details ----------------------------------------------------------------------


def test_accents_symbols_and_ligatures() -> None:
    assert body(
        "\\L\\'od\\'z, Caf\\'{e}, Stra\\ss e, na\\\"\\i ve\\\\"
        "C\\#, 20\\%, R\\&D, 2019--2021, ``quoted'' \\LaTeX~text"
    ) == ["Łódź, Café, Straße, naïve", "C#, 20%, R&D, 2019–2021, “quoted” LaTeX text"]


def test_comments_are_not_content() -> None:
    assert body("Visible % hidden secret\n\\begin{comment}\nalso hidden\n\\end{comment}") == [
        "Visible"
    ]


def test_tables_and_hfill_become_separators() -> None:
    assert body(
        "\\textbf{Example Corp} \\hfill 2019 -- 2021\\\\\n"
        "\\begin{tabular}{l r}\\textit{Engineer} & Remote \\\\ \\hline\\end{tabular}"
    ) == ["Example Corp | 2019 – 2021", "Engineer | Remote"]


def test_links_keep_targets_in_the_header_and_labels_elsewhere() -> None:
    assert body(
        "\\href{mailto:a@example.test}{\\faEnvelope} \\href{https://github.com/a}{GitHub}\n\n"
        "\\section{Projects}\n\\href{https://example.test}{Widgetizer}"
    ) == ["a@example.test https://github.com/a", "Projects", "Widgetizer"]


def test_description_list_labels() -> None:
    result = from_text(
        "\\documentclass{article}\\begin{document}\\section{Skills}\n"
        "\\begin{description}\\item[Languages] Python, Go\\end{description}\\end{document}"
    )
    line = result.extracted.lines[1]
    assert (line.text, line.is_bullet) == ("Languages: Python, Go", True)


def test_unknown_environment_naming_a_section_starts_it() -> None:
    result = from_text(
        "\\documentclass{resume}\\begin{document}\\begin{rSection}{Experience}\n"
        "Engineer at Example Corp\n\\end{rSection}\\end{document}"
    )
    first = result.extracted.lines[0]
    assert (first.text, first.is_heading) == ("Experience", True)
    assert codes(result) == ["latex_unknown_environment"]
    parsed = parse_cv(result.extracted.lines)
    assert by_key(parsed)["experience.example-corp.title"].value_normalized == "Engineer"


def test_user_environments_are_expanded() -> None:
    assert body(
        "\\newenvironment{jobs}[1]{\\section{#1}}{}\n"
        "\\begin{jobs}{Experience}Engineer at Example Corp\\end{jobs}"
    ) == ["Experience", "Engineer at Example Corp"]


def test_definitions_from_a_local_class_are_used() -> None:
    result = from_text(
        "\\documentclass{mycv}\\begin{document}\\job{Example Corp}{Engineer}\\end{document}",
        **{"mycv.cls": "\\newcommand{\\job}[2]{#2 at #1\\par}\\def\\@internal{x}"},
    )
    assert [line.text for line in result.extracted.lines] == ["Engineer at Example Corp"]
    assert codes(result) == []


def test_recursive_definitions_stop_with_a_warning() -> None:
    result = from_text(
        "\\documentclass{article}\\newcommand{\\loop}{a\\loop}"
        "\\begin{document}\\loop\\end{document}"
    )
    assert "latex_expansion_limit" in codes(result)


def test_delimited_definitions_are_reported() -> None:
    result = from_text(
        "\\documentclass{article}\\def\\pair#1,#2.{#1 and #2}"
        "\\begin{document}\\pair a,b.\\end{document}"
    )
    assert codes(result) == ["latex_unknown_command", "latex_definition_unsupported"]


def test_known_commands_cannot_be_redefined_away() -> None:
    # A user's restyled \section must still start a section.
    result = from_text(
        "\\documentclass{article}\\renewcommand{\\section}[1]{\\textbf{#1}}"
        "\\begin{document}\\section{Skills}Python\\end{document}"
    )
    assert result.extracted.lines[0].is_heading


def test_subfile_with_its_own_document() -> None:
    result = from_text(
        "\\documentclass{article}\\begin{document}\\subfile{skills}\\section{Education}"
        "\\end{document}",
        **{
            "skills.tex": "\\documentclass[cv.tex]{subfiles}\\begin{document}"
            "\\section{Skills}Python\\end{document}"
        },
    )
    assert [line.text for line in result.extracted.lines] == ["Skills", "Python", "Education"]
    assert result.document_class == "article"


def test_missing_begin_document_is_reported() -> None:
    result = extract_latex(
        LatexProject(main="cv.tex", files={"cv.tex": "\\section{Skills}\nPython\n"})
    )
    assert [line.text for line in result.extracted.lines] == ["Skills", "Python"]
    assert codes(result) == ["latex_no_document"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("\\begin{tikzpicture}\\node{Python};\\end{tikzpicture}", "latex_content_skipped"),
        ("\\includegraphics[width=2cm]{logo}", "latex_image_skipped"),
        ("\\input{a}\\input{a}", "latex_include_repeated"),
    ],
)
def test_skipped_content_is_reported(text: str, code: str) -> None:
    result = from_text(
        f"\\documentclass{{article}}\\begin{{document}}{text}\\end{{document}}",
        **{"a.tex": "x"},
    )
    assert codes(result) == [code]
    assert "Python" not in " ".join(line.text for line in result.extracted.lines)


def test_issue_and_line_reprs_never_show_cv_text() -> None:
    result = extract_latex(project(FIXTURES / "moderncv.tex"))
    rendered = repr(result) + repr(project(FIXTURES / "moderncv.tex"))
    assert "Avery" not in rendered
    assert "example.test" not in rendered
