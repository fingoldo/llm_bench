# Аудит: HTTP/API-клиенты и LLM-специфичные практики

## Scope & method (что прочитано, что запущено)

Прочитаны полностью (не выдержками):
- `src/llm_bench/cost/openrouter.py`, `cost/catalogue.py`, `cost/estimator.py`, `cost/filter.py`
- `src/llm_bench/runner/classify.py`, `runner/round_runner.py`, `runner/benchmark.py`, `runner/resume.py`, `runner/budget.py`
- `src/llm_bench/stage/base.py`
- `src/llm_bench/core/hashing.py`, `core/types.py`, `core/scoring.py`
- `src/llm_bench/halving/pairing.py`, `halving/driver.py`, `halving/assignment.py`, `halving/schedule.py`, `halving/alive_filter.py`
- `src/llm_bench/ranking/ranker.py`, `ranking/per_stage_winners.py`
- `src/llm_bench/storage/base.py`, `storage/postgres.py` (полностью, включая все f-string SQL-сайты), `storage/file.py`, `storage/memory.py`
- `tests/unit/test_cost.py`, `tests/integration/test_smoke_in_memory.py`, `README.md`

Команды (read-only):
- `grep -rn "import httpx" src/` → 0 совпадений (подтверждено дважды)
- `grep -rn "reasoning_tokens\|cost_usd" src/` — прослежены все точки суммирования/агрегации
- `grep -rn "load_winners\|load_winner_substrate\|is_validator\|allowed_validator_pairs\|select_validator_for_producer\|quarantin\|parse_failure_prefix\|budget_per_call" src/ tests/` — трассировка «объявлено vs реально вызывается»
- `grep -rln "classify_provider_error" tests/` → 0 совпадений
- `grep -rln "openrouter\|_entry_from_or_row" tests/` → 0 совпадений на реальные unit-тесты модуля
- `python -m pytest -m "not live" --no-cov -q tests/unit tests/integration` → **112 passed** (см. интерпретацию ниже — зелёный сьют не покрывает найденные архитектурные дыры)

Ничего не изменялось, не форматировалось, git не трогался. Единственный созданный файл — этот отчёт.

## Summary table

| Severity | File:Line | One-line summary |
|---|---|---|
| Critical | `runner/round_runner.py:82-165,168-313`, `ranking/per_stage_winners.py:1-18,81-114` | Механизм "M_X per-stage winner substrate" объявлен как ключевая фича, но никогда не читается обратно — `WinnerSet` только пишется, следующий раунд использует собственный (не победивший) апстрим кандидата |
| Critical | `core/types.py:79-89`, `stage/base.py:46-57`, `halving/pairing.py` (весь файл) | `Stage.is_validator` / cross-family валидатор-независимость никогда не применяется раннером; `PromptBuilder` Protocol вообще не передаёт producer model_id, поэтому задокументированное поведение нереализуемо через штатную точку расширения |
| Critical | `runner/round_runner.py:236-298`, `runner/resume.py:75-84`, `core/types.py:228`, `halving/assignment.py:38-66` | Отказ парсера / отказ модели / обрезанный ответ без HTTP-исключения молча трактуется как успех: кешируется навсегда (в т.ч. tag-agnostic межзапускный кеш), `default_row_scorer` ставит 1.0, `parse_failure_prefix` документирован, но никогда не проставляется |
| High | `runner/budget.py:49-73`, `runner/round_runner.py:201-207` | TOCTOU-гонка в `BudgetGate.check()` при реальной конкурентности (`global_concurrency=30` по умолчанию) — бюджет может быть превышен на порядок конкурентности |
| High | `core/types.py:91-94`, `runner/budget.py:49-73`, `runner/round_runner.py:202-206` | `Stage.budget_per_call` документирован как вход в проекцию бюджет-гейта, но нигде не читается — гейт использует хардкод $0.01 для первого вызова op |
| Medium | `stage/base.py:22-24,38-39` | `StageContext.quarantined`/`quarantine_reason` документированы как "set by storm-detection logic in the runner", но нигде не устанавливаются |
| Medium | `storage/postgres.py:137-167` и 18 сайтов `f"...{self._schema}..."` | `schema_name` не валидируется/не аллоклистится нигде — докстринг заявляет безопасность, которую код не обеспечивает |
| Medium | `cost/openrouter.py:179-203` | Ноль собственной устойчивости (try/except/retry/фолбэк) вокруг `list_openrouter_models(...)` — любое необработанное pyutilz-исключение обрушивает весь discovery/estimate без деградации |
| Medium | `runner/budget.py:75-79` vs `runner/round_runner.py:300` и `storage/*.py query_spend_by_op` | Несогласованность базы стоимости: `record_cost()` пишет "сырую" `cost_usd`, а `check()` сравнивается с накопленной `effective_cost_usd` (с учётом кеш-скидки) |
| Medium | `cost/openrouter.py` (весь файл) — нет теста | Нулевое покрытие тестами маппинга сырых полей OpenRouter → `CatalogueEntry` (`_entry_from_or_row`, `_pick_best_upstream`, `_per_token_to_per_m`, `_normalise_uptime`) |
| Medium | `runner/classify.py` (весь файл) — нет теста | Нулевое покрытие тестами `classify_provider_error`, несмотря на задокументированный исторический баг классификации (комментарий "lesson from the live run on 2026-05-05") |
| Low | `halving/pairing.py:137-138` | Мёртвая строка `eligible.sort(...)`, немедленно перезаписываемая `rng.shuffle(...)` |
| Low | `runner/classify.py:12-39` | Классификация по подстрокам в сообщении об ошибке — хрупкая к изменению формулировок апстрима, не покрыта таблицей реальных сообщений |
| Low | `cost/filter.py:37-39,68-73` | `CostFilter.require_healthy=True` — молчаливый no-op для любого не-OR `ModelCatalogue` (uptime всегда `None` → "opaque pass") |
| Info | `src/llm_bench/**` | Прямых обращений к `httpx` в репозитории нет — весь HTTP-слой полностью делегирован `pyutilz` |
| Info | `runner/round_runner.py:301-312` | Единственная retry/backoff-подобная логика, реально принадлежащая этому репо — грубый circuit breaker "3 DEAD-класса ошибок → прервать оставшиеся стадии этого пайплайна"; остальное — зона ответственности pyutilz |

## Findings

### 1. [Critical] Per-stage winner ("M_X") substrate promotion объявлен, но никогда не выполняется
**File:Line:** `src/llm_bench/runner/round_runner.py:82-165` (`run_round`), `src/llm_bench/runner/round_runner.py:168-313` (`_run_pipeline`), `src/llm_bench/ranking/per_stage_winners.py:1-18, 81-114` (`load_winner_substrate`)

**Описание.** README (строка 9) заявляет "per-stage winner promotion" как одну из headline-фич фреймворка. Докстринг `ranking/per_stage_winners.py:1-18` формулирует это как "центральное архитектурное ограничение multi-round Halving": когда кандидат Y выполняет стадию k+1, апстрим-контекстом для его вызова должен быть распарсенный вывод **победителя стадии k (M_k)**, а НЕ собственный вывод Y на стадии k — иначе результат стадии k+1 маскирует реальные способности Y на стадии k+1.

Фактически: `run_round()` (round_runner.py:82-165) в конце раунда действительно вызывает `select_per_stage_winners(...)` и `await cfg.storage.persist_winners(...)` (строка 160-164) — WinnerSet пишется в storage. Но:
- `_run_pipeline()` (round_runner.py:168-313) на каждый (model, task_unit) создаёт **новый** `StageContext(task_unit=task_unit)` (строка 178) и заполняет `ctx.outputs[stage.id]` **исключительно** результатом парсинга **собственного** вызова этой же модели (строка 246: `ctx.outputs[stage.id] = parsed`). Никакого чтения предыдущего `WinnerSet` нет вообще.
- `RoundConfig` (round_runner.py:56-79) не содержит поля для передачи предыдущего `WinnerSet` в следующий раунд.
- `Benchmark.run_phase()` (`runner/benchmark.py:259-285`) в цикле по раундам создаёт новый `RoundConfig` на каждой итерации без переноса победителей.
- `storage.load_winners()` и `load_winner_substrate()` вызываются **только из тестов** (`tests/unit/test_storage_protocol.py`, `tests/integration/test_smoke_in_memory.py`) — grep по всему `src/` подтверждает: 0 вызовов из runner/benchmark кода.

Сам докстринг `load_winner_substrate` (per_stage_winners.py:93-98) прямо предсказывает эту поломку: *"The round driver invokes this BEFORE building any candidate's prompt for stage k+1 ... Without this wiring, 'M_X promotion' is write-only and stage k+1 silently uses each candidate's own upstream output."* — именно это и происходит в проде.

Тест `tests/integration/test_smoke_in_memory.py:204-235` (`test_smoke_winner_persisted_per_round`) проверяет ТОЛЬКО факт записи (`storage.load_winners(...) is not None`), не проверяет, что победитель раунда N реально используется как substrate в раунде N+1 — поэтому зелёный CI не защищает от регрессии/отсутствия wiring.

**Почему это важно / сценарий отказа.** В нормальном, неадверсариальном прогоне из 2-4 раундов Sequential Halving: стадия `validate_translate_ru` кандидата Y в раунде 3 должна валидировать перевод **лучшей на данный момент модели**, а не собственный (возможно, слабый) перевод Y. Сейчас она валидирует свой же перевод — это ломает заявленную цель "decouple chain quality from any one candidate's stage k mistakes" и системно искажает ranking всех стадий, зависящих от `parent_stage`, начиная со 2-го раунда любого многораундового прогона. Это не крайний случай — это стандартный путь выполнения любого прогона с `rounds=[1,2,3,4]`.

**Recommendation.** Либо реализовать wiring: перед `_build_prompt` в `_run_pipeline` подгружать `WinnerSet` предыдущего раунда через `storage.load_winners(...)` и подменять `ctx.outputs[stage.parent_stage]` на распарсенный вывод `load_winner_substrate(...)`, либо (если это сознательный компромисс альфа-версии) немедленно исправить README/докстринги, чтобы не заявлять нереализованную фичу как рабочую — иначе потребители (VocabApp/JobApp) получают тихо неверные ranking-результаты, полагаясь на документацию.

---

### 2. [Critical] Cross-family validator independence (`is_validator`) никогда не применяется; Protocol не даёт для этого данных
**File:Line:** `src/llm_bench/core/types.py:79-89` (докстринг + поле `Stage.is_validator`), `src/llm_bench/stage/base.py:46-57` (`PromptBuilder` Protocol), `src/llm_bench/halving/pairing.py` (весь файл — `allowed_validator_pairs`, `select_validator_for_producer`)

**Описание.** Докстринг `Stage.is_validator` (types.py:79-83) прямо заявляет: *"When a candidate pool is supplied, the framework swaps in a different candidate as the validator (Layer-1 hard cross-family filter) so a model never validates its own output."* README (строка 20) перечисляет "Validator-pairing for cross-family scoring" как ключевую фичу верхнего уровня, а `halving/pairing.py` реализует продуманный алгоритм выбора cross-family валидатора (Layer 1 hard / Layer 2 soft, обработка N=1/2/3/≥4 отдельно).

Фактически:
- `grep -rn "is_validator" src/llm_bench` находит **только** объявление поля (`types.py:89`) и упоминания в докстрингах/комментариях (`types.py:79`, `halving/pairing.py:3`) — ни разу флаг не читается в исполняемом коде.
- `grep -rn "allowed_validator_pairs\|select_validator_for_producer" src/llm_bench` находит только определения функций и их экспорт в `__init__.py` — ни одного вызова из `runner/round_runner.py` или `runner/benchmark.py`.
- Что критичнее: даже если бы консьюмер попытался сам вручную реализовать cross-family swap внутри своего `prompt_builder`, это невозможно в принципе — `PromptBuilder` Protocol (`stage/base.py:54-57`) передаёт только `task_unit`, `ctx`, `lang`. Ни модель ("producer"), для которой сейчас строится промпт, ни список кандидатов не передаются. `StageContext` (`stage/base.py:14-39`) тоже не несёт текущую модель. То есть у консьюмерского `prompt_builder` физически нет данных, чтобы вызвать `select_validator_for_producer(producer_model=?, candidate_pool=?)`.

**Почему это важно / сценарий отказа.** Любой прогон, где хоть одна `Stage(is_validator=True)` задана (в т.ч. пример из README, quick-start: `Stage(id="validate_draft", parent_stage="draft", is_validator=True, ...)`) — стадия validate_draft читает `ctx.outputs["draft"]`, которое всегда является собственным выводом ТОЙ ЖЕ модели (см. Finding #1: `ctx.outputs` заполняется только своим кандидатом). Модель систематически валидирует саму себя — ровно то, что докстринг называет "self-judgment is forbidden". Итоговый score валидатор-стадии для self-consistent, но некорректных моделей будет искусственно завышен без единого предупреждения в логах.

**Recommendation.** Либо расширить `PromptBuilder`/`StageContext` полем `model: str` (+ опционально `candidate_pool: list[str]`) и реализовать реальный swap в `_run_pipeline` перед вызовом `stage.prompt_builder` для `stage.is_validator=True`, либо явно задокументировать, что `is_validator`/`halving/pairing.py` — это utility-функции, которые консьюмер обязан оркестрировать САМ вне фреймворка (и тогда снять формулировку "the framework swaps in..." из докстринга, т.к. она вводит в заблуждение).

---

### 3. [Critical] Отказ парсинга / отказ модели / обрезанный ответ без HTTP-исключения = тихий "успех", кешируется навсегда
**File:Line:** `src/llm_bench/runner/round_runner.py:236-249` (парсинг), `runner/round_runner.py:284-298` (запись + кеширование), `runner/resume.py:75-84` (`ResumeCache.put`, без фильтра длины/валидности), `core/types.py:228` (`parse_failure_prefix`, никогда не проставляется), `ranking/ranker.py:59-70` (`default_row_scorer`), `halving/assignment.py:38-66` (prefix-nested task-unit slicing, из-за которой баг проявляется и ВНУТРИ одного прогона)

**Описание.** Ответ провайдера обрабатывается так:
```
parsed = _parse_safely(stage, task_unit, response_text, in_tok, out_tok) if response_text else None
...
row = RunRow(..., response=response_text, error_class=error_class, error_message=error_message, ...)
await cfg.storage.record_call(row)
if not error_class:
    await cfg.resume_cache.put(CachedResponse(...), provider="openrouter", model=model)
```
(round_runner.py:245-298)

`error_class` устанавливается ТОЛЬКО когда `provider.generate(...)` бросает исключение (`except Exception as e:` блок, строки 226-231) либо когда `_call_llm` кладёт `telemetry["error_class"]` — но `_call_llm` (строки 346-385) **никогда не заполняет** ключи `"error_class"`/`"error_message"` в telemetry (список маппинга атрибутов на строках 365-381 их не содержит). То есть при HTTP 200 с любым содержимым (пустая строка, отказ модели "I cannot assist with that", обрезанный на `max_tokens` JSON, ответ, не проходящий JSON-схему) `error_class` остаётся `None`.

Последствия:
1. `_parse_safely` (round_runner.py:329-343) при неудаче парсинга ловит исключение и возвращает `None`, но это **никак не отражается на `error_class`/`RunRow`** — `parsed=None` живёт только в локальной переменной `ctx.outputs[stage.id]`, которая нигде не персистится.
2. `parse_failure_prefix` — поле `RunRow` (`core/types.py:228`), докстринг `ResponseParser` Protocol (`stage/base.py:60-67`) прямо обещает: *"Returning None signals an unrecoverable parse failure — the pipeline records the row with parse_failure_prefix set..."* Grep подтверждает: `parse_failure_prefix` присваивается **только** в конструкторе схемы БД/маппинге storage-бэкендов (постоянно `None`), но `round_runner.py` при конструировании `RunRow` (строки 252-283) этот параметр вообще не передаёт. Обещание из докстринга не выполняется нигде в коде.
3. `default_row_scorer` (`ranking/ranker.py:59-70`) считает успехом любую строку `len(row.response) >= 20` при `error_class` falsy — то есть отказ модели или "обрезанный, но длинный" JSON получает score=1.0.
4. Раз `error_class is None`, строка попадает в `cfg.resume_cache.put(...)` (round_runner.py:286-298) **безусловно** — `ResumeCache.put` (`runner/resume.py:75-84`) не делает НИКАКОЙ проверки длины/валидности ответа (в отличие от storage-бэкендов на этапе `prefetch_resume_cache`, где есть `min_response_len=20`). Кеш **tag-agnostic** (docstring `runner/resume.py:1-11`, `storage/base.py:19-21`): один "успешный" мусорный ответ отравляет резюме-кеш для ЛЮБОГО будущего эксперимента с тем же `(composite_hash, provider, model, thinking)`.
5. Даже длина ≥20 не спасает при постоянном хранении: SQL/SQLite/in-memory `prefetch_resume_cache` фильтрует только `len(response) < 20` — реалистичный отказ модели ("I'm sorry, but I can't help with that request.") длиннее 20 символов **проходит** этот фильтр и грузится обратно в кеш при следующем старте (`ResumeCache.populate_from_storage`, `runner/resume.py:45-60`), т.е. проблема переживает рестарт процесса.
6. Баг гарантированно проявляется даже ВНУТРИ одного многораундового прогона: `halving/assignment.py:38-66` документирует, что раунды используют `pool[:n_per_arm]` — то есть срез задач раунда 2 **включает** срез раунда 1 (например default `units_per_arm=(4,8,12,12)`). Тот же кандидат на той же стадии с той же задачей в раунде 2 хэширует идентичный промпт → cache hit на "успешно закешированный" отказ модели из раунда 1, без повторной попытки.

**Почему это важно / сценарий отказа.** Отказ модели / truncation / малформед-JSON — это НЕ адверсариальный или редкий кейс для LLM API, это обычное явление (safety-refusal, hitting `max_tokens`, модель "устала" следовать JSON-схеме). Итог: (a) такие строки тихо получают полный скор и участвуют в ranking наравне с реальными хорошими ответами; (b) они кешируются НАВСЕГДА без пути автоматического ретрая (в отличие от настоящих провайдерских ошибок, которые как раз ИСКЛЮЧАЮТСЯ из кеша и ретраятся); (c) отсутствует любой сигнал в БД ("parse_failure_prefix" всегда NULL), по которому позже можно было бы отфильтровать/перезапустить эти строки без ручного SQL-хирургии по содержимому `response`.

**Recommendation.**
- В `_run_pipeline` после `_parse_safely`, если `parsed is None` и `response_text` не пуст — проставлять `parse_failure_prefix` (например, `response_text[:200]`) в `RunRow`, и НЕ считать вызов кеш-пригодным (не звать `resume_cache.put`, либо звать с явным флагом "not cacheable").
- В `ResumeCache.put` продублировать минимальный фильтр длины/непустоты, который уже есть в storage-бэкендах — сейчас это единственная точка входа в кеш без проверки.
- Дать `default_row_scorer` (или отдельный дефолтный parse-aware scorer) доступ к признаку успешности парсинга, а не только к `error_class`/длине сырого текста.

---

### 4. [High] `BudgetGate.check()` — TOCTOU-гонка под реальной конкурентностью
**File:Line:** `src/llm_bench/runner/budget.py:49-73` (`check`), `src/llm_bench/runner/round_runner.py:104-106,201-207` (конкурентный вызов), `src/llm_bench/runner/budget.py:75-79` (`record_cost`)

**Описание.** `Benchmark.global_concurrency` по умолчанию 30 (`runner/benchmark.py:97`, `runner/round_runner.py:71`), а `run_round` (round_runner.py:105-119) запускает `_one_pair` для ВСЕХ пар (model, task_unit) через `asyncio.gather` под общим семафором на 30 одновременных корутин. Каждая пара независимо вызывает `cfg.budget_gate.check(cfg.storage, cfg.experiment_tag, stage.op)` (round_runner.py:202-206) ПЕРЕД вызовом LLM. `BudgetGate.check` (budget.py:49-73) читает `spent = await storage.query_spend_by_op(...)` — это сумма УЖЕ ЗАПИСАННЫХ в storage строк. Запись строки происходит только ПОСЛЕ завершения LLM-вызова (`await cfg.storage.record_call(row)`, round_runner.py:284), и `record_cost()` (budget.py:75-79) обновляет `last_call_cost_by_op` тоже только постфактум. Ни `check()`, ни промежуточный шаг между "check passed" и "call issued" не резервируют бюджет и не используют `self._lock` (тот `_lock` в `BudgetGate` используется ТОЛЬКО внутри `record_cost`, не внутри `check`).

**Почему это важно / сценарий отказа.** Если для одного `op` (например `translate`, дорогая reasoning-модель) одновременно стартует N корутин (до `global_concurrency`, если нет `per_op_concurrency` ограничения для этого op — оно опционально, `runner/round_runner.py:72-74`), все N вызовов `check()` в первом же `await` interleaving видят ОДИНАКОВЫЙ `spent` (ни одна из N ещё не записала свою стоимость), все N проходят гейт, все N реально выполняются и тратят деньги. Итоговый перерасход может составить `~(N-1) × real_per_call_cost` сверх заявленного `cap_usd`. Докстринг `runner/budget.py:1-11` прямо заявляет, что это "the same gate VocabApp uses to protect Phase 1's $20 budget envelope from runaway reasoning models" — при конкурентности по умолчанию 30 и стоимости reasoning-вызова $0.50-$2, потенциальный overshoot одной "волны" — десятки долларов, то есть гейт не защищает именно от того сценария, который заявлен как его смысл существования.

**Recommendation.** Либо резервировать бюджет атомарно под `asyncio.Lock` до фактического вызова (pessimistic reservation: `spent_reserved += projected` внутри лока, откат при неудаче), либо ограничивать per-op конкурентность до 1 там, где `budgets_by_op` задан для этого op, либо явно задокументировать текущее поведение как "best-effort, не hard cap" — сейчас докстринг обещает hard cap, а реализация даёт soft/racy cap.

---

### 5. [High] `Stage.budget_per_call` документирован, но никогда не используется гейтом
**File:Line:** `src/llm_bench/core/types.py:91-94` (объявление и докстринг поля), `src/llm_bench/runner/budget.py:49-73` (`BudgetGate.check`), `src/llm_bench/runner/round_runner.py:202-206` (сайт вызова)

**Описание.** `Stage.budget_per_call` (types.py:91) описан как: *"Soft per-call cost projection used by the budget gate. The gate checks `stage_total_spent + budget_per_call > stage_cap` before each call"*. Однако сигнатура `BudgetGate.check(self, storage, experiment_tag, op)` (budget.py:49-54) не принимает `stage` или `budget_per_call` вообще, а вызов в `round_runner.py:202-206` передаёт только `stage.op`. Реальная проекция считается так (budget.py:65-66):
```
last = self.last_call_cost_by_op.get(op, 0.0)
projected = max(last * budget.headroom_factor, 0.01)
```
Для ПЕРВОГО вызова любого `op` в прогоне `last_call_cost_by_op.get(op, 0.0)` возвращает `0.0` (записи ещё не было), и `projected` всегда становится хардкод `0.01` — независимо от того, что консьюмер явно задекларировал `budget_per_call=0.50` (пример из README quick-start, строка 47) или `0.10`/`0.05` (`tests/integration/test_smoke_in_memory.py:112,118`).

**Почему это важно / сценарий отказа.** Если реальная стоимость первого вызова дорогой стадии — $0.50, а `cap_usd` стадии установлен близко к ожидаемой суммарной стоимости (что естественно, раз консьюмер явно указал `budget_per_call`), гейт пропустит НЕСКОЛЬКО первых вызовов (до `cap_usd / 0.01` вызовов теоретически, ограничено только реальной конкурентностью и числом task units), прежде чем `last_call_cost_by_op` "догонит" реальность после первого завершённого вызова. Комбинируется с Finding #4 (гонка) — суммарный эффект: значимая часть защиты бюджета не работает именно в тот момент (первая волна вызовов дорогой стадии), когда она нужнее всего.

**Recommendation.** Либо прокидывать `stage.budget_per_call` в `BudgetGate.check(...)` и использовать его как проекцию по умолчанию ДО появления реальных данных (`max(last or stage.budget_per_call, ...)`), либо, если поле сознательно не используется (deprecated), убрать вводящий в заблуждение докстринг с `types.py:91-94`.

---

### 6. [Medium] `StageContext.quarantined`/`quarantine_reason` документированы, но никогда не устанавливаются
**File:Line:** `src/llm_bench/stage/base.py:14-39`

**Описание.** Докстринг `StageContext` (stage/base.py:22-24) заявляет: *"Carries quarantine state (set by storm-detection logic in the runner) so a runaway reasoning chain on one stage cancels the rest of the pipeline for that pair."* Поля `quarantined: bool = False` и `quarantine_reason: str | None = None` объявлены (строки 38-39). Grep по всему `src/` находит эти идентификаторы ТОЛЬКО в определении дата-класса — никакой "storm-detection logic" в `round_runner.py` или где-либо ещё не существует; поля никогда не читаются и не устанавливаются.

**Почему это важно.** Единственный реально существующий circuit breaker сейчас — грубый счётчик "3+ DEAD-класса ошибок → return" (`round_runner.py:301-312`), который реагирует только на классифицируемые провайдерские ошибки, а не на "runaway reasoning chain" (например, модель, которая генерирует аномально длинный/дорогой reasoning-трейс без ошибки) — сценарий, ради которого поле, судя по докстрингу, задумывалось. Не критично (в отличие от Finding #1-3), так как ничего не работает НЕПРАВИЛЬНО — просто заявленная защита отсутствует.

**Recommendation.** Либо реализовать storm-detection (например, по аномально высокой `duration`/`output_tokens+reasoning_tokens` относительно среднего по op) и устанавливать `ctx.quarantined=True` при обнаружении, либо убрать эти поля/докстринг как нереализованный задел.

---

### 7. [Medium] `PostgresStorage.schema_name` — SQL-инъекционный примитив без валидации, несмотря на заявление безопасности в докстринге
**File:Line:** `src/llm_bench/storage/postgres.py:137-167` (класс + `__init__`), 18 сайтов f-string-интерполяции `self._schema` по всему файлу (например строки 43, 203, 209, 215, 230, 266, 295, 324, 342, 356, 392, 405, 431, 437 — везде с `# nosec B608`)

**Описание.** `PostgresStorage.__init__` (postgres.py:155-167) принимает `schema_name: str = "llm_bench"` и сохраняет его как `self._schema = schema_name` **без какой-либо проверки** (regex-аллоклист, `str.isidentifier()`, экранирование через `asyncpg`/`quote_ident`). Класс-докстринг (строки 146-153) явно обосновывает подавление bandit B608: *"`self._schema` is set once at construction time from an operator-supplied config value, not per-request user input, so this isn't the SQL-injection pattern bandit's B608 heuristically flags (`# nosec B608` on each site, verified individually below)."* — но "verified individually below" не подкреплено НИКАКИМ кодом; это утверждение, а не проверяемый инвариант. Ни один из 18 сайтов не квотирует идентификатор.

Проверка по репозиторию: `grep -rn "schema_name\|PostgresStorage(" .` вне самого `postgres.py` не находит ни одного места конструирования `PostgresStorage`, кроме собственного файла (в `examples/job_app_cover_letter/run.py` используется `FileStorage`, не Postgres) — то есть реальный паттерн использования в этом репо неизвестен; проверить сценарий "откуда берётся `schema_name` в проде VocabApp" по этому репозиторию невозможно. Тестов, конструирующих `PostgresStorage` с нестандартным/враждебным `schema_name`, тоже нет (`tests/unit/test_storage_protocol.py:6,37` — параметризация на реальный Postgres целиком gated на `LLM_BENCH_TEST_DB_URL`, схема не варьируется).

**Почему это важно / сценарий отказа.** Докстринг сам называет правдоподобный вектор, который не рассмотрен: *"what happens if a deployer wires it from an env var or a multi-tenant config path?"* (это прямая цитата из задания на аудит, но вопрос по существу корректен). Если `schema_name` когда-либо попадёт в код из переменной окружения на per-tenant/per-deployment основе (обычный паттерн в SaaS вроде VocabApp, где у пользователя уже есть привычка префиксовать env-переменные по проекту — см. `memory/feedback_env_var_prefix_by_project`), либо из конфиг-файла, которым управляет менее доверенная сторона, чем "оператор БД" — получаем классический SQL injection через identifier-контекст (не через `$n`-параметр, где asyncpg безопасен, а через голый f-string в имени схемы). Например, `schema_name = "llm_bench; DROP SCHEMA public CASCADE; --"` пройдёт как обычная python-строка вплоть до выполнения DDL на `initialize()` (postgres.py:175-186, `CREATE SCHEMA IF NOT EXISTS {s};`).

Это НЕ помечается Critical, так как эксплуатация требует контроля над значением, которое по задумке класса действительно должно быть доверенным (operator-supplied at construction) — но задание аудита прямо просило независимую проверку этого самообоснования, и в коде НЕТ ни одной строчки, которая бы фактически ограничивала эту переменную identifier-безопасным подмножеством. Заявление "verified individually below" в докстринге вводит в заблуждение — верификации нет.

**Recommendation.** Добавить в `__init__` жёсткую валидацию (`re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", schema_name)` или использовать `asyncpg`/`psycopg`-совместимую quote-функцию для идентификаторов) и завести хотя бы один негативный тест (`PostgresStorage(url, schema_name="x; DROP TABLE")` должен `raise ValueError` при конструировании, а не долетать до SQL). Это дешёвая правка, которая полностью снимает вопрос вместо того, чтобы полагаться на "мы никогда так не сделаем".

---

### 8. [Medium] `cost/openrouter.py` — ноль собственной устойчивости к сбоям каталог-фетча
**File:Line:** `src/llm_bench/cost/openrouter.py:179-203` (`OpenRouterCatalogue.list_models`), `src/llm_bench/cost/estimator.py:210-215` (единственная точка вызова из фреймворка)

**Описание.** `list_models()` вызывает `list_openrouter_models(**kwargs)` из pyutilz (строка 197) без `try/except` любого рода. `estimate_top_n_by_cost` (`cost/estimator.py:210-215`) тоже не оборачивает `catalogue.list_models(...)`. Докстринг класса (openrouter.py:158-168) утверждает: *"Pyutilz handles caching (TTL on the catalogue fetch), health pre-flight..."* — то есть вся устойчивость (ретраи, TTL-кеш, деградация при недоступности OR API) полностью делегирована внешней библиотеке, которая не входит в этот репозиторий и не может быть проверена в рамках данного аудита.

**Почему это важно / сценарий отказа.** Если pyutilz исчерпает свои внутренние ретраи (или у него их вовсе нет для какого-то класса ошибок — 5xx, timeout, malformed JSON от OR) и выбросит исключение, оно долетает без изменений до вызывающего кода `Benchmark`/CLI-обвязки (которой формально не существует — `cli/main.py` отсутствует, см. контекст задания), обрывая discovery/cost-estimation целиком, без возможности fallback на последний известный успешный снимок каталога или на статический список кандидатов. Так как discovery — это разовый шаг ПЕРЕД стартом раундов (а не вызов на каждый раунд), "прервёт многочасовой прогон в середине" — не совсем точная формулировка (раунды catalogue заново не запрашивают), но она ТОЧНО помешает СТАРТОВАТЬ прогон при одном неудачном фетче, и в этом слое нет ни одной строчки кода этого репозитория, которая бы смягчала эту ситуацию.

**Recommendation.** Обернуть `list_openrouter_models(...)` в `try/except` с логированием + опциональным fallback (например, `estimate_top_n_by_cost` могла бы принимать заранее известный/кешированный список `CatalogueEntry` как аварийный путь), либо явно задокументировать в докстринге, что "выбор — целиком на совести pyutilz" (это честнее текущей формулировки, которая создаёт впечатление продуманной устойчивости).

---

### 9. [Medium] `BudgetGate` использует несогласованные базы стоимости: `cost_usd` для проекции vs `effective_cost_usd` для факта
**File:Line:** `src/llm_bench/runner/budget.py:75-79` (`record_cost`), `src/llm_bench/runner/round_runner.py:299-300` (вызов), `src/llm_bench/storage/memory.py:158-172`, `storage/file.py:287-294`, `storage/postgres.py:365-373` (`query_spend_by_op`, суммирует `effective_cost_usd`)

**Описание.** `check()` сравнивает `spent` (из `storage.query_spend_by_op`, который везде суммирует `effective_cost_usd` — см. `storage/base.py:127-136` докстринг "Group effective_cost_usd sum by op") с `projected`, построенным из `last_call_cost_by_op`. Но `record_cost` вызывается с "сырой" `cost_usd`, НЕ `effective_cost_usd` (round_runner.py:299-300: `await cfg.budget_gate.record_cost(stage.op, cost_usd)`, где `cost_usd = telemetry.get("cost_usd") or 0.0`, а не `eff_cost`). Для стадий с существенной кеш-скидкой (`effective_cost_usd < cost_usd` — именно это описывает `cache_discount_usd`/`cache_hit_tokens` в `RunRow`) две половины одного и того же гейта работают на разных единицах измерения.

**Почему это важно.** В худшем случае — не overspend (тот описан отдельно в Finding #4), а излишне консервативный/ложноположительный SKIP: проекция следующего вызова считается по завышенной (недисконтированной) исторической стоимости, тогда как фактически накопленный `spent` может расти медленнее (за счёт скидки). Стадия может преждевременно останавливаться ("SKIP"), хотя реального бюджета ещё достаточно — это снижает охват (coverage) кандидатов без необходимости и может ложно триггерить coverage gate в `halving/driver.py:104-117` (`coverage_min=0.7`).

**Recommendation.** Привести обе половины к одной базе — либо обе к `effective_cost_usd` (более честная метрика для гейта, раз она уже используется для `spent`), либо задокументировать сознательный выбор консервативной оценки.

---

### 10. [Medium] Нулевое тестовое покрытие маппинга `cost/openrouter.py` (парсинг сырого OpenRouter JSON)
**File:Line:** `src/llm_bench/cost/openrouter.py` (весь файл, функции `_entry_from_or_row`, `_pick_best_upstream`, `_per_token_to_per_m`, `_normalise_uptime`)

**Описание.** Это единственный код в репозитории, который парсит форму JSON-ответа стороннего внешнего API (OpenRouter model-list/pricing/health), с несколькими нетривиальными защитными ветвями: обработка `status` как строки vs числового кода (строки 70-79), нормализация uptime 0-100 vs 0-1 (строки 41-52), sentinel-цена `-1000000` у мета-роутеров (строки 22-38), два конкурирующих поля `context_length` (строки 108-113), выбор "лучшего" апстрима по составному ключу (строки 55-85). `grep -rn "_entry_from_or_row\|_pick_best_upstream\|_per_token_to_per_m\|_normalise_uptime\|OpenRouterCatalogue" tests/` даёт 0 совпадений — ни одной строки в `tests/unit/test_cost.py` или где-либо ещё, которая бы кормила эти функции сэмплом реального/synthетic ответа OR и проверяла результат.

**Почему это важно.** Именно эта логика больше всего подвержена молчаливой регрессии при изменении формата ответа OR API (например, если OR сменит представление `uptime` или переименует поле цены кеш-чтения) — а из всего аудируемого кода это единственное место, которое напрямую контактирует с "живой" формой стороннего API. `tests/unit/test_cost.py` тестирует только `estimate_call_cost`/`CostFilter`/`estimate_top_n_by_cost` на уже сконструированных `CatalogueEntry`, минуя весь маппинг-слой.

**Recommendation.** Добавить `tests/unit/test_openrouter_catalogue.py` с фикстурами реалистичных сырых строк OR (числовой и строковый `status`, uptime в обеих формах, sentinel-цена, отсутствующие поля) и утверждениями на итоговый `CatalogueEntry`.

---

### 11. [Medium] Нулевое тестовое покрытие `classify_provider_error`, несмотря на задокументированный исторический баг
**File:Line:** `src/llm_bench/runner/classify.py` (весь файл, 40 строк)

**Описание.** Докстринг `classify_provider_error` (classify.py:15-18) прямо ссылается на реальный инцидент: *"Order matters: ModelNotFound runs FIRST so 405s with 'Provider returned error' wording don't get mis-classified as ProviderError (lesson from the live run on 2026-05-05)."* Это явное свидетельство того, что порядок веток строкового матчинга (строки 20-38) уже один раз ломался в проде. `grep -rln "classify_provider_error" tests/` → 0 результатов: НИ ОДНОГО unit-теста, который бы фиксировал этот кейс регрессионно (в т.ч. ни один из тестов, входящих в общий сьют `112 passed`, не касается этой функции напрямую).

**Почему это важно.** Эта функция определяет членство в `DEAD_ERROR_CLASSES`/`TRANSIENT_ERROR_CLASSES` (`halving/alive_filter.py:44-90`), то есть напрямую управляет тем, будет ли кандидат исключён из пула как "мёртвый". Любая будущая правка порядка веток (например, добавление нового `if` перед существующим) может тихо воспроизвести баг 2026-05-05 без единого падающего теста, который бы это поймал — прямое противоречие принципу "Regression test every bug fix", уже применяемому в остальном проекте (судя по структуре `tests/`).

**Recommendation.** Добавить `tests/unit/test_classify.py` с табличным набором `(exc_name, message) -> expected_label`, включая явный кейс "405 Method not allowed при формулировке, содержащей 'Provider returned error'" → `ModelNotFound` (не `ProviderError`), чтобы зафиксировать урок 2026-05-05 машинно, а не только в комментарии.

---

### 12. [Low] `halving/pairing.py:allowed_validator_pairs` — мёртвая строка сортировки
**File:Line:** `src/llm_bench/halving/pairing.py:137-138`

**Описание.**
```python
eligible.sort(key=lambda v: used_validators_overall.get(v, 0))   # строка 137
rng.shuffle(eligible)                                              # строка 138 — тут же перемешивает
```
Результат сортировки на строке 137 полностью уничтожается немедленным `rng.shuffle` на строке 138 — вычисленный порядок никак не влияет на дальнейшее (реальная балансировка происходит позже, в финальном `eligible.sort(...)` на строках 139-145, который уже корректно использует `used_validators_overall` как первичный ключ после шаффла-тайбрейкера). Не баг по результату (финальный сорт всё равно корректен), но мёртвый код, вводящий в заблуждение при чтении — похоже на недоделанный рефактор.

**Recommendation.** Убрать строку 137 (или заменить на действительно значимую операцию, если задумывалось что-то другое).

---

### 13. [Low] `classify_provider_error` — хрупкий substring-matching без табличного покрытия реальных сообщений
**File:Line:** `src/llm_bench/runner/classify.py:12-39`

**Описание.** Классификация целиком построена на `in msg` подстроках (`"api error 404" in msg`, `"429" in msg`, `"quota" in msg and (...)"` и т.д.). Это принципиально хрупко к любому изменению формулировок апстрима (OpenRouter/провайдеров под ним) и потенциально к ложным срабатываниям (например, любое сообщение, содержащее подстроку `"429"` не в контексте HTTP-статуса — маловероятно, но не исключено, если апстрим когда-либо начнёт возвращать модель с идентификатором, включающим "429", в тексте ошибки). Уже отмечено как связанное с Finding #11 (нет тестов), выделено отдельно как самостоятельный дизайн-риск независимо от покрытия тестами.

**Recommendation.** См. Finding #11 — табличный тест одновременно закрывает и хрупкость (документирует контракт), и регрессионную защиту.

---

### 14. [Low] `CostFilter.require_healthy=True` — молчаливый no-op для любого не-OR каталога
**File:Line:** `src/llm_bench/cost/filter.py:37-39` (докстринг полей), `cost/filter.py:68-73` (`keep()`)

**Описание.** Логика (уже разобрана в ходе аудита как корректная по написанному коду):
```python
if self.require_healthy:
    if entry.best_uptime_30m is None or entry.best_uptime_30m < self.min_uptime:
        # When uptime is unavailable (non-OR catalogue), treat as opaque-pass
        if entry.best_uptime_30m is not None:
            return False
```
Комментарий (`cost/filter.py:70-71`) честно документирует своё поведение: при `best_uptime_30m is None` (любой `ModelCatalogue`, не заполняющий health-поля — например, гипотетический прямой Anthropic/OpenAI каталог, упомянутый в докстринге `cost/catalogue.py:1-6`) фильтр молча пропускает ВСЁ, несмотря на `require_healthy=True` по умолчанию. Это не баг относительно написанного поведения (комментарий явно предупреждает), но потенциальная ловушка для пользователя фреймворка, который включает `require_healthy=True` (дефолт) ожидая реального гейта, а получает его только для OR-каталога.

**Recommendation.** Расширить докстринг `CostFilter.require_healthy` (строки 37-39) явным предупреждением "no-op for catalogues that don't populate best_uptime_30m", либо (более строгий вариант) сделать это configurable — `require_healthy` с явным `on_missing_health: Literal["pass","fail"] = "pass"`.

---

### 15. [Info] Прямых обращений к `httpx` в `src/llm_bench` нет
**File:Line:** N/A — подтверждено `grep -rn "import httpx" src/` (0 совпадений, дважды перепроверено в ходе аудита)

**Описание.** Весь HTTP/LLM-вызов делегирован `pyutilz.llm.get_llm_provider`/`LLMProvider.generate(...)` (используется как `from pyutilz.llm import get_llm_provider`, `round_runner.py:359-360`). Это не находка-баг, а фиксация факта по прямому запросу задания: connection reuse, явные timeout'ы, retry/backoff на HTTP-уровне — вне зоны ответственности и вне видимости этого репозитория; их нельзя аудировать без доступа к исходникам pyutilz (не входит в этот репозиторий по условиям задания).

---

### 16. [Info] Retry/backoff, реально принадлежащий этому репозиторию — только грубый circuit breaker
**File:Line:** `src/llm_bench/runner/round_runner.py:301-312`

**Описание.** Единственная логика уровня "не дать одному кандидату продолжать жечь бюджет после серии ошибок", реализованная непосредственно в этом репозитории (а не в pyutilz):
```python
if error_class:
    cfg.halving.error_history.setdefault(model, []).append(error_class)
    if error_class in DEAD_ERROR_CLASSES:
        error_count += 1
        if error_count >= 3:
            logger.warning(...)
            return
```
Это НЕ retry (нет повторных попыток того же вызова) и не exponential backoff — это единоразовый обрыв ОСТАВШИХСЯ стадий текущего пайплайна (model, task_unit) после 3 ошибок из `DEAD_ERROR_CLASSES` (permanent-класс, не включает `RateLimited`/`LLMCallTimeout` — те входят в `TRANSIENT_ERROR_CLASSES`, `halving/alive_filter.py:79-90`, и не увеличивают этот локальный `error_count`, только накапливаются в `cfg.halving.error_history` для отдельного порога `transient_threshold=6` на уровне всего прогона, `halving/alive_filter.py:93-129`). Указано как явный ответ на пункт задания "does anything in THIS repo's own code implement or assume a particular retry/backoff policy" — ответ: нет retry, есть только два независимых счётчика-выключателя (per-pipeline permanent-порог=3, per-run transient-порог=6), оба без backoff/jitter, что для circuit breaker уместно (backoff нужен для retry, здесь retry вообще не делается этим репозиторием).

---

## Итог по севериям

- Critical: 3
- High: 2
- Medium: 6
- Low: 3
- Info: 2

Наиболее весомые находки (1-3) касаются не CI-проверяемых вещей (линтеры/типы/форматирование их не ловят в принципе — это семантические разрывы между докстрингами/README и реальным control flow), и все три подтверждены прямым чтением полного пути выполнения плюс grep, показывающим отсутствие вызовов. Зелёный офлайн-сьют (`112 passed`) не противоречит ни одной находке — существующие тесты либо не доходят до многораундового сценария, где проявляется Finding #1/#3, либо (Finding #2) проверяют write-only персистентность, не потребление.
