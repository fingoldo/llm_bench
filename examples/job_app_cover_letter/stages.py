"""Stage definitions for the JobApp benchmark.

Three-stage pipeline:
  1. ``research``: extract job intent + company signals (no parent).
  2. ``draft``: write the cover letter using the research output as
     context (parent=research).
  3. ``polish``: tighten tone, length, and structure (parent=draft).

Plus one validator:
  - ``validate_draft``: another model audits the draft against the job
    description for relevance + skill coverage.

No GoldChecker - the cover-letter task has no canonical right answer,
so ranking emerges from validator-pair scoring (the framework's default
when ``gold_checker=None``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from llm_bench import Stage, StageGraph

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Prompt builders
# ──────────────────────────────────────────────────────────────────────

def build_research_prompt(*, task_unit, ctx, lang=None):
    md = task_unit.metadata
    skills = ", ".join(md.get("required_skills") or []) or "(none listed)"
    sys = (
        "You are an expert at decoding JobApp job postings. Extract the "
        "buyer's true intent so a freelancer can craft a precise cover "
        "letter. Output STRICT JSON with these keys: intent (string), "
        "buyer_signals (list of strings), risk_flags (list of strings), "
        "skill_gaps (list of strings)."
    )
    user = (
        f"Job ID: {task_unit.id}\n"
        f"Company: {md.get('company')}\n"
        f"Title: {md.get('title')}\n"
        f"Industry: {task_unit.stratum}\n"
        f"Required skills: {skills}\n"
        f"Description:\n{md.get('description')}\n"
    )
    return sys, user


def build_draft_prompt(*, task_unit, ctx, lang=None):
    md = task_unit.metadata
    research = ctx.outputs.get("research") or {}
    sys = (
        "You are writing a cover letter for an JobApp freelance pitch. "
        "Use the supplied research output to address the buyer's intent "
        "directly, name-drop relevant skills, and stay under 200 words. "
        "Output STRICT JSON: { letter: string, key_skills_addressed: "
        "list of strings }. The letter must read as natural prose."
    )
    user = (
        f"Research output: {json.dumps(research, ensure_ascii=False)[:1500]}\n\n"
        f"Job title: {md.get('title')}\n"
        f"Company: {md.get('company')}\n"
        f"Description excerpt: {md.get('description', '')[:600]}\n"
    )
    return sys, user


def build_polish_prompt(*, task_unit, ctx, lang=None):
    draft = ctx.outputs.get("draft") or {}
    md = task_unit.metadata
    sys = (
        "You are a cover-letter editor. Polish the supplied draft for "
        "clarity, tone, and concision (<=180 words). Preserve every skill "
        "claim and the buyer-intent address. Output STRICT JSON with "
        "keys: letter (string, polished), edits_made (list of strings)."
    )
    user = f"Draft: {json.dumps(draft, ensure_ascii=False)[:1500]}\n\n" f"Original job title: {md.get('title')}\n"
    return sys, user


def build_validate_draft_prompt(*, task_unit, ctx, lang=None):
    draft = ctx.outputs.get("draft") or {}
    md = task_unit.metadata
    skills = ", ".join(md.get("required_skills") or []) or "(none)"
    sys = (
        "You are auditing a cover letter for an JobApp pitch. Grade it on "
        "three axes (each 0..1): intent_match, skill_coverage, tone. "
        "Output STRICT JSON: { intent_match: float, skill_coverage: "
        "float, tone: float, reasoning: string }."
    )
    user = (
        f"Cover letter: {json.dumps(draft, ensure_ascii=False)[:1500]}\n\n"
        f"Job title: {md.get('title')}\n"
        f"Required skills: {skills}\n"
        f"Description excerpt: {md.get('description', '')[:600]}\n"
    )
    return sys, user


# ──────────────────────────────────────────────────────────────────────
# Response parsers - permissive JSON parse with fallback
# ──────────────────────────────────────────────────────────────────────

def _parse_json_loose(response_text: str | None) -> Any:
    if not response_text:
        return None
    text = response_text.strip()
    # Strip markdown code fences when present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: find the first {...} block and try again.
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except json.JSONDecodeError:
                return None
    return None


def parse_research(*, task_unit, response_text, input_tokens, output_tokens):
    parsed = _parse_json_loose(response_text)
    if not isinstance(parsed, dict):
        return None
    return {
        "intent": str(parsed.get("intent", ""))[:500],
        "buyer_signals": list(parsed.get("buyer_signals") or [])[:10],
        "risk_flags": list(parsed.get("risk_flags") or [])[:10],
        "skill_gaps": list(parsed.get("skill_gaps") or [])[:10],
    }


def parse_draft(*, task_unit, response_text, input_tokens, output_tokens):
    parsed = _parse_json_loose(response_text)
    if not isinstance(parsed, dict):
        return None
    return {
        "letter": str(parsed.get("letter", ""))[:5000],
        "key_skills_addressed": list(parsed.get("key_skills_addressed") or [])[:20],
    }


def parse_polish(*, task_unit, response_text, input_tokens, output_tokens):
    parsed = _parse_json_loose(response_text)
    if not isinstance(parsed, dict):
        return None
    return {
        "letter": str(parsed.get("letter", ""))[:5000],
        "edits_made": list(parsed.get("edits_made") or [])[:20],
    }


def parse_validate(*, task_unit, response_text, input_tokens, output_tokens):
    parsed = _parse_json_loose(response_text)
    if not isinstance(parsed, dict):
        return None
    return {
        "intent_match": float(parsed.get("intent_match") or 0.0),
        "skill_coverage": float(parsed.get("skill_coverage") or 0.0),
        "tone": float(parsed.get("tone") or 0.0),
        "reasoning": str(parsed.get("reasoning", ""))[:1000],
    }


# ──────────────────────────────────────────────────────────────────────
# StageGraph factory
# ──────────────────────────────────────────────────────────────────────

def build_job_app_stage_graph() -> StageGraph:
    """Build the 4-stage JobApp pipeline (research -> draft -> polish + validator)."""
    return StageGraph([
        Stage(
            id="research", op="research",
            prompt_builder=build_research_prompt,
            parser=parse_research,
            budget_per_call=0.05,
        ),
        Stage(
            id="draft", op="draft",
            parent_stage="research",
            prompt_builder=build_draft_prompt,
            parser=parse_draft,
            budget_per_call=0.10,
        ),
        Stage(
            id="polish", op="polish",
            parent_stage="draft",
            prompt_builder=build_polish_prompt,
            parser=parse_polish,
            budget_per_call=0.05,
        ),
        Stage(
            id="validate_draft", op="validate_draft",
            parent_stage="draft", is_validator=True,
            prompt_builder=build_validate_draft_prompt,
            parser=parse_validate,
            budget_per_call=0.05,
        ),
    ])


# ──────────────────────────────────────────────────────────────────────
# Custom RowScorer - validate_draft rows produce continuous scores
# ──────────────────────────────────────────────────────────────────────

def job_app_row_scorer(row) -> float:
    """Score: validate_draft -> mean of intent_match/skill_coverage/tone;
    other stages -> default 1.0/0.0 success-rate.

    The validator's own assessment is the only continuous signal we have
    in the no-gold path. Reads the JSON it parsed back from the row's
    response (the parser stored it but the framework only persists the
    raw response_text - so we re-parse the response here).
    """
    if row.error_class:
        return 0.0
    if row.response is None or len(row.response) < 20:
        return 0.0
    if row.stage == "validate_draft":
        parsed = _parse_json_loose(row.response)
        if not isinstance(parsed, dict):
            return 0.0
        try:
            scores = [
                float(parsed.get("intent_match") or 0.0),
                float(parsed.get("skill_coverage") or 0.0),
                float(parsed.get("tone") or 0.0),
            ]
        except (TypeError, ValueError):
            return 0.0
        if not scores:
            return 0.0
        return max(0.0, min(1.0, sum(scores) / len(scores)))
    return 1.0
