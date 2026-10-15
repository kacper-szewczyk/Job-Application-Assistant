"""Tailoring a CV for one job: checked edits on top of the baseline CV (T13, ADR 0016). Pure.

A tailoring plan is a list of edits to the CV that `build_content` makes from the confirmed
profile (the baseline). Edits can only:

* hide an entry, a bullet, a skill or a certification (select);
* move a bullet within its entry, or a skill within the skills (reorder);
* set a bullet or a skill in bold (emphasize);
* reword the headline, the summary or a bullet (rewrite).

Names, employers, titles, institutions, degrees and dates cannot be targeted at all, so their
meaning cannot change. Every edit is linked to the confirmed facts behind it: the target's own
fact, plus the facts it cites. A rewrite must cite facts that back its wording: numbers, names
and technologies in the new text, and words from the job's listed skills, must appear in the
target's text or in a cited fact. Edits that break these rules are blocked unless the user
approved them (`approvals`); edits on unknown targets can never be approved.

A plan may also hold cover letter paragraphs, checked by the same rules (each needs cited
facts, and its wording must be backed by them).

The checks compare wording, like matching (ADR 0015): they catch new numbers, names and
keywords, not every change of meaning, which is why the user reviews every change.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, cast

from jaa.core.errors import TailoringError
from jaa.core.models import FactState, JsonObject, JsonValue, ProfileFact
from jaa.documents.content import CvContent, CvEntry, build_content, text_of
from jaa.matching.match import tokens

# Stored with each plan; bump when the rules change.
METHOD: Final = "tailoring-v1"
MAX_EDITS: Final = 200
MAX_TEXT: Final = 2000


class EditKind(StrEnum):
    HIDE = "hide"
    MOVE = "move"
    EMPHASIZE = "emphasize"
    REWRITE = "rewrite"


class TargetType(StrEnum):
    HEADLINE = "headline"
    SUMMARY = "summary"
    ENTRY = "entry"
    BULLET = "bullet"
    SKILL = "skill"
    CERTIFICATION = "certification"


ALLOWED: Final[dict[TargetType, frozenset[EditKind]]] = {
    TargetType.HEADLINE: frozenset({EditKind.REWRITE}),
    TargetType.SUMMARY: frozenset({EditKind.REWRITE, EditKind.HIDE}),
    TargetType.ENTRY: frozenset({EditKind.HIDE}),
    TargetType.BULLET: frozenset(EditKind),
    TargetType.SKILL: frozenset({EditKind.HIDE, EditKind.MOVE, EditKind.EMPHASIZE}),
    TargetType.CERTIFICATION: frozenset({EditKind.HIDE}),
}


# The fact key of an experience entry's bullets.
_BULLET_ENTRY: Final = re.compile(r"experience\.([^.\[\]]+)\.description")


@dataclass(frozen=True, slots=True, kw_only=True)
class Edit:
    kind: EditKind
    # An address: `headline`, `summary`, `experience.<id>`, `education.<id>`,
    # `experience.<id>.description[<n>]` (n counts the baseline bullets from 1),
    # `skills.<key>` or `certifications.<key>`.
    target: str
    # MOVE: the new position, counted from 1, among what is left after earlier edits.
    position: int | None = None
    # REWRITE: the new wording. CV content: never log it.
    text: str | None = field(default=None, repr=False)
    # Confirmed facts that back the edit, beyond the target's own fact.
    fact_ids: tuple[int, ...] = ()
    # Why the edit helps for this job, e.g. the requirement it answers.
    reason: str | None = field(default=None, repr=False)

    def to_json(self) -> JsonObject:
        data: JsonObject = {"kind": self.kind.value, "target": self.target}
        if self.position is not None:
            data["position"] = self.position
        if self.text is not None:
            data["text"] = self.text
        data["fact_ids"] = list[JsonValue](self.fact_ids)
        if self.reason is not None:
            data["reason"] = self.reason
        return data

    def fingerprint(self) -> str:
        """Identifies the edit's content, so an approval does not carry over to a changed edit."""
        return _fingerprint("edit", self.to_json())


@dataclass(frozen=True, slots=True, kw_only=True)
class Paragraph:
    """A cover letter paragraph and the confirmed facts that back it."""

    text: str = field(repr=False)
    fact_ids: tuple[int, ...] = ()

    def to_json(self) -> JsonObject:
        return {"text": self.text, "fact_ids": list[JsonValue](self.fact_ids)}

    def fingerprint(self) -> str:
        return _fingerprint("paragraph", self.to_json())


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    edits: tuple[Edit, ...] = ()
    cover_letter: tuple[Paragraph, ...] = field(default=(), repr=False)

    def to_json(self) -> JsonObject:
        return {
            "method": METHOD,
            "edits": [e.to_json() for e in self.edits],
            "cover_letter": [p.to_json() for p in self.cover_letter],
        }


def _fingerprint(kind: str, data: JsonObject) -> str:
    payload = json.dumps([kind, data], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# Reading a plan from JSON (a file the user or Claude edited) ------------------------------


def _ids(value: JsonValue, where: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        raise TailoringError(f"{where}: fact_ids must be a list of fact ids")
    return tuple(cast(list[int], value))


def _text(value: JsonValue, where: str, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TailoringError(f"{where}: {name} must be text")
    if len(value) > MAX_TEXT:
        raise TailoringError(f"{where}: {name} is longer than {MAX_TEXT} characters")
    return value


def _keys(data: Mapping[str, JsonValue], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise TailoringError(f"{where}: unknown field(s) {', '.join(unknown)}")


def plan_from_json(data: JsonValue) -> Plan:
    """Read a plan written by `Plan.to_json`, as edited by the user. Raises `TailoringError`."""
    if not isinstance(data, dict):
        raise TailoringError("a tailoring plan must be a JSON object")
    _keys(data, {"method", "edits", "cover_letter"}, "plan")
    raw_edits = data.get("edits") or []
    raw_paragraphs = data.get("cover_letter") or []
    if not isinstance(raw_edits, list) or not isinstance(raw_paragraphs, list):
        raise TailoringError("plan: edits and cover_letter must be lists")
    if len(raw_edits) > MAX_EDITS:
        raise TailoringError(f"plan: more than {MAX_EDITS} edits")
    edits: list[Edit] = []
    for n, raw in enumerate(raw_edits, start=1):
        where = f"edit {n}"
        if not isinstance(raw, dict):
            raise TailoringError(f"{where}: must be an object")
        _keys(raw, {"kind", "target", "position", "text", "fact_ids", "reason"}, where)
        try:
            kind = EditKind(cast(str, raw.get("kind")))
        except ValueError as exc:
            kinds = ", ".join(k.value for k in EditKind)
            raise TailoringError(f"{where}: kind must be one of {kinds}") from exc
        target = raw.get("target")
        if not isinstance(target, str) or not target:
            raise TailoringError(f"{where}: target must be text")
        position = raw.get("position")
        if position is not None and (
            not isinstance(position, int) or isinstance(position, bool) or position < 1
        ):
            raise TailoringError(f"{where}: position must be a whole number from 1")
        edits.append(
            Edit(
                kind=kind,
                target=target,
                position=position,
                text=_text(raw.get("text"), where, "text"),
                fact_ids=_ids(raw.get("fact_ids"), where),
                reason=_text(raw.get("reason"), where, "reason"),
            )
        )
    paragraphs: list[Paragraph] = []
    for n, raw in enumerate(raw_paragraphs, start=1):
        where = f"cover letter paragraph {n}"
        if not isinstance(raw, dict):
            raise TailoringError(f"{where}: must be an object")
        _keys(raw, {"text", "fact_ids"}, where)
        text = _text(raw.get("text"), where, "text")
        if not text or not text.strip():
            raise TailoringError(f"{where}: text is empty")
        paragraphs.append(Paragraph(text=text, fact_ids=_ids(raw.get("fact_ids"), where)))
    return Plan(edits=tuple(edits), cover_letter=tuple(paragraphs))


# Checking ---------------------------------------------------------------------------------


class IssueCode(StrEnum):
    UNKNOWN_TARGET = "unknown_target"
    NOT_ALLOWED = "not_allowed"
    MISSING_POSITION = "missing_position"
    EMPTY_TEXT = "empty_text"
    UNKNOWN_FACT = "unknown_fact"
    # Cited facts that are not confirmed in the profile version (unreviewed).
    UNREVIEWED_FACT = "unreviewed_fact"
    NO_EVIDENCE = "no_evidence"
    UNSUPPORTED_TERMS = "unsupported_terms"


# Issues the user may approve explicitly. The others make the edit meaningless or unsafe.
APPROVABLE: Final = frozenset(
    {IssueCode.UNREVIEWED_FACT, IssueCode.NO_EVIDENCE, IssueCode.UNSUPPORTED_TERMS}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Issue:
    code: IssueCode
    # May quote the edit's words: shown to the user, never logged.
    message: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """The verdict on one edit or cover letter paragraph."""

    number: int  # counted from 1, as shown to the user
    fingerprint: str
    issues: tuple[Issue, ...] = ()
    approved: bool = False
    # The facts behind it: the target's own facts and the cited ones that exist.
    fact_ids: tuple[int, ...] = ()

    @property
    def approvable(self) -> bool:
        return bool(self.issues) and all(i.code in APPROVABLE for i in self.issues)

    @property
    def blocked(self) -> bool:
        return bool(self.issues) and not (self.approved and self.approvable)

    def to_json(self) -> JsonObject:
        # Codes only: the messages quote CV text.
        return {
            "number": self.number,
            "codes": [i.code.value for i in self.issues],
            "approved": self.approved,
            "blocked": self.blocked,
            "fact_ids": list[JsonValue](self.fact_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """One applied edit as the user sees it: what was there and what is there now."""

    number: int
    kind: EditKind
    target: str
    before: str | None = field(default=None, repr=False)
    after: str | None = field(default=None, repr=False)
    fact_ids: tuple[int, ...] = ()
    reason: str | None = field(default=None, repr=False)

    def to_json(self) -> JsonObject:
        return {
            "number": self.number,
            "kind": self.kind.value,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "fact_ids": list[JsonValue](self.fact_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoringResult:
    baseline: CvContent
    content: CvContent
    edit_checks: tuple[Check, ...]
    paragraph_checks: tuple[Check, ...]
    # Applied edits in order; blocked edits are not applied.
    changes: tuple[Change, ...]

    @property
    def blocked(self) -> bool:
        return any(c.blocked for c in (*self.edit_checks, *self.paragraph_checks))


@dataclass(frozen=True, slots=True, kw_only=True)
class _Target:
    type: TargetType
    fact_ids: tuple[int, ...]
    # The text the target shows on the baseline CV (for rewrites and the change list).
    text: str | None = field(default=None, repr=False)
    # Bullets: the entry id and the baseline index.
    entry_id: str | None = None
    index: int | None = None


def _targets(content: CvContent, facts: Sequence[ProfileFact]) -> dict[str, _Target]:
    """Every address an edit may use, with the facts behind it."""
    by_key = {f.field_key: f for f in facts if f.id is not None}

    def ids(*keys: str) -> tuple[int, ...]:
        return tuple(cast(int, by_key[k].id) for k in keys if k in by_key)

    found: dict[str, _Target] = {}
    if content.headline:
        found["headline"] = _Target(
            type=TargetType.HEADLINE, fact_ids=ids("headline"), text=content.headline
        )
    if content.summary:
        found["summary"] = _Target(
            type=TargetType.SUMMARY, fact_ids=ids("summary"), text=content.summary
        )
    for section, entries in (("experience", content.experience), ("education", content.education)):
        for entry in entries:
            prefix = f"{section}.{entry.entry_id}"
            entry_facts = tuple(
                cast(int, f.id) for k, f in by_key.items() if k.startswith(prefix + ".")
            )
            label = ", ".join(p for p in (entry.role, entry.organization) if p)
            found[prefix] = _Target(type=TargetType.ENTRY, fact_ids=entry_facts, text=label)
            if section != "experience":
                continue
            for i, item in enumerate(entry.items):
                found[f"{prefix}.description[{i + 1}]"] = _Target(
                    type=TargetType.BULLET,
                    fact_ids=ids(f"{prefix}.description"),
                    text=item,
                    entry_id=entry.entry_id,
                    index=i,
                )
    for key, skill in zip(content.skill_keys, content.skills, strict=True):
        found[key] = _Target(type=TargetType.SKILL, fact_ids=ids(key), text=skill)
    for key, cert in zip(content.certification_keys, content.certifications, strict=True):
        found[key] = _Target(type=TargetType.CERTIFICATION, fact_ids=ids(key), text=cert.name)
    return found


def _strings(value: JsonValue) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)


def fact_text(fact: ProfileFact) -> str:
    """All the words a fact holds, for checking what a rewrite may say."""
    return " ".join([fact.value_raw, *_strings(fact.value_normalized)])


# Words as written, to tell names and acronyms from ordinary words.
_WORD: Final = re.compile(r"[^\W_][\w+#]*(?:[.\-][\w+#]+)*")
_SENTENCE_END: Final = re.compile(r"[.!?:;]\s*$")


def claims(text: str, job_terms: frozenset[str]) -> list[str]:
    """Words in `text` that state something checkable.

    Numbers, words with capitals (other than a sentence's first word), words with `+` or `#`,
    and words the job lists as skills. Ordinary lowercase words are rewording and pass.
    """
    text = unicodedata.normalize("NFC", text)
    found: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        start = match.start()
        sentence_start = start == 0 or bool(_SENTENCE_END.search(text[:start]))
        rest = word[1:] if sentence_start else word
        notable = (
            any(c.isdigit() for c in word)
            or any(c.isupper() for c in rest)
            or "+" in word
            or "#" in word
            or any(t in job_terms for t in tokens(word))
        )
        if notable and word not in found:
            found.append(word)
    return found


def unsupported(text: str, evidence: Iterable[str], job_terms: frozenset[str]) -> list[str]:
    """The claims in `text` that no evidence text contains."""
    known = {t for e in evidence for t in tokens(e)}
    return [w for w in claims(text, job_terms) if not set(tokens(w)) <= known]


def _evidence(
    cited: Sequence[int],
    confirmed: Mapping[int, ProfileFact],
    unreviewed: Mapping[int, ProfileFact],
) -> tuple[list[ProfileFact], list[Issue]]:
    facts: list[ProfileFact] = []
    issues: list[Issue] = []
    unknown = [i for i in cited if i not in confirmed and i not in unreviewed]
    pending = [i for i in cited if i not in confirmed and i in unreviewed]
    if unknown:
        listed = ", ".join(f"#{i}" for i in unknown)
        issues.append(
            Issue(
                code=IssueCode.UNKNOWN_FACT,
                message=f"{listed} is not a confirmed or unreviewed fact of this profile version",
            )
        )
    if pending:
        listed = ", ".join(f"#{i}" for i in pending)
        issues.append(
            Issue(
                code=IssueCode.UNREVIEWED_FACT,
                message=f"{listed} is not reviewed yet; confirm it with `jaa profile`",
            )
        )
    facts = [confirmed.get(i) or unreviewed[i] for i in cited if i not in unknown]
    return facts, issues


def _wording_issues(
    text: str,
    evidence: Sequence[str],
    job_terms: frozenset[str],
    *,
    cited: bool,
    where: str = "the target or the cited facts",
) -> list[Issue]:
    missing = unsupported(text, evidence, job_terms)
    issues: list[Issue] = []
    if missing:
        issues.append(
            Issue(
                code=IssueCode.UNSUPPORTED_TERMS,
                message=f"not found in {where}: " + ", ".join(missing),
            )
        )
    if not cited:
        issues.append(
            Issue(code=IssueCode.NO_EVIDENCE, message="cites no fact that backs the wording")
        )
    return issues


def _check_edit(
    number: int,
    edit: Edit,
    targets: Mapping[str, _Target],
    hidden: set[str],
    confirmed: Mapping[int, ProfileFact],
    unreviewed: Mapping[int, ProfileFact],
    job_terms: frozenset[str],
    approvals: frozenset[str],
) -> Check:
    fingerprint = edit.fingerprint()
    target = targets.get(edit.target)
    issues: list[Issue] = []
    if target is None:
        issues.append(
            Issue(
                code=IssueCode.UNKNOWN_TARGET,
                message=f"{edit.target!r} is not on the baseline CV; see `jaa cv tailor show`",
            )
        )
        return Check(number=number, fingerprint=fingerprint, issues=tuple(issues))
    entry_hidden = target.entry_id is not None and f"experience.{target.entry_id}" in hidden
    if edit.kind not in ALLOWED[target.type]:
        issues.append(
            Issue(
                code=IssueCode.NOT_ALLOWED,
                message=f"a {target.type.value} cannot be given a {edit.kind.value} edit",
            )
        )
    elif edit.target in hidden or entry_hidden:
        issues.append(Issue(code=IssueCode.NOT_ALLOWED, message="an earlier edit hides it"))
    if edit.kind is EditKind.MOVE and edit.position is None:
        issues.append(Issue(code=IssueCode.MISSING_POSITION, message="a move needs a position"))
    cited, fact_issues = _evidence(edit.fact_ids, confirmed, unreviewed)
    issues += fact_issues
    if edit.kind is EditKind.REWRITE:
        if not edit.text or not edit.text.strip():
            issues.append(Issue(code=IssueCode.EMPTY_TEXT, message="a rewrite needs new text"))
        else:
            evidence = [target.text or "", *(fact_text(f) for f in cited)]
            issues += _wording_issues(
                edit.text, evidence, job_terms, cited=bool(cited or target.fact_ids)
            )
    fact_ids = tuple(dict.fromkeys((*target.fact_ids, *(cast(int, f.id) for f in cited))))
    check = Check(
        number=number,
        fingerprint=fingerprint,
        issues=tuple(issues),
        approved=fingerprint in approvals,
        fact_ids=fact_ids,
    )
    if edit.kind is EditKind.HIDE and not check.blocked:
        hidden.add(edit.target)
    return check


def _check_paragraph(
    number: int,
    paragraph: Paragraph,
    confirmed: Mapping[int, ProfileFact],
    unreviewed: Mapping[int, ProfileFact],
    job_terms: frozenset[str],
    approvals: frozenset[str],
) -> Check:
    cited, issues = _evidence(paragraph.fact_ids, confirmed, unreviewed)
    evidence = [fact_text(f) for f in cited]
    issues += _wording_issues(
        paragraph.text, evidence, job_terms, cited=bool(cited), where="the cited facts"
    )
    fingerprint = paragraph.fingerprint()
    return Check(
        number=number,
        fingerprint=fingerprint,
        issues=tuple(issues),
        approved=fingerprint in approvals,
        fact_ids=tuple(cast(int, f.id) for f in cited),
    )


# Applying ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Item:
    key: str
    text: str
    emphasized: bool = False


def _move[T](items: list[T], index: int, position: int) -> int:
    """Move `items[index]` to `position` (from 1, clamped to the end). Returns the new index."""
    item = items.pop(index)
    new = min(position, len(items) + 1) - 1
    items.insert(new, item)
    return new


def _apply(
    baseline: CvContent,
    plan: Plan,
    checks: Sequence[Check],
    targets: Mapping[str, _Target],
) -> tuple[CvContent, list[Change]]:
    headline, summary = baseline.headline, baseline.summary
    experience: dict[str, list[_Item]] = {
        e.entry_id: [
            _Item(key=f"experience.{e.entry_id}.description[{i + 1}]", text=t)
            for i, t in enumerate(e.items)
        ]
        for e in baseline.experience
    }
    skills = [
        _Item(key=k, text=t) for k, t in zip(baseline.skill_keys, baseline.skills, strict=True)
    ]
    certifications = list(zip(baseline.certification_keys, baseline.certifications, strict=True))
    hidden: set[str] = set()
    changes: list[Change] = []

    def bullets_of(target: _Target) -> list[_Item]:
        return experience[cast(str, target.entry_id)]

    for edit, check in zip(plan.edits, checks, strict=True):
        if check.blocked:
            continue
        target = targets[edit.target]
        before: str | None = target.text
        after: str | None = None
        if target.type in (TargetType.BULLET, TargetType.SKILL):
            items = bullets_of(target) if target.type is TargetType.BULLET else skills
            index = next(i for i, x in enumerate(items) if x.key == edit.target)
            item = items[index]
            before = item.text
            if edit.kind is EditKind.HIDE:
                items.pop(index)
            elif edit.kind is EditKind.MOVE:
                new = _move(items, index, cast(int, edit.position))
                before, after = f"position {index + 1}", f"position {new + 1}"
            elif edit.kind is EditKind.EMPHASIZE:
                item.emphasized = True
                after = item.text
            else:
                item.text = after = cast(str, edit.text).strip()
        elif edit.kind is EditKind.REWRITE:
            after = cast(str, edit.text).strip()
            if target.type is TargetType.HEADLINE:
                before, headline = headline, after
            else:
                before, summary = summary, after
        else:  # HIDE of the summary, an entry or a certification
            hidden.add(edit.target)
            if target.type is TargetType.SUMMARY:
                summary = None
            certifications = [c for c in certifications if c[0] != edit.target]
        changes.append(
            Change(
                number=check.number,
                kind=edit.kind,
                target=edit.target,
                before=before,
                after=after,
                fact_ids=check.fact_ids,
                reason=edit.reason,
            )
        )

    def entries(section: str, found: tuple[CvEntry, ...]) -> tuple[CvEntry, ...]:
        kept: list[CvEntry] = []
        for entry in found:
            if f"{section}.{entry.entry_id}" in hidden:
                continue
            if section == "experience":
                items = experience[entry.entry_id]
                entry = replace(
                    entry,
                    items=tuple(x.text for x in items),
                    emphasized=tuple(i for i, x in enumerate(items) if x.emphasized),
                )
            kept.append(entry)
        return tuple(kept)

    content = replace(
        baseline,
        headline=headline,
        summary=summary,
        experience=entries("experience", baseline.experience),
        education=entries("education", baseline.education),
        skills=tuple(x.text for x in skills),
        skill_keys=tuple(x.key for x in skills),
        emphasized_skills=tuple(i for i, x in enumerate(skills) if x.emphasized),
        certifications=tuple(c for _, c in certifications),
        certification_keys=tuple(k for k, _ in certifications),
    )
    return content, changes


def _used_fact_ids(
    baseline: CvContent,
    content: CvContent,
    facts: Sequence[ProfileFact],
    changes: Sequence[Change],
) -> tuple[int, ...]:
    """Facts the tailored CV still shows, plus the facts cited by the applied edits."""
    ids = set(baseline.fact_ids)
    by_id = {f.id: f for f in facts}
    shown_entries = {f"experience.{e.entry_id}" for e in content.experience} | {
        f"education.{e.entry_id}" for e in content.education
    }
    for fact_id in list(ids):
        key = by_id[fact_id].field_key
        parts = key.split(".")
        if parts[0] in ("experience", "education") and len(parts) == 3:
            if f"{parts[0]}.{parts[1]}" not in shown_entries:
                ids.discard(fact_id)
        elif (
            (parts[0] == "skills" and key not in content.skill_keys)
            or (parts[0] == "certifications" and key not in content.certification_keys)
            or (key == "summary" and content.summary is None)
        ):
            ids.discard(fact_id)
    for change in changes:
        if change.kind is EditKind.REWRITE:
            ids.update(change.fact_ids)
    return tuple(sorted(ids))


def tailor(
    plan: Plan,
    facts: Sequence[ProfileFact],
    *,
    unreviewed: Sequence[ProfileFact] = (),
    job_terms: frozenset[str] = frozenset(),
    approvals: frozenset[str] = frozenset(),
) -> TailoringResult:
    """Check `plan` against the confirmed `facts` of one profile version and apply what passes.

    `unreviewed` facts may be cited, but only with the user's approval. `job_terms` are the
    folded words of the job's listed skills: a rewrite that adds one needs a fact behind it.
    `approvals` are fingerprints of edits and paragraphs the user approved despite issues.
    """
    confirmed = {f.id: f for f in facts if f.state is FactState.CONFIRMED and f.id is not None}
    pending = {
        f.id: f
        for f in unreviewed
        if f.state is FactState.UNREVIEWED and f.id is not None and f.id not in confirmed
    }
    baseline = build_content(list(confirmed.values())).content
    targets = _targets(baseline, list(confirmed.values()))
    hidden: set[str] = set()
    edit_checks = tuple(
        _check_edit(n, e, targets, hidden, confirmed, pending, job_terms, approvals)
        for n, e in enumerate(plan.edits, start=1)
    )
    paragraph_checks = tuple(
        _check_paragraph(n, p, confirmed, pending, job_terms, approvals)
        for n, p in enumerate(plan.cover_letter, start=1)
    )
    content, changes = _apply(baseline, plan, edit_checks, targets)
    content = replace(
        content, fact_ids=_used_fact_ids(baseline, content, list(confirmed.values()), changes)
    )
    return TailoringResult(
        baseline=baseline,
        content=content,
        edit_checks=edit_checks,
        paragraph_checks=paragraph_checks,
        changes=tuple(changes),
    )


# Proposing --------------------------------------------------------------------------------


def _moves(order: list[str], key: str, position: int) -> bool:
    """Move `key` to `position` in `order`; False when it is already there (no edit needed)."""
    index = order.index(key)
    if index == position - 1:
        return False
    _move(order, index, position)
    return True


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def propose(facts: Sequence[ProfileFact], evidence: Sequence[JsonValue]) -> Plan:
    """A first plan from a stored match's evidence (`JobMatch.evidence`, ADR 0015).

    Only reorders and emphasizes: skills that meet the job's requirements move to the front
    and are set in bold, required ones first; bullets that mention a met requirement move to
    the top of their entry. Nothing is hidden or reworded; those are the user's choices.
    Only confirmed facts are used, so every proposed edit passes the checks.
    """
    confirmed = {f.id: f for f in facts if f.state is FactState.CONFIRMED and f.id is not None}
    baseline = build_content(list(confirmed.values())).content
    targets = _targets(baseline, list(confirmed.values()))
    met = [
        cast(JsonObject, e)
        for e in evidence
        if isinstance(e, dict) and e.get("type") == "requirement" and e.get("outcome") == "met"
    ]
    met.sort(key=lambda e: 0 if e.get("importance") == "required" else 1)
    skills: list[tuple[str, str]] = []
    bullets: dict[str, list[tuple[str, str]]] = {}
    for found in met:
        requirement = text_of(found.get("requirement")) or ""
        for item in _as_list(found.get("facts")):
            if not isinstance(item, dict) or item.get("state") != FactState.CONFIRMED.value:
                continue
            fact = confirmed.get(cast(int, item.get("fact_id")))
            if fact is None:
                continue
            key = fact.field_key
            if key in targets and targets[key].type is TargetType.SKILL:
                if all(k != key for k, _ in skills):
                    skills.append((key, requirement))
                continue
            entry = _BULLET_ENTRY.fullmatch(key)
            term = tokens(text_of(item.get("term")) or "")
            if entry is None or not term:
                continue
            for address, target in targets.items():
                if target.type is not TargetType.BULLET or target.entry_id != entry.group(1):
                    continue
                words = tokens(target.text or "")
                mentioned = any(tuple(words[i : i + len(term)]) == term for i in range(len(words)))
                found_bullets = bullets.setdefault(entry.group(1), [])
                if mentioned and all(a != address for a, _ in found_bullets):
                    found_bullets.append((address, requirement))
    edits: list[Edit] = []
    order = list(baseline.skill_keys)
    for position, (key, requirement) in enumerate(skills, start=1):
        reason = f"the job asks for {requirement}"
        if _moves(order, key, position):
            edits.append(Edit(kind=EditKind.MOVE, target=key, position=position, reason=reason))
        edits.append(Edit(kind=EditKind.EMPHASIZE, target=key, reason=reason))
    for entry_id, found_bullets in bullets.items():
        order = [a for a, t in targets.items() if t.entry_id == entry_id]
        for position, (address, requirement) in enumerate(found_bullets, start=1):
            if _moves(order, address, position):
                edits.append(
                    Edit(
                        kind=EditKind.MOVE,
                        target=address,
                        position=position,
                        reason=f"mentions {requirement}, which the job asks for",
                    )
                )
    return Plan(edits=tuple(edits))


def job_terms(requirement_texts: Iterable[str]) -> frozenset[str]:
    """The folded words of the job's listed skills and languages, for `tailor`."""
    return frozenset(t for text in requirement_texts for t in tokens(text))
