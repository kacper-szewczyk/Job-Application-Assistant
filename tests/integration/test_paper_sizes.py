"""Every shipped CV and cover letter template on A4 and US Letter (#92, ADR 0033).

Real PDF builds, skipped without a LaTeX engine; CI requires one (JAA_REQUIRE_LATEX=1).
"""

from pathlib import Path

import pytest
from pypdf import PdfReader

from jaa.core.models import PaperSize, TemplateKind
from jaa.documents.content import CvLanguage
from jaa.documents.contract import PAPER_OPTIONS, PAPER_POINTS
from jaa.documents.engine import Engine
from jaa.documents.latex import available_templates
from jaa.documents.pipeline import BuildOptions, BuildOutput, build_cover_letter, build_cv
from jaa.documents.sample import sample_cv, sample_letter

pytestmark = pytest.mark.latex

CASES = [
    (template_id, paper) for template_id in sorted(available_templates()) for paper in PaperSize
]


def _assert_paper(out: BuildOutput, paper: PaperSize) -> None:
    want = PAPER_POINTS[paper]
    for page in PdfReader(out.pdf).pages:
        size = tuple(float(v) for v in page.mediabox.upper_right)
        assert size == pytest.approx(want, abs=1), size
    assert out.manifest["paper"] == paper.value
    assert out.missing_text == ()
    assert PAPER_OPTIONS[paper] in out.tex.read_text()


@pytest.mark.parametrize(("template_id", "paper"), CASES)
def test_shipped_templates_build_on_both_papers(
    latex_engine: Engine, tmp_path: Path, template_id: str, paper: PaperSize
) -> None:
    template = available_templates()[template_id]
    if template.kind is TemplateKind.CV:
        out = build_cv(
            sample_cv(CvLanguage.EN),
            template=template,
            engine=latex_engine,
            output_dir=tmp_path,
            options=BuildOptions(paper=paper),
            profile_version_id=None,
        )
    else:
        out = build_cover_letter(
            sample_letter(CvLanguage.EN),
            template=template,
            engine=latex_engine,
            output_dir=tmp_path,
            language=CvLanguage.EN,
            profile_version_id=None,
            job_id=None,
            fact_ids=(),
            tailoring=None,
            paper=paper,
        )
    _assert_paper(out, paper)
