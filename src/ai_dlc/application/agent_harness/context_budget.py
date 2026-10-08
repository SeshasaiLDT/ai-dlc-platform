"""Deterministic, model-aware context selection and token budgeting.

Segments are inputs to prompt assembly; they never replace ``AgentContext``. Identity and
initiative come only from the trusted ``AgentContext`` passed to ``assemble``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import JsonValue, field_validator, model_validator

from .models import AgentContext, ContractModel, ExecutionError, FrozenJsonValue, _freeze_json
from .ports import TelemetryProvider

_SAFE_TEXT = re.compile(r"^[^\s\"<>\x00-\x1f]+$")
_TRUNCATION_MARKER = " [truncated]"
_MAX_PRIORITY = 1000


class ContextCategory(StrEnum):
    SYSTEM_INSTRUCTIONS = "system_instructions"
    TASK_INSTRUCTIONS = "task_instructions"
    INITIATIVE_CONTEXT = "initiative_context"
    RETRIEVED_EVIDENCE = "retrieved_evidence"
    CONVERSATION_HISTORY = "conversation_history"
    SUPPORTING_METADATA = "supporting_metadata"


class InstructionAuthority(StrEnum):
    """Derived from category only; no segment or policy field can raise it."""

    INSTRUCTION = "instruction"
    TRUSTED_CONTEXT = "trusted_context"
    UNTRUSTED_DATA = "untrusted_data"


class ContentFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


_INSTRUCTION_CATEGORIES = (ContextCategory.SYSTEM_INSTRUCTIONS, ContextCategory.TASK_INSTRUCTIONS)
_DEFAULT_ORDER = (
    ContextCategory.INITIATIVE_CONTEXT,
    ContextCategory.RETRIEVED_EVIDENCE,
    ContextCategory.CONVERSATION_HISTORY,
    ContextCategory.SUPPORTING_METADATA,
)
_AUTHORITY = {
    ContextCategory.SYSTEM_INSTRUCTIONS: InstructionAuthority.INSTRUCTION,
    ContextCategory.TASK_INSTRUCTIONS: InstructionAuthority.INSTRUCTION,
    ContextCategory.INITIATIVE_CONTEXT: InstructionAuthority.TRUSTED_CONTEXT,
    ContextCategory.RETRIEVED_EVIDENCE: InstructionAuthority.UNTRUSTED_DATA,
    ContextCategory.CONVERSATION_HISTORY: InstructionAuthority.UNTRUSTED_DATA,
    ContextCategory.SUPPORTING_METADATA: InstructionAuthority.UNTRUSTED_DATA,
}


def authority_of(category: ContextCategory) -> InstructionAuthority:
    return _AUTHORITY[category]


# --- token counting -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenCount:
    tokens: int
    exact: bool


class TokenCounter:
    """Injected, model-specific counter. Implementations must be monotonic in text length."""

    def count(self, text: str, *, model_id: str) -> TokenCount:  # pragma: no cover - interface
        raise NotImplementedError


class EstimatingTokenCounter(TokenCounter):
    """Conservative character-based estimate. Never reports ``exact=True``."""

    def __init__(self, chars_per_token: float = 3.0, safety_factor: float = 1.15) -> None:
        if not math.isfinite(chars_per_token) or chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        if not math.isfinite(safety_factor) or safety_factor < 1:
            raise ValueError("safety_factor must be at least 1")
        self._chars_per_token = chars_per_token
        self._safety_factor = safety_factor

    def count(self, text: str, *, model_id: str) -> TokenCount:
        return TokenCount(math.ceil(len(text) / self._chars_per_token * self._safety_factor), False)


# --- configuration --------------------------------------------------------------------------


class ModelCapability(ContractModel):
    """Deployment-supplied limits for one model; the harness hard-codes none."""

    model_id: str
    max_context_tokens: int
    reserved_output_tokens: int
    reserved_tool_schema_tokens: int = 0
    reserved_protocol_tokens: int = 0

    @field_validator("model_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_id must be nonblank")
        return value

    @model_validator(mode="after")
    def feasible(self) -> ModelCapability:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        for name in (
            "reserved_output_tokens",
            "reserved_tool_schema_tokens",
            "reserved_protocol_tokens",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.available_input_tokens <= 0:
            raise ValueError("reservations leave no input budget")
        return self

    @property
    def reserved_tokens(self) -> int:
        return (
            self.reserved_output_tokens
            + self.reserved_tool_schema_tokens
            + self.reserved_protocol_tokens
        )

    @property
    def available_input_tokens(self) -> int:
        return self.max_context_tokens - self.reserved_tokens


class ContextPolicy(ContractModel):
    """Agent-tunable policy. Instruction categories are always ranked first."""

    category_order: tuple[ContextCategory, ...] = _DEFAULT_ORDER
    truncation_enabled: bool = True
    min_truncated_tokens: int = 16
    per_segment_overhead_tokens: int = 0
    fixed_overhead_tokens: int = 0
    require_exact_counting: bool = False

    @field_validator("category_order")
    @classmethod
    def permutation(cls, value: tuple[ContextCategory, ...]) -> tuple[ContextCategory, ...]:
        if sorted(value) != sorted(_DEFAULT_ORDER):
            raise ValueError("category_order must order exactly the non-instruction categories")
        return value

    @field_validator("min_truncated_tokens", "per_segment_overhead_tokens", "fixed_overhead_tokens")
    @classmethod
    def non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("token settings cannot be negative")
        return value


# --- segments -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_id: str
    initiative_id: str | None = None
    citation: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe(self.source_id, "source_id")
        if self.initiative_id is not None:
            _safe(self.initiative_id, "initiative_id")
        citation = json.loads(json.dumps(dict(self.citation), allow_nan=False))
        object.__setattr__(self, "citation", _freeze_json(citation))


@dataclass(frozen=True, slots=True)
class ContextSegment:
    segment_id: str
    category: ContextCategory
    content: str
    priority: int = 100  # token-allocation order inside a category; lower is allocated first
    relevance: float = 0.0  # evidence ranking inside equal priority; higher first
    required: bool | None = None
    truncatable: bool = False
    content_format: ContentFormat = ContentFormat.TEXT
    source: SourceReference | None = None

    def __post_init__(self) -> None:
        _safe(self.segment_id, "segment_id")
        object.__setattr__(self, "category", ContextCategory(self.category))
        object.__setattr__(self, "content_format", ContentFormat(self.content_format))
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be nonblank text")
        if type(self.priority) is not int or not 0 <= self.priority <= _MAX_PRIORITY:
            raise ValueError("priority must be an int between 0 and 1000")
        if not math.isfinite(self.relevance) or not 0 <= self.relevance <= 1:
            raise ValueError("relevance must be between 0 and 1")
        if self.content_format is ContentFormat.JSON:
            json.loads(self.content)
        instruction = self.category in _INSTRUCTION_CATEGORIES
        required = instruction if self.required is None else self.required
        if instruction and (not required or self.truncatable):
            raise ValueError("instructions are always required and never truncatable")
        if self.category is ContextCategory.RETRIEVED_EVIDENCE:
            if required:
                raise ValueError("retrieved evidence cannot be required")
            if self.source is None or self.source.initiative_id is None:
                raise ValueError("evidence needs a source with an initiative_id")
        if required and self.truncatable:
            raise ValueError("required segments are never truncated")
        object.__setattr__(self, "required", required)

    @property
    def authority(self) -> InstructionAuthority:
        return _AUTHORITY[self.category]


@dataclass(frozen=True, slots=True)
class AssembledSegment:
    segment_id: str
    category: ContextCategory
    authority: InstructionAuthority
    content: str
    text: str  # exact rendered form that was counted
    tokens: int
    original_tokens: int
    truncated: bool
    source: SourceReference | None


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    segment_id: str
    category: ContextCategory
    reason: str  # initiative_mismatch | duplicate_evidence | over_budget


@dataclass(frozen=True, slots=True)
class BudgetReport:
    model_id: str
    available_input_tokens: int
    reserved_tokens: int
    used_tokens: int
    included_segments: int
    excluded_segments: int
    truncated_segments: int
    overflow_events: int
    counting_method: str  # "exact" | "estimated"
    provider_validation_required: bool

    @property
    def utilization(self) -> float:
        return self.used_tokens / self.available_input_tokens


@dataclass(frozen=True, slots=True)
class AssembledContext:
    segments: tuple[AssembledSegment, ...]
    exclusions: tuple[ExclusionRecord, ...]
    report: BudgetReport

    def render(self) -> str:
        return "\n".join(segment.text for segment in self.segments)


@dataclass(frozen=True, slots=True)
class BudgetFailure:
    code: str
    required_tokens: int
    available_input_tokens: int
    report: BudgetReport

    def to_execution_error(self) -> ExecutionError:
        return ExecutionError(
            code=self.code,
            message="Context could not be assembled within the model budget.",
            details={
                "required_tokens": self.required_tokens,
                "available_input_tokens": self.available_input_tokens,
            },
        )


# --- assembly -------------------------------------------------------------------------------


class _CountingFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class ContextAssembler:
    """Stateless apart from injected collaborators; safe for concurrent invocations."""

    def __init__(
        self,
        capability: ModelCapability,
        counter: TokenCounter,
        policy: ContextPolicy | None = None,
        telemetry: TelemetryProvider | None = None,
    ) -> None:
        self._capability = capability
        self._counter = counter
        self._policy = policy if policy is not None else ContextPolicy()
        self._telemetry = telemetry

    def assemble(
        self,
        segments: Sequence[ContextSegment],
        *,
        context: AgentContext,
        tool_schema_tokens: int | None = None,
    ) -> AssembledContext | BudgetFailure:
        capability = self._capability
        if tool_schema_tokens is not None:
            if type(tool_schema_tokens) is not int:
                raise TypeError("tool_schema_tokens must be an int")
            capability = capability.model_copy(
                update={"reserved_tool_schema_tokens": tool_schema_tokens}
            )
            ModelCapability.model_validate(capability.model_dump())
        ids = [segment.segment_id for segment in segments]
        if len(set(ids)) != len(ids):
            raise ValueError("segment IDs must be unique")
        budget = capability.available_input_tokens - self._policy.fixed_overhead_tokens
        outcome = self._select(segments, context, capability, budget)
        self._emit(outcome, context, capability.model_id)
        return outcome

    def _select(
        self,
        segments: Sequence[ContextSegment],
        context: AgentContext,
        capability: ModelCapability,
        budget: int,
    ) -> AssembledContext | BudgetFailure:
        policy = self._policy
        counts = _Counts(self._counter, capability.model_id, policy)
        exclusions: list[ExclusionRecord] = []
        rank = {
            **{category: index for index, category in enumerate(_INSTRUCTION_CATEGORIES)},
            **{category: 2 + index for index, category in enumerate(policy.category_order)},
        }
        indexed = list(enumerate(segments))
        eligible: list[tuple[int, ContextSegment]] = []
        seen_evidence: set[tuple[str, str]] = set()
        failed_required: list[ContextSegment] = []
        for index, segment in sorted(indexed, key=lambda item: _order(item, rank)):
            reason = None
            source = segment.source
            if source is not None and source.initiative_id not in (None, context.initiative_id):
                reason = "initiative_mismatch"
            elif segment.category is ContextCategory.RETRIEVED_EVIDENCE and source is not None:
                key = (source.source_id, _digest(segment.content))
                if key in seen_evidence:
                    reason = "duplicate_evidence"
                seen_evidence.add(key)
            if reason is None:
                eligible.append((index, segment))
            else:
                exclusions.append(ExclusionRecord(segment.segment_id, segment.category, reason))
                if segment.required:
                    failed_required.append(segment)

        def failure(code: str, needed: int) -> BudgetFailure:
            return BudgetFailure(
                code,
                needed,
                budget,
                _report(capability, counts, 0, len(exclusions), 0, needed, 1),
            )

        if failed_required:
            return failure("required_segment_unavailable", 0)
        chosen: dict[int, AssembledSegment] = {}
        try:
            used = 0
            for index, segment in eligible:
                if segment.required:
                    assembled = self._whole(segment, counts)
                    chosen[index] = assembled
                    used += assembled.tokens
            if used > budget:
                return failure("required_context_exceeds_budget", used)
            overflow = 0
            for index, segment in eligible:
                if segment.required:
                    continue
                assembled = self._whole(segment, counts)
                if used + assembled.tokens <= budget:
                    chosen[index] = assembled
                    used += assembled.tokens
                    continue
                overflow += 1
                shortened = self._truncate(segment, assembled, budget - used, counts)
                if shortened is None:
                    exclusions.append(
                        ExclusionRecord(segment.segment_id, segment.category, "over_budget")
                    )
                else:
                    chosen[index] = shortened
                    used += shortened.tokens
        except _CountingFailure as error:
            return failure(error.code, 0)
        ordered = tuple(
            chosen[index]
            for index, segment in sorted(
                ((i, s) for i, s in eligible if i in chosen),
                key=lambda item: _render_order(item, rank),
            )
        )
        truncated = sum(1 for segment in ordered if segment.truncated)
        report = _report(
            capability,
            counts,
            len(ordered),
            len(exclusions),
            truncated,
            used + policy.fixed_overhead_tokens,
            overflow,
        )
        return AssembledContext(ordered, tuple(exclusions), report)

    def _whole(self, segment: ContextSegment, counts: _Counts) -> AssembledSegment:
        text = _render(segment, segment.content, False)
        tokens = counts.count(text) + self._policy.per_segment_overhead_tokens
        return AssembledSegment(
            segment.segment_id,
            segment.category,
            segment.authority,
            segment.content,
            text,
            tokens,
            tokens,
            False,
            segment.source,
        )

    def _truncate(
        self,
        segment: ContextSegment,
        whole: AssembledSegment,
        room: int,
        counts: _Counts,
    ) -> AssembledSegment | None:
        policy = self._policy
        if not (policy.truncation_enabled and segment.truncatable):
            return None
        overhead = policy.per_segment_overhead_tokens

        def cost(content: str) -> int:
            return counts.count(_render(segment, content, True)) + overhead

        if segment.content_format is ContentFormat.JSON:
            candidate = _truncate_json(segment.content, lambda text: cost(text) <= room)
        else:
            candidate = _truncate_text(segment.content, lambda text: cost(text) <= room)
        if candidate is None:
            return None
        retained = cost(candidate)
        if retained < policy.min_truncated_tokens:
            return None
        return AssembledSegment(
            segment.segment_id,
            segment.category,
            segment.authority,
            candidate,
            _render(segment, candidate, True),
            retained,
            whole.original_tokens,
            True,
            segment.source,
        )

    def _emit(
        self, outcome: AssembledContext | BudgetFailure, context: AgentContext, model_id: str
    ) -> None:
        if self._telemetry is None:
            return
        report = outcome.report
        failed = isinstance(outcome, BudgetFailure)
        safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", model_id)
        suffix = f"model.{safe_model}.counting.{report.counting_method}"
        metrics = {
            "available_input_tokens": report.available_input_tokens,
            "reserved_tokens": report.reserved_tokens,
            "used_tokens": report.used_tokens,
            "utilization": report.utilization,
            "included_segments": report.included_segments,
            "excluded_segments": report.excluded_segments,
            "truncated_segments": report.truncated_segments,
            "overflow_events": report.overflow_events,
            "estimated_counting": float(report.counting_method == "estimated"),
        }
        try:
            self._telemetry.record_event(
                f"context_budget.{'failed' if failed else 'assembled'}.{suffix}", context=context
            )
            for name, value in metrics.items():
                self._telemetry.record_metric(
                    f"context_budget.{name}", float(value), context=context
                )
            if isinstance(outcome, BudgetFailure):
                self._telemetry.record_error(outcome.to_execution_error(), context=context)
        except Exception:
            pass  # Observability cannot replace the assembly outcome.


class _Counts:
    """Counts rendered text and tracks whether every count was exact."""

    def __init__(self, counter: TokenCounter, model_id: str, policy: ContextPolicy) -> None:
        self._counter = counter
        self._model_id = model_id
        self._policy = policy
        self.exact = True

    def count(self, text: str) -> int:
        try:
            result = self._counter.count(text, model_id=self._model_id)
            tokens, exact = result.tokens, result.exact
        except Exception:
            raise _CountingFailure("token_counting_failed") from None
        if type(tokens) is not int or tokens < 0 or type(exact) is not bool:
            raise _CountingFailure("token_counting_failed")
        if not exact:
            if self._policy.require_exact_counting:
                raise _CountingFailure("exact_counting_required")
            self.exact = False
        return tokens


def _report(
    capability: ModelCapability,
    counts: _Counts,
    included: int,
    excluded: int,
    truncated: int,
    used: int,
    overflow: int,
) -> BudgetReport:
    return BudgetReport(
        model_id=capability.model_id,
        available_input_tokens=capability.available_input_tokens,
        reserved_tokens=capability.reserved_tokens,
        used_tokens=used,
        included_segments=included,
        excluded_segments=excluded,
        truncated_segments=truncated,
        overflow_events=overflow,
        counting_method="exact" if counts.exact else "estimated",
        provider_validation_required=not counts.exact,
    )


def _safe(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SAFE_TEXT.match(value):
        raise ValueError(f"{name} must be nonblank without whitespace, quotes, or angle brackets")


def _digest(content: str) -> str:
    return hashlib.sha256(" ".join(content.split()).encode()).hexdigest()


def _order(
    item: tuple[int, ContextSegment], rank: Mapping[ContextCategory, int]
) -> tuple[int, int, float, int]:
    index, segment = item
    return (rank[segment.category], segment.priority, -segment.relevance, index)


def _render_order(
    item: tuple[int, ContextSegment], rank: Mapping[ContextCategory, int]
) -> tuple[int, ...]:
    """Evidence renders by relevance; everything else keeps the caller's (chronological) order."""
    index, segment = item
    if segment.category is ContextCategory.RETRIEVED_EVIDENCE:
        return (
            rank[segment.category],
            segment.priority,
            -round(segment.relevance * 1_000_000),
            index,
        )
    return (rank[segment.category], 0, 0, index)


def _render(segment: ContextSegment, content: str, truncated: bool) -> str:
    attrs = (
        f'id="{segment.segment_id}" category="{segment.category}" authority="{segment.authority}"'
    )
    lines: list[str] = []
    if segment.source is not None:
        attrs += f' source="{segment.source.source_id}"'
        if segment.source.citation:
            citation = json.dumps(_thaw(segment.source.citation), sort_keys=True)
            lines.append("citation: " + citation.replace("<", "\\u003c"))
    if truncated:
        attrs += ' truncated="true"'
    if segment.authority is not InstructionAuthority.INSTRUCTION:
        content = content.replace("<<", "<​<")  # untrusted text cannot forge delimiters
    return "\n".join([f"<<segment {attrs}>>", *lines, content, "<</segment>>"])


def _thaw(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _largest(count: int, fits: Callable[[int], bool]) -> int | None:
    """Largest k in [1, count] with fits(k), assuming monotonic cost; None when none fits."""
    low, high, best = 1, count, None
    while low <= high:
        middle = (low + high) // 2
        if fits(middle):
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def _truncate_text(content: str, fits: Callable[[str], bool]) -> str | None:
    def prefix(length: int) -> str:
        return content[:length].rstrip() + _TRUNCATION_MARKER

    length = (
        _largest(len(content) - 1, lambda size: fits(prefix(size))) if len(content) > 1 else None
    )
    return prefix(length) if length is not None else None


def _truncate_json(content: str, fits: Callable[[str], bool]) -> str | None:
    """Drop trailing items/keys only, so the result is always valid JSON."""
    value = json.loads(content)
    if isinstance(value, list):
        items: list[JsonValue] = value

        def build(size: int) -> str:
            return json.dumps(items[:size], separators=(",", ":"))

        total = len(items)
    elif isinstance(value, dict):
        pairs = list(value.items())

        def build(size: int) -> str:
            return json.dumps(dict(pairs[:size]), separators=(",", ":"))

        total = len(pairs)
    else:
        return None
    size = _largest(total - 1, lambda candidate: fits(build(candidate))) if total > 1 else None
    return build(size) if size is not None else None
