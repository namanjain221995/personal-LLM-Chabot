"""Query result in, grounded user-facing answer out.

    classify -> extract facts -> build context -> MAIN MODEL -> validate
             -> (one regeneration) -> validate -> render

The main model writes every normal answer. The deterministic fallback exists
only for the case where it has failed grounding twice, and it is traced as a
failure when it runs, so an evaluation counts how often the model could not
stay grounded rather than hiding it behind an answer that looks fine.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import fallback, tracing
from .classifier import classify_result
from .client import MainModelClient
from .config import AnswerConfig, load_answer_config
from .context import build_context
from .facts import extract_facts
from .generator import generate
from .models import (AnswerFailure, FinalAnswer, GroundingReport,
                     InterpretedResult, ResultType)
from .prompts import build_answer_prompt, build_regeneration_prompt
from .render import dedupe, render as render_answer
from .validator import validate_grounded_answer

log = logging.getLogger(__name__)


class AnswerService:
    def __init__(self, config: AnswerConfig | None = None, *,
                 root: str | Path = "salesforce_knowledge",
                 client: Any = None) -> None:
        self.config = config or load_answer_config(root)
        self.client = client or MainModelClient(self.config)

    # -- entry ------------------------------------------------------------
    def answer(self, question: str, query_result: Any, *,
               grounded_plan: Any = None, query_plan: Any = None) -> FinalAnswer:
        started = time.perf_counter()
        final = FinalAnswer(model=getattr(self.client, "model", ""),
                            model_role=self.config.model_role)
        final.trace.append(tracing.interpretation_started(question))

        result_type = classify_result(query_result, grounded_plan, query_plan)
        final.result_type = result_type.value
        final.trace.append(tracing.result_classified(result_type))

        if result_type is ResultType.ERROR_RESULT:
            # An operational failure is not a factual answer and is not the
            # main model's to explain. Sending a DuckDB error to a model that
            # must not speculate can only produce speculation.
            final.duration_ms = _elapsed(started)
            final.failure = AnswerFailure.MODEL_UNAVAILABLE
            final.failure_detail = "the query failed; no answer was generated"
            final.trace.append(tracing.final_response(final))
            return final

        mark = time.perf_counter()
        interpreted = extract_facts(query_result, result_type,
                                    question=question,
                                    grounded_plan=grounded_plan,
                                    query_plan=query_plan)
        final.trace.append(tracing.facts_extracted(interpreted, _elapsed(mark)))
        return self._explain(question, interpreted, final, started)

    def answer_interpreted(self, question: str, interpreted: InterpretedResult,
                           *, log_to: Any = None,
                           meaning: dict[str, Any] | None = None) -> FinalAnswer:
        """Typed results from the analytical path: facts already built by code.

        Percentages, comparisons, rankings, trends, schema and operational
        facts arrive with every number computed. One main-model call first
        interprets them (which facts answer the question, how sub-results
        relate) and then writes the answer; the same validator and fallback
        apply. `log_to` (a pipeline StageLog) receives the interpretation and
        answer stage records; `meaning` is the question's semantic intent.
        """
        started = time.perf_counter()
        final = FinalAnswer(model=getattr(self.client, "model", ""),
                            model_role=self.config.model_role)
        final.trace.append(tracing.interpretation_started(question))
        final.result_type = interpreted.result_type.value
        final.trace.append(tracing.result_classified(interpreted.result_type))
        final.trace.append(tracing.facts_extracted(interpreted, 0))
        final = self._explain(question, interpreted, final, started, meaning=meaning)
        if log_to is not None:
            _record_stages(final, log_to)
        return final

    def _explain(self, question: str, interpreted: InterpretedResult,
                 final: FinalAnswer, started: float,
                 meaning: dict[str, Any] | None = None) -> FinalAnswer:
        final.interpreted = interpreted
        context = build_context(interpreted, self.config)
        final.trace.append(tracing.context_created(context))
        context_dict = context.as_dict()
        if meaning:
            # The question's meaning in the user's own business words: what
            # the facts must answer. Presentation intent, not evidence.
            context_dict["question_meaning"] = meaning
        # When the context shows fewer rows than the result holds, the model is
        # TOLD how many it sees ("rows_shown: 50") and may say so. That number
        # is established -- by this layer -- so it is supported. Rejecting it
        # sent every long list to the fallback. Found live, 2026-09-29.
        if context.model_context_truncated:
            interpreted.supported_numbers.add(float(context.rows_sent_to_model))

        messages = build_answer_prompt(interpreted, context_dict)
        answer, report, detail = self._attempt(messages, interpreted, final,
                                               attempt=1)

        if answer is not None and not report:
            final.trace.append(tracing.regeneration_started(report))
            messages = build_regeneration_prompt(interpreted, context_dict,
                                                 answer, report)
            second, second_report, second_detail = self._attempt(
                messages, interpreted, final, attempt=2)
            final.regenerated = True
            final.trace.append(tracing.regeneration_completed(
                ok=second is not None and bool(second_report),
                detail=second_detail))
            if second is not None:
                answer, report, detail = second, second_report, second_detail

        if answer is not None and report:
            final.answer = answer
            final.grounding = report
            final.grounded = True
        else:
            final.answer = fallback.build_fallback(interpreted)
            if answer is not None:
                # The model's interpretation stands; only its wording failed.
                final.answer.interpretation = answer.interpretation
            final.grounding = report if answer is not None else None
            final.fallback_used = True
            final.grounded = False
            # The model answered but unusably (cut off at the token limit, not
            # JSON): it was reachable, so this is a rejected answer and the
            # verified-facts fallback applies. Only no response at all is an
            # outage (§52). Seen live: a 2,744-row answer hit max_tokens and
            # was reported as MAIN_MODEL_UNAVAILABLE.
            reached = any(c.get("finish_reason") for c in final.model_calls)
            final.failure = (AnswerFailure.GROUNDING_REJECTED if answer is not None
                             else AnswerFailure.MODEL_OUTPUT_MALFORMED if reached
                             else AnswerFailure.MODEL_UNAVAILABLE)
            final.failure_detail = detail
            final.trace.append(tracing.fallback_used(
                detail or "grounding rejected twice",
                report.codes() if report else []))

        # Dedupe BEFORE the freshness policy: a note that repeats the
        # freshness sentence must go with it, or hiding the freshness note
        # would still leave its copy in `notes`.
        final.answer = dedupe(final.answer)
        final.answer = self._apply_freshness_policy(final.answer, interpreted)
        final.text = render_answer(final.answer)
        final.duration_ms = _elapsed(started)
        final.trace.append(tracing.final_response(final))
        return final

    # -- one generation + validation ---------------------------------------
    def _attempt(self, messages: list[dict[str, str]],
                 interpreted: InterpretedResult, final: FinalAnswer,
                 *, attempt: int) -> tuple[Any, GroundingReport | None, str]:
        final.attempts = attempt
        final.trace.append(tracing.model_started(
            getattr(self.client, "model", ""), self.config.model_role, attempt))
        answer, meta, detail = generate(self.client, messages)
        final.model = meta.get("model") or final.model
        final.model_calls.append({"duration_ms": int(meta.get("duration_ms") or 0),
                                  "completion_tokens": int(meta.get("completion_tokens") or 0),
                                  "finish_reason": meta.get("finish_reason"),
                                  "ok": answer is not None, "detail": detail})
        final.trace.append(tracing.model_completed(
            meta, self.config.model_role, ok=answer is not None, detail=detail))
        if answer is None:
            return None, None, detail

        final.trace.append(tracing.validation_started(attempt))
        mark = time.perf_counter()
        report = validate_grounded_answer(answer, interpreted)
        final.trace.append(tracing.validation_completed(report, _elapsed(mark)))
        if not report:
            detail = "; ".join(v.detail for v in report.violations)
        return answer, report, detail

    # -- freshness presentation ---------------------------------------------
    def _apply_freshness_policy(self, answer: Any,
                                interpreted: InterpretedResult) -> Any:
        """Drop the freshness note when policy says not to show it.

        Done here rather than as a grounding violation on purpose. Whether to
        mention data age is a presentation choice, and spending the single
        regeneration on it would cost a correct answer for a cosmetic rule.
        """
        if answer.freshness_note and not self.config.freshness.should_show(
                interpreted.freshness.status):
            answer.freshness_note = None
        return answer


def _record_stages(final: FinalAnswer, log_to: Any) -> None:
    """The answer call's stage records: answer, and interpretation carried by it."""
    from pipeline.stage_model import ModelCall
    calls = final.model_calls
    unavailable = final.failure is AnswerFailure.MODEL_UNAVAILABLE
    record = log_to.add(ModelCall(
        stage="answer", model_role=final.model_role, model=final.model,
        model_called=bool(calls), ok=bool(calls) and not unavailable,
        duration_ms=sum(c["duration_ms"] for c in calls),
        completion_tokens=sum(c["completion_tokens"] for c in calls),
        valid=final.grounded, retries=max(0, len(calls) - 1),
        failure="unavailable" if unavailable else "",
        error=final.failure_detail if final.failure else "",
        selected={"result_type": final.result_type, "grounded": final.grounded,
                  "fallback_used": final.fallback_used}))
    interpretation = final.answer.interpretation if final.answer is not None else None
    log_to.combined("interpretation", record, selected=interpretation,
                    valid=interpretation is not None)


def _elapsed(mark: float) -> int:
    return int((time.perf_counter() - mark) * 1000)
