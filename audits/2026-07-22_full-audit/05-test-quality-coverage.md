# Аудит: качество и покрытие тестов (llm_bench)

## Scope & method (что прочитано, что запускалось)

Прочитаны целиком (не выборочно):
- `src/llm_bench/core/{types,hashing,scoring}.py`
- `src/llm_bench/cost/{catalogue,estimator,filter,openrouter}.py`
- `src/llm_bench/halving/{driver,pruner,alive_filter,schedule,pairing,assignment}.py`
- `src/llm_bench/ranking/{ranker,per_stage_winners}.py`
- `src/llm_bench/runner/{benchmark,budget,classify,resume,round_runner}.py`
- `src/llm_bench/storage/{base,memory,file,postgres}.py`
- `src/llm_bench/pool/base.py`, `src/llm_bench/stage/base.py`
- `tests/conftest.py`, `tests/test_meta/conftest.py`
- `tests/unit/*.py` (test_cost, test_halving, test_per_stage_winners, test_ranker, test_storage_protocol)
- `tests/integration/*.py` (test_preflight, test_smoke_in_memory, test_job_app_example)
- `tests/test_meta/*.py` (все 8 файлов, включая `_code_audit_baseline.json`, `_api_snapshot.json`)
- `examples/job_app_cover_letter/{run,job_pool}.py` (частично, для проверки integration-теста)
- `pyproject.toml` (секции pytest/coverage/ruff/deptry/interrogate)

Команды (все read-only, офлайн):
- `pytest -m "not live" --no-cov -q -p no:randomly` — фактический прогон офлайн-сьюта (122 теста)
- `git log`/`git show`/`git blame`-эквиваленты по `pyproject.toml`, `tests/test_meta/_code_audit_baseline.json`
- `grep`/`ToolSearch`-эквиваленты по всему репо для проверки использования `hypothesis`, `pytest.mark.postgres`, `BudgetGate(`, `classify_provider_error`, `ResumeCache`, `moderated_penalty`, `_prompts.jsonl`, `load_winner_substrate`, `n_stages_attempted`
- Два изолированных **дискриминирующих repro-скрипта** (написаны и выполнены мной, не гипотезы) против реального кода репозитория — оба подтвердили баг фактическим выполнением:
  1. `repro_resume_coverage_bug.py` — воспроизводит "resume-after-interrupt" через реальный `Benchmark.run_phase` + `InMemoryStorage`.
  2. `repro_prompts_jsonl_bug.py` — воспроизводит баг идемпотентности `FileStorage.upsert_prompts` через реальный `FileStorage`.

Не запускалось: `pytest -m live` (платные вызовы — намеренно исключено), Postgres-тесты (нет `LLM_BENCH_TEST_DB_URL`, но, как показано ниже, для Postgres таких тестов вообще не существует).

## Резюме прогона офлайн-сьюта

```
collected 122 items
...
1 failed, 121 passed in 10.24s
FAILED tests/test_meta/test_code_audit_baseline.py::test_no_new_code_audit_findings
```

Единственный сбой — новая находка `docstring_args_incomplete [Low] cost/estimator.py:161` (докстрока `estimate_top_n_by_cost` не упоminает параметр `refresh` в секции `Args:` — подтверждено чтением `src/llm_bench/cost/estimator.py:161-203`, параметр `refresh: bool = False` объявлен в сигнатуре на строке 169, но отсутствует в `Args:`). Это, по всей видимости, следствие того, что локально в этом окружении `py-ci-shared`/`pyutilz` — sibling-репозитории, редактируемые (`pip install -e`) и опережающие коммит, на который был сделан последний `--refresh-code-audit-baseline` в llm_bench (`ebc1c78`, 2026-07-15); проверено: `py-ci-shared` — editable install из активно развивающегося соседнего репо. **Альтернативное прочтение**: если CI llm_bench клонирует `py-ci-shared` "как есть" на момент прогона (а не фиксированный тег), то CI сейчас тоже красный по этой же причине — это стоит проверить у автора отдельно. В любом случае это факт, зафиксированный по прямому указанию задания ("report what you observed").

## Summary table

| Severity | File:Line | Summary |
|---|---|---|
| Critical | `src/llm_bench/runner/round_runner.py:189-198` (vs `:303`) | Резюмированный (100% cache-hit) раунд обнуляет весь пул кандидатов через coverage-gate — `n_stages_attempted` не инкрементируется при cache-hit. Подтверждено рабочим repro. |
| High | `tests/unit/test_storage_protocol.py` + `src/llm_bench/storage/postgres.py` | `PostgresStorage` — production-бэкенд с самооправданным `# nosec B608` — не покрыт вообще ни одним тестом; `schema_name` нигде не валидируется. |
| High | `src/llm_bench/runner/budget.py:26-79` | `BudgetGate` (единственный защитный механизм бюджета) не покрыт вообще ни одним тестом, включая happy-path и exact-boundary. |
| High | `src/llm_bench/runner/round_runner.py` (весь файл, 386 строк) | Ядро оркестрации раунда не имеет ни одного прямого unit-теста; несколько defensive-веток недостижимы ни одним тестом. |
| High | `src/llm_bench/runner/classify.py:12-39` | `classify_provider_error` — чистая функция с 6 ветками и задокументированным историческим инцидентом ("live run on 2026-05-05") — покрыта лишь 1 из 6 веток. |
| Medium | `src/llm_bench/storage/file.py:104-126` | `FileStorage.upsert_prompts` проверяет идемпотентность по НЕПРАВИЛЬНОЙ таблице (`resume_cache`, т.е. индекс результатов) — подтверждено repro: текст промпта может вообще не попасть в `_prompts.jsonl`. |
| Medium | `src/llm_bench/ranking/per_stage_winners.py:110-113` | Тай-брейк "latest by ts" в `load_winner_substrate` инвертирован относительно собственной докстроки — подтверждено repro; ветка с >1 кандидатом вообще не тестируется. |
| Medium | `src/llm_bench/cost/openrouter.py` (весь файл) | Конкретный адаптер OpenRouter-каталога (`_per_token_to_per_m`, `_normalise_uptime`, `_pick_best_upstream`, `_entry_from_or_row`) не покрыт вообще ни одним тестом. |
| Medium | `src/llm_bench/cost/filter.py:41-43` | `CostFilter.moderated_penalty` — полностью мёртвое поле (объявлено, задокументировано, нигде не читается) — тестов нет, потому что нечего тестировать. |
| Medium | `tests/property/` (пусто) + `pyproject.toml:59` | `hypothesis>=6.0` объявлен как dev-зависимость, каталог `tests/property/` существует, но property-based тестов нет вообще ни одного. |
| Medium | `src/llm_bench/runner/resume.py` (весь файл) | `ResumeCache` не имеет ни одного прямого unit-теста (`populate_from_storage`, `get`, `put`, `__len__`). |
| Medium | `src/llm_bench/halving/pruner.py:145-192` | Paired-bootstrap ветка `mad_bootstrap_prune` (penalty за дисперсию и за стоимость, ~50 строк статистической логики) не покрыта вообще ни одним тестом. |
| Low | (весь офлайн-сьют) | `pytest -m "not live"` сейчас НЕ зелёный (1/122 упавший) — см. раздел выше. |
| Low | `src/llm_bench/halving/pairing.py:123-150` | Анти-коллизийная логика "избегать (A,B)+(B,A)" в `allowed_validator_pairs` (N>=4) не тестируется напрямую. |
| Low | `src/llm_bench/halving/driver.py` (весь `promote`) | `Halving.promote()` никогда не тестируется с ровно 1 выжившим кандидатом. |
| Low | `src/llm_bench/storage/{memory,file}.py` vs `postgres.py` | Разная точность суммирования (`float`-накопление vs. серверный `NUMERIC`) — не покрыто precision-чувствительным тестом; сейчас невидимо благодаря `pytest.approx`. |
| Info | `tests/test_meta/_code_audit_baseline.json` | Механизм — работающий "трещоточный" gate (не rubber stamp), но 17 бейзлайновых находок навсегда прощены без срока пересмотра. |
| Info | `pyproject.toml:82` | `-p no:randomly` — не задокументированный воркэраунд конкретного бага, а скопированный из pyutilz шаблон с первого коммита. |
| Info | `tests/integration/test_job_app_example.py` | Пример-консьюмер (`examples/job_app_cover_letter`) имеет достаточно честный e2e-тест — заметных пробелов не найдено. |

## Findings

### [CRITICAL] round_runner.py: resume-after-interrupt обнуляет весь пул кандидатов через coverage-gate

**File:Line**: `src/llm_bench/runner/round_runner.py:189-198` (пропущенный инкремент относительно строки `:303`); задействовано через `src/llm_bench/halving/driver.py:104-127` (coverage gate).

**Описание**: В `_run_pipeline` при попадании в resume-кэш (`cached is not None`, строка 193) выполняется `continue` (строка 198), которое пропускает весь остаток тела цикла, включая единственное место, где `n_stages_attempted[model]` инкрементируется (строка 303: `n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1`). Таким образом кэш-хиты НЕ засчитываются как "попытка стадии", хотя стадия фактически была успешно выполнена (просто в прошлом запуске). Эта величина затем передаётся в `Halving.promote(..., n_stages_attempted=n_stages_attempted, total_stages=len(cfg.stages.stages))` (`round_runner.py:150`) без явного `coverage_min`, то есть используется дефолт `coverage_min=0.7` (`halving/driver.py:82`). Coverage-gate вычисляет `cov = attempted / total_stages` и исключает модель, если `cov < coverage_min` (`halving/driver.py:107-115`).

**Почему это важно / сценарий отказа**: Ровно тот сценарий, который framework явно рекламирует как своё ключевое свойство ("cross-tag resume cache so an interrupted run doesn't re-pay for completed calls" — см. описание репозитория и докстроку `storage/base.py`). Если многораундовый прогон был прерван в раунде 2 и перезапущен тем же тегом (стандартный API-паттерн — `run_phase(tag=..., candidates=..., rounds=[1,2,3,4])`, что и является дефолтом `rounds` в `benchmark.py:238`), раунд 1 при повторном исполнении будет обслужен **на 100% из resume-кэша**. Тогда `n_stages_attempted` для КАЖДОГО кандидата останется на нуле → coverage-gate признаёт каждого кандидата "below coverage_min: 0/N = 0% < 70%" → `scores` становится пустым → `Halving.promote` возвращает `candidates_out=[]` (деградационная ветка `halving/driver.py:119-127`) → каскад в `Benchmark.run_phase` останавливается на следующем раунде с "no candidates left, halting cascade" (`benchmark.py:261`). Никакого исключения, никакой ошибки — только тихий пустой результат.

**Подтверждено выполнением** (`repro_resume_coverage_bug.py`, запущен через реальные `Benchmark`/`InMemoryStorage`):
```
=== First invocation (fresh) round 1 ===
candidates_out: ['a', 'b', 'd']
eliminated: ['c'] {'c': 'below halving cutoff'}

=== Second invocation (resumed / all cache-hit) round 1 ===
candidates_out: []
eliminated_reasons: {'a': 'below coverage_min: 0/1 = 0% < 70%', 'b': ..., 'c': ..., 'd': ...}
final_candidates after full resumed cascade: []
```

**Почему тесты это не поймали**: единственный тест, касающийся резюме (`tests/integration/test_smoke_in_memory.py::test_smoke_resume_cache_hits_on_rerun`), использует schedule с **одним раундом** (`round_sizes=(2,)`) и проверяет только `second_run_total_calls == 0` — то есть отсутствие новых LLM-вызовов, но никогда не инспектирует `candidates_out`/coverage-gate после резюме и никогда не запускает резюме в многораундовом сценарии, где эта бухгалтерия вообще имеет значение. Это ровно тот edge case ("resume-after-interrupt"), который задание явно просило проверить.

**Recommendation**: считать cache-hit как "попытку стадии" для целей coverage-gate (инкрементировать `n_stages_attempted[model]` и в ветке `cached is not None`, до `continue`), либо явно передавать в `Halving.promote` признак "этот раунд обслуживался из кэша" и отключать/ослаблять coverage-gate для таких раундов. Добавить регрессионный тест: многораундовый прогон, прерванный и резюмированный тем же тегом, должен воспроизводить те же survivors, что и непрерывный прогон.

---

### [HIGH] PostgresStorage не покрыт вообще ни одним тестом; self-graded `# nosec B608` не подкреплён валидацией `schema_name`

**File:Line**: `src/llm_bench/storage/postgres.py:137-153` (докстрока-обоснование), `:159-166` (`__init__`, `schema_name` без валидации), `tests/unit/test_storage_protocol.py:33-53` (фикстура параметризована только по `memory`/`file`).

**Описание**: `tests/unit/test_storage_protocol.py:37-39` содержит явный комментарий: `# PostgresStorage parametrization gated on LLM_BENCH_TEST_DB_URL — see test_storage_protocol_postgres.py (Phase D follow-up)`. Такого файла **не существует** — `find tests -iname "*postgres*"` не находит ничего, и это подтверждено. Маркер `postgres` зарегистрирован в `pyproject.toml:88`, фикстура `postgres_test_url` определена в `tests/conftest.py:36-43` — но ни то, ни другое не используется НИ ОДНИМ тестом в репозитории (`grep -rn "pytest.mark.postgres"` и `grep -rln "postgres_test_url"` дают только определения, ни одного использования). `PostgresStorage` вообще не импортируется ни в одном тестовом файле, кроме упоминания в докстроке `test_storage_protocol.py`.

Отдельно: класс-докстрока `postgres.py:146-153` заявляет, что f-string-интерполяция `self._schema` безопасна, поскольку "`self._schema` is set once at construction time from an operator-supplied config value, not per-request user input". Прочитан весь файл — **нигде** (`__init__`, `postgres.py:155-166`) `schema_name` не валидируется (нет `str.isidentifier()`, нет allowlist-регэкспа, нет проверки на кавычки/точки с запятой). Это не противоречие с "operator-supplied" тезисом, но означает, что данный тезис — единственная линия защиты, и она нигде не закреплена тестом, который документировал бы предполагаемый контракт (например, "конструктор должен отклонять `schema_name`, не являющийся валидным identifier").

**Почему это важно**: `PostgresStorage` — заявленный production-бэкенд (509 строк, включает DDL, `ON CONFLICT`, JSONB-кодирование). Ноль тестов означает: (а) ни один из пяти инвариантов контракта `BenchmarkStorage` (idempotent `record_call`, exclusion неуспешных строк из кэша, tag-agnostic кэш и т.д. — см. `storage/base.py:11-24`) фактически не проверен против Postgres-реализации; (б) если будущий рефакторинг случайно превратит `self._schema` в реально пользовательские данные (например, кто-то решит прокидывать `schema_name` из request-scoped multi-tenant конфига — ровно тот риск, который задание просило оценить), ни один тест этого не заметит; (в) `_record_to_row`'s "defensive" JSONB double-decode (`postgres.py:462-468`) и `query_rows`'s cursor-based streaming (`postgres.py:319-334`) — код, который никогда не выполнялся под тестами.

**Recommendation**: как минимум — создать `test_storage_protocol_postgres.py`, параметризованный через `LLM_BENCH_TEST_DB_URL` (как и обещано в комментарии), гейтящийся при отсутствии переменной. Дополнительно — unit-тест на `PostgresStorage.__init__`, документирующий/закрепляющий ожидание насчёт формата `schema_name` (даже если валидация не добавляется, тест должен явно фиксировать текущий "trust the caller" контракт, а не оставлять его только в докстроке).

---

### [HIGH] BudgetGate — защитный механизм бюджета — не покрыт вообще ни одним тестом

**File:Line**: `src/llm_bench/runner/budget.py:26-79`.

**Описание**: Класс `BudgetGate` (и вложенный `StageBudget`) реализует единственный защитный механизм от перерасхода ("protect Phase 1's $20 budget envelope from runaway reasoning models" — докстрока файла, строки 1-11). Проверено: `grep -rn "BudgetGate(" .` по всему репозиторию (включая `examples/`) не находит НИ ОДНОГО места, где `BudgetGate` реально инстанциируется — ни в тестах, ни в `examples/job_app_cover_letter`. Smoke-тест `tests/integration/test_smoke_in_memory.py:112,118` использует `budget_per_call=0.10` на `Stage`, но это поле не связано с `BudgetGate` — `Benchmark.budget_gate` (`benchmark.py:94`) остаётся `None`, а значит ветка `if cfg.budget_gate is not None:` в `round_runner.py:202-207` никогда не исполняется ни в одном тесте.

**Почему это важно / сценарий отказа**: логика `check()` (`budget.py:49-73`) содержит нетривиальную формулу: `projected = max(last * headroom_factor, 0.01)` и сравнение `spent + projected > cap`. Ни одна из следующих веток не протестирована: (1) happy path (проход при недостаточном spend), (2) skip-ветка при превышении cap, (3) exact-boundary (`spent + projected == cap` — по коду ДОЛЖНО пропускать вызов, т.к. условие строго `>`, но это никогда не подтверждено), (4) эффект `headroom_factor` по умолчанию 1.5, (5) `record_cost()` действительно обновляющий `last_call_cost_by_op` для следующего вызова `check()`. Регрессия здесь (например, случайная замена `>` на `>=`, или использование устаревшего `last`) молча снимет защиту от перерасхода или, наоборот, будет неоправданно пропускать вызовы — и ни один тест в CI об этом не узнает.

**Recommendation**: добавить `tests/unit/test_budget.py` с прямыми (без Benchmark/round_runner) тестами `BudgetGate.check()` против `InMemoryStorage`: happy path, skip-при-превышении, exact-boundary, эффект `headroom_factor`, `record_cost` roundtrip. Плюс минимум один интеграционный smoke-тест, где `Benchmark.budget_gate` реально установлен и наблюдаемо режет один из вызовов.

---

### [HIGH] runner/round_runner.py (386 строк, ядро оркестрации) не имеет ни одного прямого unit-теста

**File:Line**: `src/llm_bench/runner/round_runner.py` (весь файл); косвенно затрагивается только через `tests/integration/test_smoke_in_memory.py`, `tests/integration/test_preflight.py`, `tests/integration/test_job_app_example.py`.

**Описание**: `grep -rln "runner" tests/` показывает, что ни `round_runner.py`, ни отдельные его функции (`run_round`, `_run_pipeline`, `_build_prompt`, `_parse_safely`, `_call_llm`) не импортируются НИ ОДНИМ тестовым файлом по имени — они достижимы только транзитивно через `Benchmark.run_phase` в трёх интеграционных smoke-тестах, у которых fake-провайдеры всегда успешны и парсеры никогда не бросают исключений. В частности, следующие defensive-ветки не воспроизведены ни одним тестом (проверено через анализ `_build_prompt`/`_parser` заглушек во всех трёх интеграционных файлах — ни одна не бросает исключение и не возвращает malformed-ответ):
- `round_runner.py:182-186` — `except Exception:` вокруг `_build_prompt` (сбой построителя промпта пропускает стадию с `continue`, но не роняет пайплайн);
- `round_runner.py:336-343` (`_parse_safely`) — `except Exception:` вокруг `stage.parser(...)`, возвращает `None` при сбое парсера;
- `round_runner.py:214-234` — семафор per-op (`per_op_sems`), задаваемый через `cfg.per_op_concurrency`, ни в одном тесте не передаётся непустым;
- `round_runner.py:306-312` — circuit breaker "3+ DEAD errors -> aborting remaining stages" (счётчик `error_count`, ранний `return`) — нет теста, который бы довёл `error_count` до 3 в рамках одного пайплайна;
- `round_runner.py:202-207` — budget-gate skip-ветка (см. предыдущий finding).

**Почему это важно**: это самый большой файл в `runner/` и один из крупнейших во всём пакете; он единственное место, где реально стыкуются resume-cache, budget-gate, halving error-history и storage-запись. Баг здесь (как и найденный Critical-finding выше, который находится именно в этом файле) с высокой вероятностью останется незамеченным, потому что покрытие идёт только "снаружи" через happy-path smoke-тесты.

**Recommendation**: добавить `tests/unit/test_round_runner.py`, тестирующий `_run_pipeline`/`run_round` напрямую с управляемым fake-провайдером, который умеет: бросать исключение из парсера/prompt_builder, накапливать 3+ DEAD-ошибки подряд, срабатывать budget-gate skip, задействовать `per_op_concurrency`.

---

### [HIGH] classify_provider_error покрыт только 1 из 6 веток — при том что регрессия здесь уже случалась в проде

**File:Line**: `src/llm_bench/runner/classify.py:12-39`.

**Описание**: `classify_provider_error` — чистая, тривиально тестируемая функция без зависимостей, с 6 явными ветками (`ModelNotFound`, `ContextOverflow`, `EmptyChoices`, `JsonModeUnsupported`, `RateLimited`, `ProviderError`, plus fallback `exc_name`). Докстрока (`classify.py:14-18`) прямо говорит: *"Order matters: `ModelNotFound` runs FIRST so 405s with 'Provider returned error' wording don't get mis-classified as `ProviderError` (lesson from the live run on 2026-05-05)."* — то есть это задокументированный исторический инцидент. Тем не менее, единственное место, где эта функция вообще косвенно затрагивается — `tests/integration/test_preflight.py:52` (`behavior == "404"` → сообщение `"OpenRouter API error 404: No endpoints found for X."` → `ModelNotFound`). Ни `ContextOverflow`, ни `EmptyChoices`, ни `JsonModeUnsupported`, ни `RateLimited`, ни `ProviderError`, ни fallback-ветка, ни (самое важное) сообщение, которое одновременно содержит признаки `ModelNotFound` И `ProviderError` (чтобы проверить именно приоритет порядка веток, о котором предупреждает докстрока) — не протестированы вообще.

**Почему это важно**: результат этой функции напрямую определяет членство в `DEAD_ERROR_CLASSES`/`TRANSIENT_ERROR_CLASSES` (`halving/alive_filter.py`) и, соответственно, решение "жив ли кандидат" в ходе halving-каскада. Ошибка классификации молча меняет, какие модели выживают/выбывают — без исключений, без явного сигнала.

**Recommendation**: `tests/unit/test_classify.py` — таблица (message, expected_class) минимум по 6 веткам + отдельный тест на приоритет порядка (сообщение, содержащее оба триггера "404"/"not found" и "provider returned error", должно классифицироваться как `ModelNotFound`, не как `ProviderError`) — это прямая регрессия для инцидента, упомянутого в докстроке.

---

### [MEDIUM] FileStorage.upsert_prompts проверяет идемпотентность по неправильной таблице

**File:Line**: `src/llm_bench/storage/file.py:104-126`.

**Описание**: `upsert_prompts` должен вести отдельный, дедуплицированный по `composite_hash` журнал `_prompts.jsonl` (докстрока модуля, `file.py:7`: "one entry per (system, user) pair"). Однако проверка "уже записано ли" делается так (`file.py:108-111`):
```python
cur = await self._db.execute(
    "SELECT 1 FROM resume_cache WHERE composite_hash = ? LIMIT 1",
    (comp_h,),
)
```
`resume_cache` — это таблица **индекса результатов** (см. DDL `file.py:44-61`), а не индекс промптов. Запись в неё появляется только при успешном `record_call` с непустым, достаточно длинным ответом (`file.py:159`: `if not row.error_class and row.response and len(row.response) >= 20`). То есть проверка отвечает не на вопрос "текст этого промпта уже есть в `_prompts.jsonl`?", а на вопрос "хоть КАКОЙ-то успешный результат с этим `composite_hash` уже когда-либо записан?" — это другое утверждение.

**Подтверждено выполнением** (`repro_prompts_jsonl_bug.py`, реальный `FileStorage` на временной директории): успешный `record_call` для `composite_hash=X` (без предварительного `upsert_prompts(X)`) делает так, что последующий первый в жизни вызов `upsert_prompts` с текстом, хэширующимся в `X`, **тихо ничего не пишет** — `_prompts.jsonl` вообще не создаётся:
```
*** BUG CONFIRMED: _prompts.jsonl was never created --
    upsert_prompts silently skipped writing the prompt text ...
```

**Почему это важно / реалистичность сценария**: в штатном пути `round_runner.py:211` `upsert_prompts` всегда вызывается ДО `record_call` в рамках одного пайплайна, поэтому для *самого первого* вызова с данным `composite_hash` (по всему раунду, по всем моделям) баг не проявляется. Проявляется он, когда `BenchmarkStorage` используется не строго через `round_runner.py` (Protocol в `storage/base.py` НЕ документирует обязательный порядок "upsert_prompts всегда раньше record_call" как часть контракта) — например, кастомный консьюмер фреймворка, батч-миграция, либо гонка между конкурентными `asyncio.gather`-пайплайнами при доп. интеграциях. Влияние ограничено: `grep` подтверждает, что `_prompts.jsonl` нигде программно не читается обратно (только "human-greppable" по докстроке) — то есть это не ломает ranking/resume/halving, но ломает заявленную полноту audit-trail файла.

**Почему тесты это не поймали**: `tests/unit/test_storage_protocol.py::test_upsert_prompts_idempotent` (параметризован по `memory`+`file`, строки 105-118) проверяет только то, что *возвращаемые* хэши стабильны между двумя вызовами — но `upsert_prompts` вычисляет хэши чистой функцией (`prompt_hashes()`), независимо от какого-либо чтения хранилища, так что возвращаемое значение стабильно ВСЕГДА, даже при полностью сломанной персистентности. Тест не инспектирует реальное состояние (`_prompts.jsonl`/эквивалент), поэтому не может обнаружить этот баг в принципе — ни в `FileStorage`, ни гипотетически в любом другом бэкенде.

**Recommendation**: 1) В `file.py` вести собственный индекс промптов (например, отдельная таблица `prompts_index(composite_hash PRIMARY KEY)` в том же sqlite-файле) вместо переиспользования `resume_cache`. 2) Усилить контрактный тест: после двух вызовов `upsert_prompts` с одинаковым текстом явно проверять, что физическое хранилище (файл/таблица) содержит ровно одну запись — не только возвращаемый кортеж хэшей.

---

### [MEDIUM] load_winner_substrate: тай-брейк "latest by ts" инвертирован относительно докстроки

**File:Line**: `src/llm_bench/ranking/per_stage_winners.py:110-113`.

**Описание**: 
```python
# Latest by ts (None ts sorts last).
candidates.sort(
    key=lambda r: (r.ts is None, r.ts), reverse=True,
)
return candidates[0]
```
Изолированный repro (Python, точная копия ключа сортировки) с тремя записями — `old` (2020), `new` (2024), `none1` (`ts=None`):
```
sorted (reverse=True): [none1(None), new(2024), old(2020)]
picked (candidates[0]): none1
```
При `reverse=True` группа `ts is None` (ключ `True`) сортируется **первой** (`True > False`), то есть строка без `ts` побеждает строки с реальными датами — прямо противоположно комментарию "(None ts sorts last)".

**Почему это важно**: `load_winner_substrate` определяет, чей вывод M_X-победителя раунда используется как upstream-контекст для следующей стадии (`per_stage_winners.py:81-99` — центральный механизм "M_X promotion" каскада halving). На практике сейчас баг **латентен**: все три бэкенда (`memory.py:76-78`, `file.py:132-133`/`369-371`, `postgres.py:227`) всегда проставляют `ts`, если он `None`, в момент записи — поэтому строка с `ts=None`, прочитанная обратно из storage, практически никогда не встречается. Баг проявится, как только: (а) появится сценарий с несколькими строками для одной и той же (`stage`, `task_unit_id`, `winner_model`) комбинации, отличающимися только `thinking` (это уже возможно сегодня — `composite_hash`/PK не включают `task_unit_id`/`stage`, но `thinking` варьируется), и хотя бы одна из этих строк была вставлена напрямую в обход стандартного `record_call` (тестовый фикстур, миграция, будущий бэкенд без auto-stamping), либо (б) появится новый storage-бэкенд, не наследующий соглашение "always stamp ts".

**Почему тесты это не поймали**: `tests/unit/test_per_stage_winners.py::TestLoadWinnerSubstrate::test_returns_winner_row` (строки 107-127) заводит ровно ОДНУ подходящую строку на кандидата — значит `.sort()` вызывается на списке из 1 элемента, и ветка сравнения никогда фактически не исполняется content-значимо. Тест-сьют вообще не содержит сценария с >1 строкой на (`winner_model`, `stage`, `task_unit_id`).

**Recommendation**: исправить ключ сортировки (например, `key=lambda r: (r.ts is not None, r.ts or datetime.min.replace(tzinfo=UTC))`, без `reverse`, взять `candidates[-1]`), либо использовать `max(candidates, key=..., default=None)` с явной семантикой "None sorts last". Добавить тест минимум с двумя строками на одного `winner_model` — одна старее, одна новее — и убедиться, что возвращается более новая; отдельный тест на смешение `ts=None` + реальных `ts`.

---

### [MEDIUM] cost/openrouter.py — конкретный адаптер каталога OpenRouter не покрыт ни одним тестом

**File:Line**: `src/llm_bench/cost/openrouter.py` (весь файл, 204 строки).

**Описание**: `grep -rln "openrouter" tests/` находит только строковые литералы `provider="openrouter"` внутри фикстур `RunRow` в `test_per_stage_winners.py`/`test_ranker.py` — ни одного реального импорта из `llm_bench.cost.openrouter`. Файл содержит несколько нетривиальных, легко тестируемых в изоляции (без сети) функций с реальными edge cases по разбору "грязных"/частичных данных из внешнего API:
- `_normalise_uptime` (`openrouter.py:41-52`) — OpenRouter иногда отдаёт uptime как 0-100%, иногда как 0-1 долю; нормализация через `v / 100.0 if v > 1.0 else v` никогда не протестирована на граничных значениях (например, `v == 1.0` — валидная доля 100% или уже нормализованная единица?).
- `_per_token_to_per_m` (`openrouter.py:22-38`) — явно описанная защита от meta-router-заглушек с ценой `-1000000` (`v < 0: return None`) не протестирована.
- `_pick_best_upstream` (`openrouter.py:55-85`) — сортировка апстримов по статусу, где статус может прийти либо строкой (`"live"/"degraded"`), либо числом (`0`/отрицательное) — оба варианта веток `status_rank` не протестированы.
- `_entry_from_or_row` (`openrouter.py:88-154`) — фильтрация `:free`-тира, реконсиляция двух разных полей `context_length` (top-level vs `top_provider`) через `min()`, fallback `max_completion_tokens` — ни одна из этих веток не протестирована.

**Почему это важно**: это единственное место в репозитории, где реально парсятся "сырые" (потенциально неполные/несогласованные) данные внешнего API — ровно та категория "malformed/partial response" edge cases, которую задание просило проверить. Раз framework уже добавляет специальную защиту от `-1000000`-заглушек и смешанных типов статуса (что подразумевает — авторы уже сталкивались с этими случаями в реальных данных OpenRouter), отсутствие тестов на эти конкретные, уже известные пограничные случаи — заметный пробел.

**Recommendation**: `tests/unit/test_openrouter_catalogue.py` с табличными тестами на `_entry_from_or_row` против сконструированных "грязных" OR-строк (недостающий `pricing`, `-1000000` цена, `uptime=99.8` vs `uptime=0.998`, статус как int vs str, отсутствующий `top_provider`, `:free`-тир).

---

### [MEDIUM] CostFilter.moderated_penalty — мёртвое поле, задокументированное как реально работающее

**File:Line**: `src/llm_bench/cost/filter.py:41-43`.

**Описание**: 
```python
moderated_penalty: float = 1.15
"""Multiplier applied to the cost rank of moderated providers when
``exclude_moderated=False``. Higher = moderated peers lose ties."""
```
`grep -rn "moderated_penalty"` по всему репозиторию находит ровно одно вхождение — само определение поля. Оно нигде не читается: ни в `CostFilter.keep()` (`filter.py:50-76`, единственный метод класса), ни в `estimator.py`'s `_sort_key`/`estimate_top_n_by_cost`. Функциональность, описанную в докстроке ("moderated-провайдеры проигрывают тай-брейки"), физически невозможно вызвать.

**Почему тесты это не поймали**: `tests/unit/test_cost.py::TestCostFilter::test_exclude_moderated` (строки 127-130) тестирует только жёсткий фильтр `exclude_moderated=True`, но не тай-брейк-штраф при `exclude_moderated=False` — потому что тестировать нечего, штрафа не существует в коде.

**Recommendation**: либо реализовать штраф в `_sort_key`/`keep()` (и покрыть тестом), либо удалить поле и его докстроку как обещающие несуществующее поведение — текущее состояние вводит в заблуждение любого, кто настраивает `CostFilter(moderated_penalty=...)`, ожидая эффекта.

---

### [MEDIUM] tests/property/ — пустая директория; hypothesis объявлен, но не используется вообще

**File:Line**: `tests/property/__init__.py` (0 байт, единственный файл в каталоге), `pyproject.toml:59`.

**Описание**: `find tests/property -type f` находит только `__init__.py` размером 0 байт. `git log --all -- tests/property` показывает, что каталог не менялся с первого коммита ("Phase A: repo skeleton"). `grep -rn "hypothesis"` по всем `.py`/`.toml`/`.cfg`/`.ini` файлам находит `hypothesis` только в объявлении dev-зависимости (`pyproject.toml:59,219,224`) — ни одного `import hypothesis`/`from hypothesis import ...` нигде в репозитории.

**Почему это важно**: задание аудита прямо описывает `tests/property/` как "uses hypothesis" — но по факту это пустой скаффолд. Framework содержит именно тот класс логики, где property-based тестирование особенно ценно и которое сейчас проверяется только точечными example-based тестами: инварианты halving (`Halving.promote` никогда не должен продвигать больше кандидатов, чем `target_size`; `candidates_out` ∩ `eliminated` всегда пусто; сумма `len(candidates_out) + len(eliminated) == len(candidates_in)` с точностью до coverage-исключённых), round-trip хэширования (`composite_hash(a,b) == composite_hash(a,b)` для произвольных unicode-строк), сериализация `WinnerSet` через `FileStorage`/`PostgresStorage` (round-trip для произвольных dict-содержимых).

**Recommendation**: либо написать реальные property-тесты (минимум для `halving/driver.py::Halving.promote` — инвариант "никогда не продвигает больше, чем `target_size`", и для `core/hashing.py` — детерминированность/отсутствие коллизий на разумных входах), либо убрать `hypothesis` из dev-зависимостей и удалить пустой каталог, чтобы не создавать ложное впечатление покрытия.

---

### [MEDIUM] ResumeCache не имеет ни одного прямого unit-теста

**File:Line**: `src/llm_bench/runner/resume.py` (весь файл, 88 строк).

**Описание**: `grep -rln "ResumeCache" tests/` не находит ни одного результата — класс нигде не импортируется напрямую в тестах, только используется транзитивно внутри `Benchmark`/`round_runner`. Методы `populate_from_storage` (строки 45-60), `get` (62-73), `put` (75-84), `__len__` (86-87) не имеют изолированных тестов — например, нет теста, что `put()` после `populate_from_storage()` действительно делает следующий `get()` попаданием без повторного обращения к storage, или что `min_response_len`/`only_successful` параметры `populate_from_storage` реально фильтруют так, как задокументировано.

**Recommendation**: `tests/unit/test_resume_cache.py` — прямые тесты `populate_from_storage`/`get`/`put`/`__len__` против `InMemoryStorage`, включая проверку, что `put()` не требует повторного `populate_from_storage()`.

---

### [MEDIUM] mad_bootstrap_prune: paired-bootstrap ветка (penalty за дисперсию/стоимость) не покрыта тестами

**File:Line**: `src/llm_bench/halving/pruner.py:83-178` (variance/cost penalty + paired-bootstrap блок).

**Описание**: `tests/unit/test_halving.py::TestMADBootstrapPrune` содержит 6 тестов, но ВСЕ они вызывают `mad_bootstrap_prune(scores)` только с позиционным словарём `scores`, ни разу не передавая `per_task_unit_scores` или `eff_cost`. Это означает, что следующая логика никогда не исполняется под тестом:
- Variance penalty по коэффициенту вариации (`pruner.py:85-98`);
- Cost penalty относительно самой дешёвой модели (`pruner.py:100-112`);
- Paired-bootstrap ветка `use_paired` (`pruner.py:153-178`) — вложенный цикл ресэмплинга по task-unit'ам, вычисление `peer_means_eff`/`cand_mean_eff`/`lb_deviation` — примерно 25 строк статистического кода, реализующих именно ту "apples-to-apples"-семантику, о которой явно говорит докстрока модуля (`pruner.py:1-30`) как о ключевом свойстве корректности Sequential Halving.

**Почему это важно**: это самая математически тонкая часть репозитория — ошибка в индексации (`idx[i]`), в знаке penalty, или в позиции `int(0.025 * n_bootstrap)` при вычислении нижней доверительной границы, молча изменит, какие кандидаты статистически "выживают" каждый раунд, без единого сигнала об ошибке.

**Recommendation**: минимум 2-3 теста с `per_task_unit_scores`, покрывающие: (1) явный случай, где variance penalty меняет исход по сравнению с агрегированным средним; (2) явный случай, где cost penalty вытесняет дорогую модель, балансирующую на грани порога; (3) базовая санитарная проверка paired-пути (одинаковый входной набор данных с `per_task_unit_scores`, дающий тот же список выживших, что и агрегированный путь, когда все task-unit'ы идентичны).

---

### [LOW] Офлайн-сьют сейчас не полностью зелёный

**File:Line**: `tests/test_meta/test_code_audit_baseline.py:31-36`, `src/llm_bench/cost/estimator.py:161-203`.

См. раздел "Резюме прогона офлайн-сьюта" выше — `docstring_args_incomplete` на `cost/estimator.py:161` (параметр `refresh` реально отсутствует в `Args:`, проверено чтением кода). Зафиксировано как наблюдение по прямому требованию задания; вероятная причина — опережающий sibling-чекаут `py-ci-shared`/`pyutilz` в этом окружении, а не обязательно текущее состояние настоящего CI.

**Recommendation**: (а) исправить докстроку `estimate_top_n_by_cost`, добавив `refresh` в `Args:` — тривиально; (b) если находка подтвердится и в реальном CI — обновить baseline через `--refresh-code-audit-baseline` после ревью, как и предписывает механизм.

---

### [LOW] allowed_validator_pairs (N>=4): анти-симметричная логика не тестируется напрямую

**File:Line**: `src/llm_bench/halving/pairing.py:123-150`.

**Описание**: Комментарий в коде (`pairing.py:131-132`) описывает цель — избегать одновременного появления и `(A,B)`, и `(B,A)` в списке пар через `used_validators_for`. `tests/unit/test_halving.py::TestValidatorPairs::test_n_ge_4_no_self` (строки 231-235) проверяет только `producer != validator` для каждой пары, но не то, что анти-симметричная эвристика реально работает (например, генерируя много кандидатов и явно проверяя отсутствие одновременных `(A,B)`/`(B,A)` в результирующем списке).

**Recommendation**: добавить тест, который для N=6-8 кандидатов явно проверяет отсутствие зеркальных пар.

---

### [LOW] Halving.promote() никогда не тестируется с единственным выжившим кандидатом

**File:Line**: `src/llm_bench/halving/driver.py` (весь `promote`), `tests/unit/test_halving.py::TestHalvingPromote`.

**Описание**: Все тесты `TestHalvingPromote` используют `scores` с 2+ моделями. `mad_bootstrap_prune` имеет отдельный явный тест на `len(scores)==1` (`test_single`), но `Halving.promote` со словарём из одной модели (`scores={"only": 0.5}`) не тестируется — то есть комбинация "одна модель + coverage-gate + dead-filter + specialty" на этом крайнем случае не подтверждена.

**Recommendation**: добавить `test_single_survivor` в `TestHalvingPromote`.

---

### [LOW] Расхождение в точности суммирования между InMemoryStorage/FileStorage и PostgresStorage не покрыто

**File:Line**: `src/llm_bench/storage/memory.py:141-145` (`sum(...)` на чистых `float`), `src/llm_bench/storage/postgres.py:336-347` (`SUM(effective_cost_usd)` — `NUMERIC` на стороне сервера, один `float()`-каст в конце).

**Описание**: Не баг, а честно отмеченное потенциальное расхождение "fake vs real backend": `InMemoryStorage`/`FileStorage` накапливают сумму через инкрементальное сложение Python `float` (накопление ошибки округления на большом числе строк), `PostgresStorage` делает это через SQL `NUMERIC`-агрегацию (точная десятичная арифметика) с единственным приведением к `float` в самом конце. Сейчас незаметно, потому что все тесты сравнения используют `pytest.approx(...)` с невысокой точностью (`test_storage_protocol.py:207-211`, `:224-228`).

**Recommendation**: не требует действия сейчас; стоит иметь в виду при написании precision-чувствительных тестов на больших прогонах (сотни/тысячи строк).

---

### [INFO] code_audit_baseline — реально работающий "трещоточный" gate, не rubber stamp; но 17 находок прощены навсегда

**File:Line**: `tests/test_meta/_code_audit_baseline.json` (17 записей), `tests/test_meta/test_code_audit_baseline.py`.

**Описание**: История файла в git (`git log --follow`) показывает только 2 коммита: `05ad2f5` (первичное создание бейзлайна) и `ebc1c78` ("style: clear pre-existing filtered-black debt" — по коммит-мессаджу: "Refreshed the code-audit baseline since reformatting shifted some findings' line numbers (0 new findings, 15 drained/relocated)"). Диф этого коммита подтверждён построчно — изменились только номера строк, ни одна запись не добавлена и не удалена по содержанию (те же 17 `check::file` пар, только сдвинутые line numbers). **Признаков паттерна "молча расширяем бейзлайн, чтобы не чинить новые находки" не обнаружено** — при фактическом прогоне сьюта в этом окружении (см. раздел выше) новая находка `docstring_args_incomplete` действительно **упала тестом**, а не была тихо добавлена в бейзлайн — это прямое доказательство, что механизм — реальный regression gate, не декорация.

При этом стоит явно зафиксировать: 17 baselined находок (`default_via_or` x9, `log_only_except` x4, `broad_except_swallow` x1 плюс дубли) прощены **навсегда**, без срока пересмотра/тикета — это осознанный trade-off ("baseline ratchet" паттерн), не ошибка, но человеку, полагающемуся на "CI зелёный => нет known static-analysis issues", стоит понимать, что это не так.

**Recommendation**: без действия по существу; можно рассмотреть периодический ручной пересмотр бейзлайна (например, раз в квартал) как процессную практику, а не техническое требование к этому аудиту.

---

### [INFO] `-p no:randomly` — не задокументированный воркэраунд конкретного бага, а унаследованный шаблон

**File:Line**: `pyproject.toml:82`.

**Описание**: Флаг присутствует с самого первого коммита (`97026ee`, "Phase A: repo skeleton") — коммит-мессадж описывает `pyproject.toml` как "Bootstrap files (copied/adapted from pyutilz)". `pytest-randomly` установлен в этом окружении (v3.16.0, вероятно транзитивная dev-зависимость через `py-ci-shared`/`pyutilz`), то есть флаг — не no-op, он реально отключает плагин, который иначе рандомизировал бы порядок тестов. Никаких следов в git-истории или комментариях о том, что это воркэраунд под конкретный найденный баг межтестовой изоляции в llm_bench — похоже на осознанный детерминированный дефолт, унаследованный из шаблона pyutilz.

**Проверка изоляции**: явных `module`-level мутируемых глобалов или `class`-level fixture с мутируемыми дефолтами, которые могли бы вызывать order-dependent flakiness, при чтении всех unit/integration/test_meta файлов не найдено — каждый тест создаёт свежие `InMemoryStorage()`/`FileStorage(tmp_path/...)`/`Halving()` экземпляры без общего состояния между тестами.

**Recommendation**: без действия; можно оставить как есть или прогнать сьют разово с `-p randomly` (без `no:`) для дополнительной проверки отсутствия скрытой межтестовой зависимости — на момент этого аудита рисков не выявлено.

---

### [INFO] examples/job_app_cover_letter — интеграционный тест честный, заметных пробелов не найдено

**File:Line**: `tests/integration/test_job_app_example.py`, `examples/job_app_cover_letter/{run,job_pool,stages}.py`.

Прочитаны `run.py`/`job_pool.py` целиком — `test_job_app_dry_run_completes` реально гоняет полный `Benchmark.run_phase` через `FileStorage` (не `InMemoryStorage`, в отличие от прочих smoke-тестов — хорошее разнообразие бэкендов в сьюте) с детерминированным `_FakeProvider`, различающим ответы по системному промпту (валидатор vs. обычная стадия) — правдоподобная имитация, не тривиальный stub. Отдельные тесты на топологию графа стадий и загрузку CSV-пула — адекватны.

## Итог по количеству находок

- Critical: 1
- High: 4
- Medium: 7
- Low: 4
- Info: 3
