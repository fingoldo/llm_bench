"""Subpackage-level re-export tests (audit: 01-Low).

``pool/__init__.py`` and ``stage/__init__.py`` previously had no
re-exports at all, so ``from llm_bench.pool import TaskPool`` /
``from llm_bench.stage import PromptBuilder, ...`` raised
``ImportError`` even though the top-level ``llm_bench`` package
re-exports these same Protocols fine. ``tests/test_meta/
test_api_stability.py`` only snapshots the top-level ``llm_bench.__all__``,
not subpackage-level exports, so a regression here wouldn't be caught
anywhere else.
"""

from __future__ import annotations


class TestPoolReexports:
    def test_task_pool_importable_from_pool_subpackage(self):
        from llm_bench.pool import TaskPool

        assert TaskPool is not None

    def test_task_pool_matches_top_level_export(self):
        from llm_bench import TaskPool as TopLevelTaskPool
        from llm_bench.pool import TaskPool as SubpackageTaskPool

        assert SubpackageTaskPool is TopLevelTaskPool


class TestStageReexports:
    def test_protocols_importable_from_stage_subpackage(self):
        from llm_bench.stage import GoldChecker, PromptBuilder, ResponseParser, StageContext

        assert StageContext is not None
        assert PromptBuilder is not None
        assert ResponseParser is not None
        assert GoldChecker is not None

    def test_stage_context_matches_base_module_class(self):
        # StageContext isn't in llm_bench's own top-level __all__ (it's
        # an internal pipeline-state carrier, not part of the public
        # extension-point surface) -- just confirm the subpackage
        # import path itself resolves to the real implementation class.
        from llm_bench.stage import StageContext as SubpackageStageContext
        from llm_bench.stage.base import StageContext as BaseStageContext

        assert SubpackageStageContext is BaseStageContext

    def test_prompt_builder_matches_top_level_export(self):
        from llm_bench import PromptBuilder as TopLevelPromptBuilder
        from llm_bench.stage import PromptBuilder as SubpackagePromptBuilder

        assert SubpackagePromptBuilder is TopLevelPromptBuilder
