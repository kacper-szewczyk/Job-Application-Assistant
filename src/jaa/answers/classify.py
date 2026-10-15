"""Classify application questions and decide which answers may be used (SPEC §6, ADR 0017).

Pure. Every question gets one class:

- `profile_backed`: a confirmed profile fact answers it (email, phone, links, name, location);
- `reusable_answer`: the user saved an answer for it earlier;
- `inferred_suggestion`: something suggests an answer (a preference, an unreviewed fact, an
  answer to a differently worded sensitive question), but the user has to confirm it;
- `unanswered`: nothing suggests an answer.

and one action: `fill` (use the value), `review` (use it and show it to the user) or `ask`
(the user must type or confirm a value). Explicit topics (work authorization, sponsorship,
salary, demographics, criminal/medical, consent, legal) are always `ask`, unless the user saved
an answer to the same question with the `reuse` policy, which is how a safe reusable answer is
configured. Inferences and expired or unconfirmed answers are always `ask`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from jaa.answers.topics import Topic, find_topic, is_sensitive_question_key, question_key
from jaa.core.dedupe import fold
from jaa.core.models import Answer, JsonValue, ProfileFact, ReusePolicy
from jaa.core.preferences import format_value
from jaa.core.preferences import question as preference_question


class AnswerClass(StrEnum):
    PROFILE_BACKED = "profile_backed"
    REUSABLE_ANSWER = "reusable_answer"
    INFERRED_SUGGESTION = "inferred_suggestion"
    UNANSWERED = "unanswered"


class AnswerAction(StrEnum):
    FILL = "fill"
    REVIEW = "review"
    ASK = "ask"


class Confidence(StrEnum):
    # A confirmed fact, or a saved answer to the same wording.
    HIGH = "high"
    # A saved answer to a differently worded question on the same topic, or a preference.
    MEDIUM = "medium"
    # Unreviewed facts.
    LOW = "low"


class Note(StrEnum):
    """Why a question got its action. Codes only; the CLI words them."""

    SENSITIVE = "sensitive"
    SAFE_REUSE = "safe_reuse"
    EXPIRED = "expired"
    UNCONFIRMED = "unconfirmed"
    ALWAYS_ASK = "always_ask"
    REVIEW_POLICY = "review_policy"
    DIFFERENT_WORDING = "different_wording"
    UNREVIEWED_FACT = "unreviewed_fact"
    FROM_PREFERENCE = "from_preference"
    NOT_AN_OPTION = "not_an_option"
    UNKNOWN_QUESTION = "unknown_question"


@dataclass(frozen=True, slots=True, kw_only=True)
class QuestionInput:
    """A question as the employer asks it."""

    text: str
    required: bool = False
    options: tuple[str, ...] = ()
    # Where the question came from: "listing" or "user" (copied by the user), plus its id.
    origin: str = "user"
    origin_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Where a suggested or used value comes from."""

    answer_id: int | None = None
    fact_ids: tuple[int, ...] = ()
    preference_key: str | None = None

    def is_empty(self) -> bool:
        return self.answer_id is None and not self.fact_ids and self.preference_key is None


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolution:
    question: QuestionInput
    question_key: str
    topic: Topic | None
    sensitive: bool
    explicit: bool
    answer_class: AnswerClass
    action: AnswerAction
    confidence: Confidence | None = None
    value: str | None = field(default=None, repr=False)
    provenance: Provenance = field(default_factory=Provenance)
    notes: tuple[Note, ...] = ()
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerContext:
    """What answers can come from.

    `answers` holds the current answer per question key. `facts` maps field keys to the
    confirmed fact for the key, else the newest unreviewed one. `preferences` holds answered
    preference values. `default_policy` is the user's reuse policy for ordinary answers
    (ADR 0007), applied to profile facts.
    """

    answers: Mapping[str, Answer]
    facts: Mapping[str, ProfileFact]
    preferences: Mapping[str, JsonValue]
    default_policy: ReusePolicy
    now: datetime


def is_expired(answer: Answer, now: datetime) -> bool:
    return answer.expires_at is not None and answer.expires_at <= now


def _action_for(policy: ReusePolicy) -> AnswerAction:
    match policy:
        case ReusePolicy.REUSE:
            return AnswerAction.FILL
        case ReusePolicy.REUSE_WITH_REVIEW:
            return AnswerAction.REVIEW
        case ReusePolicy.ALWAYS_ASK:
            return AnswerAction.ASK


def _fits_options(value: str, options: Sequence[str]) -> bool:
    return not options or fold(value) in {fold(o) for o in options}


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else str(value)


def _from_saved(
    q: QuestionInput, key: str, topic: Topic | None, saved: Answer, ctx: AnswerContext
) -> Resolution:
    explicit = topic is not None and topic.explicit
    same_wording = fold(saved.question_text) == fold(q.text)
    notes: list[Note] = []
    if not same_wording:
        notes.append(Note.DIFFERENT_WORDING)
    if saved.confirmed_at is None:
        notes.append(Note.UNCONFIRMED)
    if is_expired(saved, ctx.now):
        notes.append(Note.EXPIRED)
    value = _text(saved.value)
    if not _fits_options(value, q.options):
        notes.append(Note.NOT_AN_OPTION)
    action = _action_for(saved.reuse_policy)
    if saved.reuse_policy is ReusePolicy.ALWAYS_ASK:
        notes.append(Note.ALWAYS_ASK)
    elif saved.reuse_policy is ReusePolicy.REUSE_WITH_REVIEW:
        notes.append(Note.REVIEW_POLICY)
    if explicit:
        if saved.reuse_policy is ReusePolicy.REUSE and same_wording:
            notes.append(Note.SAFE_REUSE)
        else:
            action = AnswerAction.ASK
    elif not same_wording and action is AnswerAction.FILL:
        action = AnswerAction.REVIEW
    if {Note.UNCONFIRMED, Note.EXPIRED, Note.NOT_AN_OPTION} & set(notes):
        action = AnswerAction.ASK
    return Resolution(
        question=q,
        question_key=key,
        topic=topic,
        sensitive=saved.is_sensitive or is_sensitive_question_key(key),
        explicit=explicit,
        # An explicit answer to another wording only suggests: the facts may differ.
        answer_class=(
            AnswerClass.INFERRED_SUGGESTION
            if explicit and not same_wording
            else AnswerClass.REUSABLE_ANSWER
        ),
        action=action,
        confidence=Confidence.HIGH if same_wording else Confidence.MEDIUM,
        value=value,
        provenance=Provenance(answer_id=saved.id),
        notes=tuple(notes),
        expires_at=saved.expires_at,
    )


def _saved_for(key: str, topic: Topic | None, ctx: AnswerContext) -> Answer | None:
    """The answer saved under this key; for explicit topics, else the newest on the topic."""
    found = ctx.answers.get(key)
    if found is not None or topic is None or not topic.explicit:
        return found
    related = [
        a
        for k, a in ctx.answers.items()
        if k.startswith(topic.key + ".") and a.confirmed_at is not None
    ]
    return max(related, key=lambda a: (a.created_at, a.id or 0), default=None)


def _from_fact(
    q: QuestionInput, key: str, topic: Topic, fact: ProfileFact, ctx: AnswerContext
) -> Resolution:
    confirmed = fact.confirmed_at is not None and fact.rejected_at is None
    notes: list[Note] = [] if confirmed else [Note.UNREVIEWED_FACT]
    if not _fits_options(fact.value_raw, q.options):
        notes.append(Note.NOT_AN_OPTION)
    action = _action_for(ctx.default_policy) if confirmed else AnswerAction.ASK
    if Note.NOT_AN_OPTION in notes:
        action = AnswerAction.ASK
    return Resolution(
        question=q,
        question_key=key,
        topic=topic,
        sensitive=fact.is_sensitive or is_sensitive_question_key(key),
        explicit=False,
        answer_class=(AnswerClass.PROFILE_BACKED if confirmed else AnswerClass.INFERRED_SUGGESTION),
        action=action,
        confidence=Confidence.HIGH if confirmed else Confidence.LOW,
        value=fact.value_raw,
        provenance=Provenance(fact_ids=(fact.id,) if fact.id is not None else ()),
        notes=tuple(notes),
    )


def _preference_value(topic: Topic, ctx: AnswerContext) -> str | None:
    if topic.preference_key is None or topic.preference_key not in ctx.preferences:
        return None
    value = ctx.preferences[topic.preference_key]
    # "Depends on the country" asks per application by definition (ADR 0007).
    if topic.preference_key == "sponsorship.required" and value == "depends":
        return None
    return format_value(preference_question(topic.preference_key), value)


def classify(q: QuestionInput, ctx: AnswerContext) -> Resolution:
    """Classify one question and decide whether a value may be used for it."""
    topic = find_topic(q.text)
    key = question_key(q.text)
    explicit = topic is not None and topic.explicit
    sensitive = is_sensitive_question_key(key)
    saved = _saved_for(key, topic, ctx)
    if saved is not None:
        resolution = _from_saved(q, key, topic, saved, ctx)
    elif topic is not None and (
        fact := next((ctx.facts[k] for k in topic.fact_keys if k in ctx.facts), None)
    ):
        resolution = _from_fact(q, key, topic, fact, ctx)
    elif topic is not None and (suggested := _preference_value(topic, ctx)) is not None:
        resolution = Resolution(
            question=q,
            question_key=key,
            topic=topic,
            sensitive=sensitive,
            explicit=explicit,
            answer_class=AnswerClass.INFERRED_SUGGESTION,
            action=AnswerAction.ASK,
            confidence=Confidence.MEDIUM,
            value=suggested,
            provenance=Provenance(preference_key=topic.preference_key),
            notes=(Note.FROM_PREFERENCE,),
        )
    else:
        resolution = Resolution(
            question=q,
            question_key=key,
            topic=topic,
            sensitive=sensitive,
            explicit=explicit,
            answer_class=AnswerClass.UNANSWERED,
            action=AnswerAction.ASK,
            notes=() if topic is not None else (Note.UNKNOWN_QUESTION,),
        )
    if explicit and Note.SENSITIVE not in resolution.notes:
        resolution = _with_note(resolution, Note.SENSITIVE)
    return resolution


def _with_note(resolution: Resolution, note: Note) -> Resolution:
    return replace(resolution, notes=(note, *resolution.notes))


def classify_all(questions: Sequence[QuestionInput], ctx: AnswerContext) -> list[Resolution]:
    return [classify(q, ctx) for q in questions]
