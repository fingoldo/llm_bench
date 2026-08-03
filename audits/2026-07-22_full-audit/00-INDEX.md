# Сводный индекс аудита llm_bench (2026-07-22)

## Обзор

10 независимых агентов провели построчный аудит репозитория `llm_bench` (Python-фреймворк для
сравнительного бенчмаркинга LLM: cost-rank discovery, Sequential Halving, per-stage winner
promotion, gold-anchored ranking, cross-tag resume cache, три storage-бэкенда — in-memory/file/
Postgres) по десяти категориям: (01) Архитектура и дизайн, (02) Качество кода и Python-практики,
(03) Корректность и граничные случаи, (04) Асинхронность и конкурентность, (05) Качество и
покрытие тестов, (06) Вычислительная эффективность, (07) Безопасность, (08) SQL и Postgres storage
backend, (09) HTTP/API-клиенты и LLM-специфичные практики, (10) Пакетирование, документация, CLI и
CI. Каждый агент читал соответствующий срез кода целиком (не выборочно), запускал офлайн-сьют
тестов и статические инструменты (ruff/mypy/bandit/interrogate/deptry), а по ключевым находкам —
самостоятельно написанные дискриминирующие repro-скрипты против реального кода. Ниже — faithful
агрегация всех находок всех десяти отчётов (162 находки, ни одна не объединена и не отброшена),
консолидированная таблица по убыванию критичности, кросс-категорийные наблюдения (находки,
независимо подтверждённые 2+ отчётами) и пробелы аудита, которые стоит проверить человеку отдельно.

## Сводная таблица находок (Critical → High → Medium → Low → Info)

| Severity | Категория | Файл:строка | Описание | Отчёт |
|---|---|---|---|---|
| Critical | Архитектура | `runner/round_runner.py` (`_run_pipeline`, 168-312) + `ranking/per_stage_winners.py:81-114` | Per-stage winner promotion — заявленная ключевая фича — не подключена к раннеру; `load_winner_substrate`/`load_winners` никогда не вызываются | [01](01-architecture-design.md#finding-1-critical-per-stage-winner-promotion-мёртвый-код-никогда-не-подключён-к-раннеру) |
| Critical | Архитектура | `storage/memory.py:68-80`, `storage/postgres.py:225-256`, `storage/base.py:11-15` | `record_call` не может перезаписать провалившуюся строку успешным повтором — resume cache и ranking навсегда "залипают" на первой ошибке | [01](01-architecture-design.md#finding-2-critical-record_call-не-умеет-перезаписать-провалившуюся-строку-успешным-повтором-inmemorystorage-postgresstorage) |
| Critical | Качество кода | `runner/round_runner.py:168-313`, `ranking/per_stage_winners.py:81-114` | Per-stage «M_X»-substrate promotion не подключена к раннеру — каждый кандидат использует свой, а не winner-substrate, вывод предыдущего этапа | [02](02-code-quality-python-practices.md#critical-per-stage-«m_x»-substrate-promotion-не-подключена-к-раннеру) |
| Critical | Качество кода | `runner/round_runner.py:168-313`, `halving/pairing.py:1-24,97-185` | Layer-1 "hard"-правило независимости валидатора не применяется — раннер не смотрит на `Stage.is_validator` вообще | [02](02-code-quality-python-practices.md#critical-layer-1-независимость-валидатора-«модель-не-судит-сама-себя»-не-применяется) |
| Critical | Корректность | `runner/round_runner.py` (`_run_pipeline`, нет упоминания `is_validator`) | Layer-1 "no self-judgment" для validator-стадий никогда не применяется — каждый кандидат валидирует сам себя | [03](03-correctness-edge-cases.md#critical-validator-независимость-layer-1-no-self-judgment-никогда-не-применяется) |
| Critical | Корректность | `runner/round_runner.py:160-164` vs `ranking/per_stage_winners.py:81-114` | M_X promotion write-only: `persist_winners` вызывается, `load_winner_substrate` — никогда | [03](03-correctness-edge-cases.md#critical-m_x-per-stage-winner-substrate-promotion-write-only) |
| Critical | Корректность | `runner/round_runner.py:194-199,303` + `halving/driver.py:107-118` | Resume/retry ломает coverage-gate: попадания в resume-cache не увеличивают `n_stages_attempted` → полностью закэшированный раунд теряет всех кандидатов (подтверждено repro) | [03](03-correctness-edge-cases.md#critical-resumeretry-ломает-coverage-gate-закэшированный-раунд-теряет-всех-кандидатов) |
| Critical | Корректность | `storage/memory.py:68-80`, `postgres.py:225-256`, `file.py:236-269` | Успешный повтор вызова после провала с тем же PK молча теряется/затеняется старой неудачной строкой (подтверждено repro, 2 варианта) | [03](03-correctness-edge-cases.md#critical-успешный-повтор-вызова-молча-теряетсязатеняется-старой-неудачной-записью-с-тем-же-pk) |
| Critical | Асинхронность | `runner/round_runner.py:109-117,284` | Широкий `except Exception` в `_one_pair` тихо проглатывает сбои `storage.record_call`/`resume_cache.put` ПОСЛЕ успешного платного LLM-вызова | [04](04-async-concurrency.md#critical-широкое-except-exception-в-_one_pair-тихо-теряет-уже-оплаченные-результаты-и-может-обнулить-весь-раунд-без-сигнала-наверх) |
| Critical | Тесты | `runner/round_runner.py:189-198` (vs `:303`) | Резюмированный (100% cache-hit) раунд обнуляет весь пул кандидатов через coverage-gate (подтверждено repro) | [05](05-test-quality-coverage.md#critical-round_runnerpy-resume-after-interrupt-обнуляет-весь-пул-кандидатов-через-coverage-gate) |
| Critical | Безопасность | `storage/file.py:134-136,239,303,326,350-353` | Path traversal / absolute-path escape через `experiment_tag` в FileStorage — подтверждён arbitrary write + arbitrary recursive delete (`shutil.rmtree` вне `root`) | [07](07-security.md#finding-1-critical-path-traversal-absolute-path-escape-через-experiment_tag-в-filestorage-arbitrary-write-arbitrary-recursive-delete) |
| Critical | SQL/Postgres | `storage/postgres.py:275-286` | `prefetch_resume_cache` вызывает `conn.cursor()` без `conn.transaction()` — гарантированно падает с `NoActiveSQLTransactionError` на реальном Postgres | [08](08-sql-postgres-storage.md#finding-1-prefetch_resume_cache-гарантированно-падает-на-реальном-postgres-critical) |
| Critical | SQL/Postgres | `storage/postgres.py:229-256` | `record_call`'s `ON CONFLICT DO NOTHING` навсегда теряет успешный повтор ранее провалившегося вызова | [08](08-sql-postgres-storage.md#finding-2-on-conflict-do-nothing-в-record_call-навсегда-хоронит-успешный-повтор-ранее-провалившегося-вызова-critical) |
| Critical | HTTP/API/LLM | `runner/round_runner.py:82-165,168-313`, `ranking/per_stage_winners.py:1-18,81-114` | Механизм M_X substrate объявлен как ключевая фича, но никогда не читается обратно | [09](09-http-api-llm-integration.md#1-critical-per-stage-winner-m_x-substrate-promotion-объявлен-но-никогда-не-выполняется) |
| Critical | HTTP/API/LLM | `core/types.py:79-89`, `stage/base.py:46-57`, `halving/pairing.py` | Cross-family validator independence никогда не применяется; `PromptBuilder` Protocol не даёт данных для её реализации даже консьюмером | [09](09-http-api-llm-integration.md#2-critical-cross-family-validator-independence-is_validator-никогда-не-применяется-protocol-не-даёт-для-этого-данных) |
| Critical | HTTP/API/LLM | `runner/round_runner.py:236-298`, `resume.py:75-84`, `core/types.py:228`, `halving/assignment.py:38-66` | Отказ парсера/модели/обрезанный ответ без HTTP-исключения молча трактуется как успех и кешируется навсегда (в т.ч. tag-agnostic) | [09](09-http-api-llm-integration.md#3-critical-отказ-парсинга-отказ-модели-обрезанный-ответ-без-http-исключения-тихий-успех-кешируется-навсегда) |
| High | Архитектура | `storage/postgres.py:137-172` + 20 f-string SQL-сайтов | `schema_name` подставляется в SQL через f-string без валидации/allowlist, вопреки заявленной в docstring безопасности | [01](01-architecture-design.md#finding-3-high-postgresstorageschema_name-sql-injection-surface-без-валидации-несмотря-на-заявленную-в-docstring-безопасность) |
| High | Архитектура | `storage/file.py:130-176` vs `236-269` | FileStorage: resume-кэш и `query_rows`/ranking расходятся МЕЖДУ СОБОЙ после retry-after-failure и оба расходятся с Postgres/InMemory | [01](01-architecture-design.md#finding-4-high-filestorage-кэш-и-аналитика-расходятся-между-собой-после-retry-after-failure-и-оба-расходятся-с-postgresinmemory) |
| High | Архитектура | `runner/round_runner.py:191,254,297,360` | Хардкод `"openrouter"` в generic-раннере ломает заявленную расширяемость через `provider_factory` | [01](01-architecture-design.md#finding-5-high-хардкод-openrouter-в-generic-раннере-ломает-заявленную-расширяемость-через-provider_factory) |
| High | Качество кода | `halving/driver.py:130`, `halving/pruner.py:41-193` | `mad_bootstrap_prune`'s cost/variance-penalty и paired-bootstrap никогда не активируются | [02](02-code-quality-python-practices.md#high-mad_bootstrap_prunes-costvariance-penalty-и-paired-bootstrap-никогда-не-активируются) |
| High | Качество кода | `runner/round_runner.py:129-152`, `core/scoring.py:10-44` | Latency-aware Pareto tiebreak используется при выборе winner'а этапа, но не при halving cut | [02](02-code-quality-python-practices.md#high-latency-aware-tiebreak-учитывается-при-выборе-winnerа-этапа-но-не-при-halving-cut) |
| High | Качество кода | `storage/*.py`, `ranking/ranker.py:66-70` | Предикат «строка успешна» продублирован в 8 местах в 4 файлах с "магическим" порогом 20 | [02](02-code-quality-python-practices.md#high-предикат-«строка-успешна»-продублирован-в-8-местах-с-«магическим»-порогом-20) |
| High | Качество кода | `stage/base.py:64-66`, `core/types.py:228`, `round_runner.py:252-283` | `RunRow.parse_failure_prefix`/`logs`/... прошиты через все storage-бэкенды, но никогда не заполняются; docstring лжёт | [02](02-code-quality-python-practices.md#high-диагностические-поля-runrow-полностью-прошиты-в-storage-но-никогда-не-заполняются-docstring-лжёт) |
| High | Качество кода | `storage/postgres.py:39-167,203-441` | `schema_name` f-string-интерполируется в 15+ SQL-запросов без валидации на входе конструктора | [02](02-code-quality-python-practices.md#high-postgresstorageschema_name-динамический-sql-без-валидации-обоснование-безопасности-не-подкреплено-кодом) |
| High | Качество кода | `round_runner.py:116-117,184-185,341-343` | «Log-only» broad-except в hot path уже задетектированы собственным сканером, но лишь занесены в baseline, не исправлены | [02](02-code-quality-python-practices.md#high-проглоченные-исключения-в-hot-path-уже-найдены-собственным-сканером-но-не-исправлены) |
| High | Корректность | `runner/budget.py:49-73`, `round_runner.py:104-119` | У BudgetGate нет резервирования in-flight трат — конкурентные вызовы одного `op` коллективно превышают cap (repro: $4.50 факт против cap $1.00) | [03](03-correctness-edge-cases.md#high-budgetgate-не-резервирует-«in-flight»-расходы-конкурентные-вызовы-одного-op-коллективно-превышают-cap) |
| High | Корректность | `ranking/per_stage_winners.py:58-60` vs `ranker.py:248-257` | `select_per_stage_winners` выбирает победителя по чистому score, игнорируя `cost_tiebreak_key` — расходится со `StageRanking.winner` (подтверждено repro) | [03](03-correctness-edge-cases.md#high-select_per_stage_winners-расходится-с-stagerankingwinner-при-равенстве-очков-игнорирует-cost_tiebreak_key) |
| High | Корректность | `round_runner.py:151` vs `194-199,206-207` | Знаменатель coverage-gate (`total_stages`) не масштабируется на `units_per_arm` — гейт ловит только полный отказ, не частичный | [03](03-correctness-edge-cases.md#high-знаменатель-coverage-gate-не-масштабируется-на-units_per_arm) |
| High | Корректность | `core/types.py:91-94` vs `budget.py:60-73` | `Stage.budget_per_call` документирован как основа прогноза бюджета, но нигде не читается | [03](03-correctness-edge-cases.md#high-stagebudget_per_call-документирован-но-нигде-не-используется) |
| High | Асинхронность | `storage/file.py:18-19,79-87,124-125,153-154` | Заявленная кросс-процессная безопасность ("OS-locked via SQLite WAL mode") не обеспечена: нет `PRAGMA busy_timeout`, нет retry на `database is locked` | [04](04-async-concurrency.md#high-заявленная-кросс-процессная-безопасность-filestorage-os-locked-via-sqlite-wal-mode-не-обеспечивается-кодом) |
| High | Асинхронность | `runner/budget.py:49-73` | `BudgetGate.check()` — TOCTOU race: N параллельных вызовов одного `op` могут одновременно пройти проверку и совместно превысить `cap_usd` | [04](04-async-concurrency.md#high-budgetgatecheck-toctou-race-конкурентные-вызовы-одного-op-могут-совместно-превысить-cap_usd) |
| High | Асинхронность | `pool/base.py:23-25`, `benchmark.py:239,299-304` | `TaskPool.sample()` документирован как async-aware, но `_sample_units()` синхронный и никогда не `await`-ит результат | [04](04-async-concurrency.md#high-taskpoolsample-документирован-как-async-aware-но-реализация-вызова-синхронная-и-никогда-не-await-ит-результат) |
| High | Асинхронность | `storage/file.py:236-269`, `budget.py:63` | `query_rows`/`query_spend_by_op` блокирующе читают весь `results.jsonl` под общим `asyncio.Lock`, замораживая event loop; дёргается на каждый stage-call при `BudgetGate` | [04](04-async-concurrency.md#high-filestoragequery_rowsquery_spend_by_op-блокирующе-сканируют-весь-resultsjsonl-под-общим-asynciolock-замораживая-весь-event-loop-вызывается-на-каждый-stage-call-при-включённом-budgetgate) |
| High | Тесты | `test_storage_protocol.py` + `postgres.py` | PostgresStorage — production-бэкенд с самооправданным `# nosec B608` — не покрыт вообще ни одним тестом | [05](05-test-quality-coverage.md#high-postgresstorage-не-покрыт-вообще-ни-одним-тестом-self-graded-nosec-b608-не-подкреплён-валидацией-schema_name) |
| High | Тесты | `runner/budget.py:26-79` | BudgetGate (единственный защитный механизм бюджета) не покрыт вообще ни одним тестом | [05](05-test-quality-coverage.md#high-budgetgate-защитный-механизм-бюджета-не-покрыт-вообще-ни-одним-тестом) |
| High | Тесты | `runner/round_runner.py` (весь файл, 386 строк) | Ядро оркестрации раунда не имеет ни одного прямого unit-теста; несколько defensive-веток недостижимы ни одним тестом | [05](05-test-quality-coverage.md#high-runnerround_runnerpy-386-строк-ядро-оркестрации-не-имеет-ни-одного-прямого-unit-теста) |
| High | Тесты | `runner/classify.py:12-39` | `classify_provider_error` покрыт лишь 1 из 6 веток, несмотря на задокументированный исторический инцидент неправильной классификации | [05](05-test-quality-coverage.md#high-classify_provider_error-покрыт-только-1-из-6-веток-при-том-что-регрессия-здесь-уже-случалась-в-проде) |
| High | Производительность | `round_runner.py:122-128`, `benchmark.py:288-295`, `ranker.py:156-165` | `compute_ranking` пересчитывает ранжирование по ВСЕМ накопленным строкам на каждом раунде + ещё раз в конце (~3.8x избыточных проходов, измерено) | [06](06-performance-efficiency.md#1-high-compute_ranking-пересчитывает-ранжирование-по-всем-накопленным-строкам-на-каждом-раунде-ещё-раз-в-конце) |
| High | Производительность | `pruner.py:159-192`, `driver.py:130` | `mad_bootstrap_prune` пересчитывает один и тот же bootstrap-ресэмпл заново для каждого кандидата — измерено 986мс при n=52, 6.4с при n=100 | [06](06-performance-efficiency.md#2-high-mad_bootstrap_prune-пересчёт-одного-и-того-же-bootstrap-ресэмпла-отдельно-на-каждого-кандидата) |
| High | Производительность | `budget.py:49-73`, `round_runner.py:202-207` | `BudgetGate.check()` перед каждым вызовом стадии заново агрегирует весь потраченный бюджет из storage — измерено 0.17→119мс линейный рост | [06](06-performance-efficiency.md#3-high-budgetgatecheck-заново-агрегирует-весь-потраченный-бюджет-из-storage-перед-каждым-вызовом-стадии) |
| High | Производительность | `postgres.py:108-110` vs `260-287` | `prefetch_resume_cache()` — полный скан всей исторической (across all tags) таблицы, ни один индекс не покрывает условия WHERE | [06](06-performance-efficiency.md#4-high-postgresstorageprefetch_resume_cache-полный-скан-без-поддерживающего-индекса-растущий-без-ограничения-по-мере-накопления-истории) |
| High | Безопасность | `postgres.py:39-52,155-186` и все `# nosec` сайты | `schema_name` f-string-подставляется без экранирования/allowlist; DDL идёт через simple query protocol, поддерживающий multi-statement stacking | [07](07-security.md#finding-2-high-schema_name-в-postgresstorage-неэкранированная-интерполяция-идентификатора-без-какой-либо-валидации-в-коде) |
| High | SQL/Postgres | `postgres.py:179-183` | Нет `command_timeout`/`timeout` при создании пула — зависший запрос может держать соединение вечно, пул исчерпывается без самовосстановления | [08](08-sql-postgres-storage.md#finding-3-нет-commandacquire-timeout-на-пуле-соединений-high) |
| High | SQL/Postgres | `postgres.py:39-120`; `pyproject.toml:47-50,216-218` | Нет пути миграции (только `CREATE ... IF NOT EXISTS`); `alembic` заявлен как зависимость, но не используется — противоречит собственному докстрингу | [08](08-sql-postgres-storage.md#finding-4-нет-пути-миграции-схемы-заявленная-alembic-зависимость-нигде-не-используется-и-противоречит-докстрингу-файла-high) |
| High | SQL/Postgres | `postgres.py:155-167, 203-437` | `schema_name` не валидируется в конструкторе, но f-string-интерполируется в 13 SQL-местах (SQL-специфичный разбор) | [08](08-sql-postgres-storage.md#finding-5-schema_name-не-валидируется-перед-f-string-интерполяцией-в-sql-high) |
| High | SQL/Postgres | `postgres.py:260-287` | `prefetch_resume_cache` не имеет поддерживающего индекса и не ограничивает выборку — full seq scan + безлимитная загрузка `response` в память | [08](08-sql-postgres-storage.md#finding-6-prefetch_resume_cache-нет-индекса-под-фильтр-нет-ограничения-объёма-full-scan-неограниченная-загрузка-в-память-high) |
| High | SQL/Postgres | `postgres.py:425-441` | Два `DELETE` в `delete_experiment` не обёрнуты в общую транзакцию — сбой между ними оставляет осиротевшие `benchmark_winners` | [08](08-sql-postgres-storage.md#finding-7-delete_experiment-не-атомарен-между-двумя-delete-high) |
| High | HTTP/API/LLM | `budget.py:49-73`, `round_runner.py:201-207` | TOCTOU-гонка в `BudgetGate.check()` под реальной конкурентностью — бюджет может быть превышен на порядок конкурентности | [09](09-http-api-llm-integration.md#4-high-budgetgatecheck-toctou-гонка-под-реальной-конкурентностью) |
| High | HTTP/API/LLM | `core/types.py:91-94`, `budget.py:49-73`, `round_runner.py:202-206` | `Stage.budget_per_call` документирован как вход в проекцию бюджет-гейта, но нигде не читается — гейт хардкодит $0.01 для первого вызова | [09](09-http-api-llm-integration.md#5-high-stagebudget_per_call-документирован-но-никогда-не-используется-гейтом) |
| High | Пакетирование/CI | `pyproject.toml:70`, `cli/__init__.py:1` | Консольный скрипт `llm-bench` объявлен, но `llm_bench.cli.main` физически не существует — падает `ModuleNotFoundError` (проверено эмпирически) | [10](10-packaging-docs-cli-ci.md#1-high-консольный-скрипт-llm-bench-объявлен-в-pyprojecttoml-но-не-существует-падает-при-каждом-запуске) |
| High | Пакетирование/CI | `README.md:38-57` | Quick Start не запускается «как есть» — два независимых `TypeError` (нет `op=` у второго `Stage`, нет `candidates=` у `run_phase`) | [10](10-packaging-docs-cli-ci.md#2-high-пример-quick-start-в-readmemd-не-запускается-«как-есть»-два-независимых-typeerror) |
| High | Пакетирование/CI | `.github/workflows/ci.yml:29-47`, `test_storage_protocol.py:33-53` | PostgresStorage не имеет вообще никакого тестового покрытия; Postgres-контейнер в CI поднимается впустую | [10](10-packaging-docs-cli-ci.md#3-high-postgresstorage-не-имеет-вообще-никакого-тестового-покрытия-ни-unit-ни-integration) |
| Medium | Архитектура | `README.md:56` vs `benchmark.py:206-213` | Quick-start в README вызывает `run_phase(tag=..., rounds=...)` без обязательного `candidates` — падает `TypeError` | [01](01-architecture-design.md#finding-6-medium-readme-quick-start-вызывает-run_phase-без-обязательного-candidates-сломанный-copy-paste) |
| Medium | Архитектура | `pyproject.toml:70` | Console-script `llm-bench` указывает на несуществующий модуль | [01](01-architecture-design.md#finding-7-medium-console-script-entry-point-llm-bench-ссылается-на-несуществующий-модуль) |
| Medium | Архитектура | `round_runner.py:75-78,365-384`; `provider/__init__.py:1` | Контракт «провайдера» нигде не оформлен как `Protocol`, в отличие от остальных точек расширения | [01](01-architecture-design.md#finding-8-medium-контракт-провайдера-неформальный-duck-typing-без-protocol-в-отличие-от-остальных-точек-расширения) |
| Medium | Качество кода | `runner/budget.py:63` | `# type: ignore[arg-type]` маскирует реальное несоответствие типов `str`/`ExperimentTag` | [02](02-code-quality-python-practices.md#medium-type-ignorearg-type-маскирует-реальное-расхождение-типов-strexperimenttag) |
| Medium | Качество кода | `cost/estimator.py:136` | `# type: ignore[operator]` — mypy не может сузить `Optional` через промежуточный булев флаг | [02](02-code-quality-python-practices.md#medium-type-ignoreoperator-mypy-не-сужает-optional-через-промежуточный-bool) |
| Medium | Качество кода | `test_storage_protocol.py:5,33-40`, `conftest.py:36-43` | PostgresStorage (509 строк) не покрыт вообще никаким тестом | [02](02-code-quality-python-practices.md#medium-postgresstorage-не-покрыт-вообще-никаким-тестом) |
| Medium | Качество кода | `tests/property/__init__.py` | Директория `property/` пуста; `hypothesis>=6.0` объявлен как dev-зависимость, но нигде не используется | [02](02-code-quality-python-practices.md#medium-testsproperty-пуста-заявленная-hypothesis-часть-тестового-набора-не-существует) |
| Medium | Качество кода | `round_runner.py:308`, `alive_filter.py:44-72` | Fast-abort «3+ DEAD-ошибки» не учитывает `RateLimited` — вероятно намеренно, стоит подтвердить у автора | [02](02-code-quality-python-practices.md#medium-fast-abort-«3-dead-errors»-в-одном-пайплайне-игнорирует-ratelimited-стоит-подтвердить-у-автора) |
| Medium | Корректность | `round_runner.py:145-152` vs `core/scoring.py:30-36` | `mean_latency_sec` считается в `compute_ranking`, но никогда не агрегируется/передаётся в `Halving.promote()` | [03](03-correctness-edge-cases.md#medium-mean_latency_sec-не-передаётся-из-round_runnerpy-в-halvingpromote) |
| Medium | Корректность | `halving/driver.py:174-181` | Multi-specialty bonus (+0.05) вычисляется, кладётся в `RoundResult.scores`, но это поле никем не читается | [03](03-correctness-edge-cases.md#medium-multi-specialty-bonus-не-влияет-ни-на-одно-реальное-решение-о-продвижении) |
| Medium | Корректность | `round_runner.py:191,254,297,360` | `provider="openrouter"` захардкожен независимо от реального `provider_factory` | [03](03-correctness-edge-cases.md#medium-provideropenrouter-захардкожен-независимо-от-provider_factory) |
| Medium | Корректность | `halving/pruner.py:125,130` | `mad == 0.0` / `spread == 0.0` — точное сравнение float без допуска | [03](03-correctness-edge-cases.md#medium-mad-00-spread-00-точное-сравнение-float-без-допуска) |
| Medium | Корректность | `halving/schedule.py` (весь класс) | Нет валидации `round_sizes`/`units_per_arm` (длины, монотонность, положительность) | [03](03-correctness-edge-cases.md#medium-halvingschedule-не-валидирует-свою-конфигурацию) |
| Medium | Корректность | `ranking/ranker.py:181-186` | `RowScorer`/`GoldChecker` не валидируются/не клэмпятся в `[0,1]` | [03](03-correctness-edge-cases.md#medium-rowscorergoldchecker-не-валидируются-в-0-1) |
| Medium | Корректность | `halving/schedule.py:63-77` vs `driver.py:151-172` | В последнем раунде specialty preservation может вернуть >1 «финального победителя» | [03](03-correctness-edge-cases.md#medium-последний-раунд-может-вернуть-больше-одного-«финального-победителя») |
| Medium | Корректность | `cost/estimator.py:126-129` | `estimate_call_cost` полностью исключает модели с ценой `<=0` вместо трактовки их как бесплатных | [03](03-correctness-edge-cases.md#medium-estimate_call_cost-исключает-легитимно-бесплатные-модели) |
| Medium | Асинхронность | `file.py:124-125,153-154,317-321` | Синхронный `open()`/`write()` внутри `async def` под локом на каждую запись (подтверждено `ruff ASYNC230`) | [04](04-async-concurrency.md#medium-синхронный-блокирующий-openwrite-внутри-async-def-под-локом-на-каждую-запись-подтверждено-независимо-через-ruff---select-async-правило-не-входит-в-реально-включённый-select-репозитория) |
| Medium | Асинхронность | `postgres.py:425-441`; `file.py:342-362` | `delete_experiment` — два связанных удаления не обёрнуты в одну транзакцию/атомарную операцию | [04](04-async-concurrency.md#medium-delete_experiment-связанные-удаления-не-атомарны) |
| Medium | Асинхронность | `postgres.py:175-186`; `file.py:79-87` | `initialize()` — TOCTOU race без лока при создании pool/connection | [04](04-async-concurrency.md#medium-initialize-toctou-race-без-лока-на-создание-poolconnection) |
| Medium | Асинхронность | `file.py:89-92` | `close()` не берёт `self._lock` — может закрыть соединение параллельно с активной операцией | [04](04-async-concurrency.md#medium-filestorageclose-не-берёт-self_lock) |
| Medium | Асинхронность | `stage/base.py:22,38-39` | `StageContext.quarantined`/`quarantine_reason` — документированный "storm-detection" механизм нигде не реализован | [04](04-async-concurrency.md#medium-stagecontextquarantinedquarantine_reason-задокументированный-механизм-отмены-выполнения-не-реализован) |
| Medium | Тесты | `file.py:104-126` | `FileStorage.upsert_prompts` проверяет идемпотентность по НЕПРАВИЛЬНОЙ таблице (`resume_cache` вместо индекса промптов) | [05](05-test-quality-coverage.md#medium-filestorageupsert_prompts-проверяет-идемпотентность-по-неправильной-таблице) |
| Medium | Тесты | `per_stage_winners.py:110-113` | Тай-брейк "latest by ts" в `load_winner_substrate` инвертирован относительно собственной докстроки | [05](05-test-quality-coverage.md#medium-load_winner_substrate-тай-брейк-latest-by-ts-инвертирован-относительно-докстроки) |
| Medium | Тесты | `cost/openrouter.py` (весь файл) | Конкретный адаптер каталога OpenRouter не покрыт вообще ни одним тестом | [05](05-test-quality-coverage.md#medium-costopenrouterpy-конкретный-адаптер-каталога-openrouter-не-покрыт-ни-одним-тестом) |
| Medium | Тесты | `cost/filter.py:41-43` | `CostFilter.moderated_penalty` — полностью мёртвое поле, задокументированное как реально работающее | [05](05-test-quality-coverage.md#medium-costfiltermoderated_penalty-мёртвое-поле-задокументированное-как-реально-работающее) |
| Medium | Тесты | `tests/property/` + `pyproject.toml:59` | `hypothesis>=6.0` объявлен, каталог существует, но property-based тестов нет вообще ни одного | [05](05-test-quality-coverage.md#medium-testsproperty-пустая-директория-hypothesis-объявлен-но-не-используется-вообще) |
| Medium | Тесты | `src/llm_bench/runner/resume.py` (весь файл) | `ResumeCache` не имеет ни одного прямого unit-теста | [05](05-test-quality-coverage.md#medium-resumecache-не-имеет-ни-одного-прямого-unit-теста) |
| Medium | Тесты | `halving/pruner.py:145-192` | Paired-bootstrap ветка `mad_bootstrap_prune` (~50 строк статистики) не покрыта вообще ни одним тестом | [05](05-test-quality-coverage.md#medium-mad_bootstrap_prune-paired-bootstrap-ветка-penalty-за-дисперсиюстоимость-не-покрыта-тестами) |
| Medium | Производительность | `postgres.py:160-166` vs `benchmark.py:97` | `max_connections=8` по умолчанию против `global_concurrency=30` — пул станет узким местом | [06](06-performance-efficiency.md#5-medium-postgresstorage-пул-соединений-max8-меньше-дефолтного-global_concurrency-30) |
| Medium | Производительность | `file.py:236-269` | `query_rows(stage=...)` не спускает фильтр по стадии на уровень чтения файла — читает весь JSONL целиком | [06](06-performance-efficiency.md#6-medium-filestoragequery_rowsstage-не-спускает-фильтр-по-стадии-на-уровень-чтения-файла) |
| Medium | Производительность | `file.py:74,138-176,245-267,279-294` | Все операции FileStorage сериализованы одним `asyncio.Lock`, включая полные сканы файла | [06](06-performance-efficiency.md#7-medium-filestorage-сериализует-все-операции-одним-asynciolock-включая-полные-сканы-файла) |
| Medium | Безопасность | `.github/workflows/ci.yml:112-127` | Ruff/black/bandit-шаги job `lint` — `continue-on-error: true`; реальный блокирующий гейт есть только в необязательном локальном pre-commit | [07](07-security.md#finding-3-medium-security-гейты-в-github-actions-ci-advisory-only-continue-on-error-true-реально-блокирует-только-необязательный-local-pre-commit) |
| Medium | Безопасность | `test_storage_protocol.py:33-40`, `.env.example:23-25` | SQL-код PostgresStorage не покрыт вообще никаким тестом; referenced файлы `test_storage_protocol_postgres.py`/`test_smoke_postgres.py` не существуют | [07](07-security.md#finding-4-medium-sql-код-postgresstorage-не-покрыт-вообще-никаким-тестом-в-репозитории) |
| Medium | Безопасность | `pyproject.toml:62`; `ci.yml:72,157,194`; `mypy-full.yml:33` | `py-ci-shared` и `pyutilz` подтягиваются через `git+https://...` без пина на commit/tag | [07](07-security.md#finding-5-medium-pyutilz-и-py-ci-shared-подтягиваются-из-git-без-пина-на-committag) |
| Medium | SQL/Postgres | `postgres.py:67-106` | Нет `FOREIGN KEY` от `benchmark_results.composite_hash` к `benchmark_prompts.composite_hash` | [08](08-sql-postgres-storage.md#finding-8-нет-fk-benchmark_resultscomposite_hash-→-benchmark_promptscomposite_hash-medium) |
| Medium | SQL/Postgres | `postgres.py:160-166`; `round_runner.py:71`; `budget.py:63` | `max_connections=8` явно меньше `global_concurrency=30`, усугубляется частыми spend-агрегатами | [08](08-sql-postgres-storage.md#finding-9-дефолтный-размер-пула-8-меньше-дефолтного-global_concurrency-30-усугубляется-частыми-spend-агрегатами-medium) |
| Medium | SQL/Postgres | `postgres.py:108-109` | `idx_benchmark_results_stage` как одиночный индекс не соответствует реальному паттерну запросов (всегда вместе с `experiment_tag`) | [08](08-sql-postgres-storage.md#finding-10-idx_benchmark_results_stage-не-соответствует-реальному-паттерну-запросов-medium) |
| Medium | SQL/Postgres | `postgres.py:110` | `idx_benchmark_results_task_unit` не используется ни одним запросом в этом файле | [08](08-sql-postgres-storage.md#finding-11-idx_benchmark_results_task_unit-не-используется-ни-одним-запросом-в-этом-файле-medium) |
| Medium | SQL/Postgres | `postgres.py:73`; `core/types.py:193` | `task_unit_id` в БД nullable, хотя в доменной модели `RunRow.task_unit_id: str` — обязательное поле | [08](08-sql-postgres-storage.md#finding-12-task_unit_id-nullable-в-бд-хотя-в-домене-обязателен-medium) |
| Medium | SQL/Postgres | `postgres.py:81-97` | Нет `CHECK`-констрейнтов против отрицательных cost/token значений | [08](08-sql-postgres-storage.md#finding-13-нет-check-констрейнтов-против-отрицательных-costtoken-значений-medium) |
| Medium | HTTP/API/LLM | `stage/base.py:22-24,38-39` | `StageContext.quarantined`/`quarantine_reason` документированы как "storm-detection", но нигде не устанавливаются | [09](09-http-api-llm-integration.md#6-medium-stagecontextquarantinedquarantine_reason-документированы-но-никогда-не-устанавливаются) |
| Medium | HTTP/API/LLM | `postgres.py:137-167` и 18 сайтов | `schema_name` не валидируется/не аллоклистится нигде — докстринг заявляет безопасность, которую код не обеспечивает | [09](09-http-api-llm-integration.md#7-medium-postgresstorageschema_name-sql-инъекционный-примитив-без-валидации-несмотря-на-заявление-безопасности-в-докстринге) |
| Medium | HTTP/API/LLM | `cost/openrouter.py:179-203` | Ноль собственной устойчивости (try/except/retry/фолбэк) вокруг `list_openrouter_models(...)` | [09](09-http-api-llm-integration.md#8-medium-costopenrouterpy-ноль-собственной-устойчивости-к-сбоям-каталог-фетча) |
| Medium | HTTP/API/LLM | `budget.py:75-79` vs `round_runner.py:300` и `storage.query_spend_by_op` | Несогласованность базы стоимости: `record_cost()` пишет "сырую" `cost_usd`, а `check()` сравнивает с накопленной `effective_cost_usd` | [09](09-http-api-llm-integration.md#9-medium-budgetgate-использует-несогласованные-базы-стоимости-cost_usd-для-проекции-vs-effective_cost_usd-для-факта) |
| Medium | HTTP/API/LLM | `cost/openrouter.py` (весь файл) — нет теста | Нулевое покрытие тестами маппинга сырых полей OpenRouter → `CatalogueEntry` | [09](09-http-api-llm-integration.md#10-medium-нулевое-тестовое-покрытие-маппинга-costopenrouterpy-парсинг-сырого-openrouter-json) |
| Medium | HTTP/API/LLM | `runner/classify.py` (весь файл) — нет теста | Нулевое покрытие тестами `classify_provider_error`, несмотря на задокументированный исторический баг | [09](09-http-api-llm-integration.md#11-medium-нулевое-тестовое-покрытие-classify_provider_error-несмотря-на-задокументированный-исторический-баг) |
| Medium | Пакетирование/CI | `postgres.py:155-167` | `schema_name` конструктора PostgresStorage не валидируется перед f-string-интерполяцией в SQL/DDL | [10](10-packaging-docs-cli-ci.md#4-medium-postgresstorageschema_name-не-валидируется-перед-f-string-интерполяцией-в-sql) |
| Medium | Пакетирование/CI | `ci.yml:94-134` vs `.pre-commit-config.yaml` | CI-джоба `lint` полностью `continue-on-error`; 6 "blocking" pre-commit хуков не имеют аналога в GitHub Actions | [10](10-packaging-docs-cli-ci.md#5-medium-реальный-ci-не-блокирует-то-что-pre-commit-документирует-как-«blocking»-заметный-gap-между-локальными-хуками-и-github-actions) |
| Medium | Пакетирование/CI | `CHANGELOG.md:6-22` vs git log | CHANGELOG описывает только первый коммит; 20+ реально запушенных коммитов не отражены | [10](10-packaging-docs-cli-ci.md#6-medium-changelogmd-существенно-устарел-относительно-реальной-истории-коммитов) |
| Medium | Пакетирование/CI | `release.yml:43-49` | Проверка «git tag == package version» пропускается для `workflow_dispatch` — ручной запуск публикует в PyPI без этой защиты | [10](10-packaging-docs-cli-ci.md#7-medium-releaseyml-проверка-«тег-версия-пакета»-пропускается-при-ручном-workflow_dispatch) |
| Low | Архитектура | `README.md:26` | Ссылка на `docs/architecture.md` мёртвая — папки `docs/` в репозитории нет | [01](01-architecture-design.md#finding-9-low-readme-ссылается-на-несуществующий-docsarchitecturemd) |
| Low | Архитектура | `pool/__init__.py:1`, `stage/__init__.py:1` | Не ре-экспортируют свои Protocol-ы, в отличие от storage/cost/halving/ranking/runner | [01](01-architecture-design.md#finding-10-low-pool__init__py-и-stage__init__py-не-ре-экспортируют-свои-protocol-ы) |
| Low | Архитектура | `cost/openrouter.py:157` | `OpenRouterCatalogue(ModelCatalogue)` явно наследуется от Protocol-класса, в отличие от storage-бэкендов | [01](01-architecture-design.md#finding-11-low-openroutercatalogue-явно-наследуется-от-protocol-класса-в-отличие-от-storage-бэкендов) |
| Low | Качество кода | `cost/openrouter.py:22,41` | Приватные хелперы `_per_token_to_per_m`/`_normalise_uptime` принимают `value` без аннотации типа | [02](02-code-quality-python-practices.md#low-непроаннотированные-параметры-в-приватных-хелперах-costopenrouterpy) |
| Low | Качество кода | `cost/openrouter.py:188` | `kwargs: dict` — «голый» `dict` вместо `dict[str, Any]` | [02](02-code-quality-python-practices.md#low-«голый»-dict-вместо-dictstr-any) |
| Low | Качество кода | `pyproject.toml` (`[[tool.mypy.overrides]] module = "tests.*"`) | Мёртвый конфиг: ни pre-commit, ни CI не запускают mypy на `tests/` | [02](02-code-quality-python-practices.md#low-мёртвая-секция-toolmypyoverrides-module-tests) |
| Low | Качество кода | `test_no_unicode_in_console_output.py:49-52` | AST-проверка смотрит только на `ast.Constant` как позиционные аргументы — f-строки/kwargs/конкатенацию не проверяет | [02](02-code-quality-python-practices.md#low-unicode-console-checker-не-видит-f-строкиkwargsконкатенацию) |
| Low | Качество кода | `halving/alive_filter.py:75` | Комментарий «Subset of DEAD_ERROR_CLASSES that are TRANSIENT» неточен | [02](02-code-quality-python-practices.md#low-неточная-формулировка-комментария-про-«подмножество»-в-alive_filterpy) |
| Low | Качество кода | `halving/pairing.py:38-82` | `_VENDOR_TO_FAMILY` — большая статичная руками сопровождаемая карта вендоров, будет тихо устаревать | [02](02-code-quality-python-practices.md#low-статичная-hand-maintained-карта-_vendor_to_family) |
| Low | Корректность | `halving/pairing.py:137` | Мёртвый `.sort()` — немедленно перекрывается `shuffle()` и повторным `.sort()` | [03](03-correctness-edge-cases.md#low-мёртвый-sort-в-pairingpy137) |
| Low | Корректность | `halving/schedule.py:41-56` | Дефолтный `pool_size` в `n_calls_for_stage()` может разойтись с реальным размером пула | [03](03-correctness-edge-cases.md#low-расхождение-дефолтного-pool_size-с-реальным-размером-пула) |
| Low | Корректность | `halving/driver.py:112` | Строка причины исключения может показывать >100% coverage | [03](03-correctness-edge-cases.md#low-coverage-строка-причины-исключения-может-показывать-100) |
| Low | Корректность | `stage/base.py:18-19` | Докстринг `StageContext` ссылается на несуществующее поле `parent_outputs`; реальное поле — `outputs` | [03](03-correctness-edge-cases.md#low-докстринг-stagecontext-ссылается-на-несуществующее-поле-parent_outputs) |
| Low | Корректность | `README.md:26` | Ссылка на `docs/architecture.md`, которого нет в репозитории | [03](03-correctness-edge-cases.md#low-readme-ссылается-на-несуществующий-docsarchitecturemd) |
| Low | Асинхронность | `file.py:80-83` | `initialize()` безусловно делает `mkdir()` на каждый hot-path вызов | [04](04-async-concurrency.md#low-filestorageinitialize-безусловно-вызывает-mkdir-на-каждый-hot-path-вызов) |
| Low | Асинхронность | `cost/estimator.py:211`, `cost/openrouter.py:186-197` | Полностью синхронный (вероятно сетевой) путь получения каталога моделей в асинхронном фреймворке | [04](04-async-concurrency.md#low-полностью-синхронный-путь-получения-каталога-моделей-в-асинхронном-фреймворке) |
| Low | Асинхронность | `ranking/ranker.py:180-198` | `compute_ranking` скорит строки строго последовательно даже когда `RowScorer`/`GoldChecker` асинхронны | [04](04-async-concurrency.md#low-compute_ranking-скорит-строки-строго-последовательно) |
| Low | Асинхронность | `postgres.py:161` vs `round_runner.py:71` | `PostgresStorage(max_connections=8)` по умолчанию vs `RoundConfig.global_concurrency=30` по умолчанию | [04](04-async-concurrency.md#low-рассогласование-дефолтов-postgresstoragemax_connections8-vs-roundconfigbenchmarkglobal_concurrency30) |
| Low | Тесты | (весь офлайн-сьют) | `pytest -m "not live"` сейчас НЕ зелёный (1/122 упавший) | [05](05-test-quality-coverage.md#low-офлайн-сьют-сейчас-не-полностью-зелёный) |
| Low | Тесты | `halving/pairing.py:123-150` | Анти-коллизийная логика "избегать (A,B)+(B,A)" в `allowed_validator_pairs` (N>=4) не тестируется напрямую | [05](05-test-quality-coverage.md#low-allowed_validator_pairs-n4-анти-симметричная-логика-не-тестируется-напрямую) |
| Low | Тесты | `halving/driver.py` (весь `promote`) | `Halving.promote()` никогда не тестируется с ровно 1 выжившим кандидатом | [05](05-test-quality-coverage.md#low-halvingpromote-никогда-не-тестируется-с-единственным-выжившим-кандидатом) |
| Low | Тесты | `storage/{memory,file}.py` vs `postgres.py` | Разная точность суммирования (float vs `NUMERIC`) не покрыта precision-чувствительным тестом | [05](05-test-quality-coverage.md#low-расхождение-в-точности-суммирования-между-inmemorystoragefilestorage-и-postgresstorage-не-покрыто) |
| Low | Производительность | `core/types.py:122-139`, `round_runner.py:180` | `StageGraph.topo_order()` пересчитывается DFS-обходом на каждый (model, task_unit) пайплайн вместо одного раза за раунд | [06](06-performance-efficiency.md#8-low-stagegraphtopo_order-пересчитывается-на-каждый-model-task_unit-пайплайн-вместо-одного-раза-за-раунд) |
| Low | Производительность | `round_runner.py:211`, `postgres.py:195-221`, `file.py:96-126` | `upsert_prompts()` вызывается без клиентского дедупа "уже видели этот hash в этом run" | [06](06-performance-efficiency.md#9-low-upsert_prompts-вызывается-без-клиентского-дедупа-уже-видели-этот-hash-в-этом-run) |
| Low | Производительность | `memory.py:137,188` | `InMemoryStorage.query_rows`/`load_winners` делают `deepcopy` каждой строки под локом вместо более дешёвого варианта | [06](06-performance-efficiency.md#10-low-inmemorystoragequery_rowsload_winners-deepcopy-вместо-более-дешёвой-изоляции) |
| Low | Безопасность | `round_runner.py:229-230` | `error_message = str(e)[:500]` персистится без редактирования в JSONL/Postgres — потенциальный канал утечки секрета | [07](07-security.md#finding-6-low-непроверенноенеотредактированное-исключение-stre500-персистится-в-storage-и-в-логи) |
| Low | Безопасность | `postgres.py:179-183` | `asyncpg.create_pool(dsn=self._url)` вызывается без try/except внутри `initialize()` — DSN потенциально с паролем | [07](07-security.md#finding-7-low-postgresstorageinitialize-не-перехватывает-исключение-asyncpgcreate_pool-потенциально-несущее-dsn) |
| Low | Безопасность | `storage/base.py`, все три backend-а | Ни один backend не предлагает опцию at-rest шифрования — промпты/ответы пишутся в plaintext | [07](07-security.md#finding-8-low-отсутствие-опции-шифрования-результатов-at-rest-ни-в-одном-backend) |
| Low | SQL/Postgres | `postgres.py:199,226,263,292,322,339,352,381,402,428` | `assert self._pool is not None` как единственная защита от вызова до `initialize()` — пропадает под `python -O` | [08](08-sql-postgres-storage.md#finding-14-assert-self_pool-is-not-none-как-единственная-защита-от-вызова-до-initialize-low) |
| Low | SQL/Postgres | `postgres.py:81-96` | `NUMERIC`-колонки корректны по типу, но значения приходят как нативные Python `float` без `Decimal` | [08](08-sql-postgres-storage.md#finding-15-numeric-колонки-корректны-по-типу-но-точность-не-реализована-сквозным-образом-low-info) |
| Low | HTTP/API/LLM | `halving/pairing.py:137-138` | Мёртвая строка `eligible.sort(...)`, немедленно перезаписываемая `rng.shuffle(...)` | [09](09-http-api-llm-integration.md#12-low-halvingpairingpyallowed_validator_pairs-мёртвая-строка-сортировки) |
| Low | HTTP/API/LLM | `runner/classify.py:12-39` | Классификация по подстрокам в сообщении об ошибке — хрупкая к изменению формулировок апстрима | [09](09-http-api-llm-integration.md#13-low-classify_provider_error-хрупкий-substring-matching-без-табличного-покрытия-реальных-сообщений) |
| Low | HTTP/API/LLM | `cost/filter.py:37-39,68-73` | `CostFilter.require_healthy=True` — молчаливый no-op для любого не-OR `ModelCatalogue` | [09](09-http-api-llm-integration.md#14-low-costfilterrequire_healthytrue-молчаливый-no-op-для-любого-не-or-каталога) |
| Low | Пакетирование/CI | `README.md:26` | Ссылка на `docs/architecture.md` — директории `docs/` в репозитории не существует вовсе | [10](10-packaging-docs-cli-ci.md#8-low-readmemd-ссылается-на-docsarchitecturemd-которого-не-существует) |
| Low | Пакетирование/CI | `tests/test_meta/test_code_audit_baseline.py` | Офлайн-сьют (команда из README) на текущем HEAD падает с новой находкой `docstring_args_incomplete` в `cost/estimator.py:161` | [10](10-packaging-docs-cli-ci.md#9-low-офлайн-pytest-сьют-команда-из-самого-readme-сейчас-красный-1-упавший-meta-тест) |
| Low | Пакетирование/CI | `CHANGELOG.md:20` vs `pyproject.toml:37` | CHANGELOG утверждает «Hard dep on pyutilz>=1.1», `pyproject.toml` фактически пинит `>=1.0` | [10](10-packaging-docs-cli-ci.md#10-low-changelogmd-утверждает-«hard-dep-on-pyutilz11»-реальный-пин-в-pyprojecttoml-10) |
| Info | Архитектура | `test_no_import_cycles.py` + собственная проверка графа импортов | Слоистость core→pool/stage/cost/storage/provider→halving/ranking/discovery→confirmation/runner→cli реально соблюдена (позитивная находка) | [01](01-architecture-design.md#finding-12-info-слоистость-импортов-реально-соблюдена-позитивная-находка) |
| Info | Архитектура | `tests/test_meta/test_code_audit_baseline.py` | На чистом чекауте уже падает (`docstring_args_incomplete` в `cost/estimator.py:161`) — CI сейчас red вне зависимости от находок этого отчёта | [01](01-architecture-design.md#finding-13-info-test_code_audit_baseline-уже-красный-на-чистом-чекауте-вне-категории-этого-аудита) |
| Info | Качество кода | `README.md:20`, отсутствие `docs/` | README рекламирует «Validator-pairing for cross-family scoring», а `docs/architecture.md` физически отсутствует | [02](02-code-quality-python-practices.md#info-readme-заявляет-функциональность-для-которой-отсутствует-ссылочная-документация) |
| Info | Качество кода | `pyproject.toml` (`[project.scripts]`), `cli/` | Заявлен console-script `llm-bench = llm_bench.cli.main:main`, но `cli/main.py` не существует | [02](02-code-quality-python-practices.md#info-console_scripts-точка-входа-указывает-на-несуществующий-модуль) |
| Info | Корректность | `postgres.py:484` vs DDL `NUMERIC` | Стоимость везде — обычный `float`, включая явный `float()`-каст `NUMERIC` из Postgres; практических ошибок не даёт | [03](03-correctness-edge-cases.md#info-cost-accumulation-plain-float-везде-но-не-критично-на-данных-масштабах) |
| Info | Корректность | `runner/budget.py:66-73` | На границе `cap_usd` и при `cap_usd<=0` gate корректно скипает, не падает | [03](03-correctness-edge-cases.md#info-budgetgate-корректно-скипает-на-границе-cap_usd-не-падает) |
| Info | Асинхронность | `src/` (весь) | `asyncio.create_task` нигде не используется — класс багов "fire-and-forget задача собрана GC" неприменим | [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов) |
| Info | Асинхронность | `src/` (весь) | Bare `except:`/`except BaseException` нигде не найдены — `CancelledError` корректно пробрасывается | [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов) |
| Info | Асинхронность | `round_runner.py:119`; `benchmark.py:192` | `asyncio.gather` без `return_exceptions=True`, но т.к. каждая корутина сама ловит исключения, поведение эквивалентно | [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов) |
| Info | Асинхронность | `postgres.py:137-153` | `# nosec B608`/`self._schema` — вопрос вне категории async/concurrency, но `schema_name` не валидируется (факт зафиксирован) | [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов) |
| Info | Тесты | `tests/test_meta/_code_audit_baseline.json` | Механизм — работающий "трещоточный" gate (не rubber stamp), но 17 бейзлайновых находок навсегда прощены без срока пересмотра | [05](05-test-quality-coverage.md#info-code_audit_baseline-реально-работающий-трещоточный-gate-не-rubber-stamp-но-17-находок-прощены-навсегда) |
| Info | Тесты | `pyproject.toml:82` | `-p no:randomly` — не задокументированный воркэраунд конкретного бага, а скопированный из pyutilz шаблон | [05](05-test-quality-coverage.md#info--p-norandomly-не-задокументированный-воркэраунд-конкретного-бага-а-унаследованный-шаблон) |
| Info | Тесты | `tests/integration/test_job_app_example.py` | Пример-консьюмер имеет достаточно честный e2e-тест — заметных пробелов не найдено | [05](05-test-quality-coverage.md#info-examplesjob_app_cover_letter-интеграционный-тест-честный-заметных-пробелов-не-найдено) |
| Info | Производительность | `ranking/per_stage_winners.py:81-114` | `load_winner_substrate` нигде не вызывается; если подключат так, как предписывает докстринг, реализация станет классическим N+1 | [06](06-performance-efficiency.md#11-info-load_winner_substrate-не-подключена-к-round_runnerpy-при-подключении-её-текущая-реализация-станет-n1) |
| Info | Производительность | `cost/openrouter.py:157-203` | Кеширование каталога полностью делегировано `pyutilz`; фактическое TTL-кеширование не проверяемо из этого чекаута | [06](06-performance-efficiency.md#12-info-кеширование-каталога-моделей-полностью-делегировано-pyutilz-не-проверяемо-из-этого-чекаута) |
| Info | Безопасность | (—) | Секреты (`OPENROUTER_API_KEY` и т.д.) читаются только через `os.environ.get(...)` для presence-check, никогда не логируются | [07](07-security.md#summary-table) |
| Info | Безопасность | (—) | `eval`/`exec`/`pickle`/`yaml.load`(unsafe)/`subprocess`/`os.system`/`shell=True` — ни одного вхождения во всём `src/` и `examples/` | [07](07-security.md#summary-table) |
| Info | Безопасность | `round_runner.py:329-343`, `stages.py:109-129` | LLM-output обрабатывается как untrusted: `_parse_safely` оборачивает parser-callback в try/except | [07](07-security.md#summary-table) |
| Info | Безопасность | `.github/workflows/*.yml` | zizmor-hardening заявления подтверждены: все actions SHA-pinned, `persist-credentials: false`, `permissions: contents: read` | [07](07-security.md#summary-table) |
| Info | SQL/Postgres | `tests/unit/test_storage_protocol.py:33-40` | PostgresStorage не участвует ни в одном тесте контракта — объясняет, почему Critical Finding 1 (crash) не был замечен раньше | [08](08-sql-postgres-storage.md#finding-16-в-репозитории-нет-ни-одного-реального-теста-postgresstorage-против-живой-бд-info) |
| Info | HTTP/API/LLM | `src/llm_bench/**` | Прямых обращений к `httpx` в репозитории нет — весь HTTP-слой полностью делегирован `pyutilz` | [09](09-http-api-llm-integration.md#15-info-прямых-обращений-к-httpx-в-srcllm_bench-нет) |
| Info | HTTP/API/LLM | `round_runner.py:301-312` | Единственная retry/backoff-подобная логика этого репо — грубый circuit breaker "3 DEAD-класса ошибок → прервать пайплайн" | [09](09-http-api-llm-integration.md#16-info-retrybackoff-реально-принадлежащий-этому-репозиторию-только-грубый-circuit-breaker) |
| Info | Пакетирование/CI | `pyproject.toml:72-74`, `egg-info/SOURCES.txt` | 4 пустых подпакета (`cli/`, `provider/`, `discovery/`, `confirmation/`) реально попадают в собранный wheel | [10](10-packaging-docs-cli-ci.md#11-info-четыре-пустых-подпакета-реально-попадают-в-собранный-wheel) |
| Info | Пакетирование/CI | `git tag -l` (пусто) | Тегов/релизов ещё не было — то, что CHANGELOG держит всё под `[Unreleased]`, само по себе корректно | [10](10-packaging-docs-cli-ci.md#12-info-теговgithub-releases-ещё-не-было-структура-changelog-unreleased-only-сама-по-себе-корректна) |

Итого: 16 Critical, 36 High, 50 Medium, 36 Low, 24 Info = **162 находки** из 10 отчётов.

## Кросс-категорийные наблюдения

Находки ниже независимо всплыли в 2 и более отчётах (разными агентами, разными методами — чтением
кода, статическим анализом, репро-скриптами) — это самый надёжный сигнал во всём аудите, поскольку
подтверждён с разных углов зрения без сговора между агентами.

1. **`schema_name` в `PostgresStorage` f-string-интерполируется в SQL без валидации, а
   docstring-обоснование безопасности ничем не подкреплено в коде** — независимо подтверждено
   **8 из 10** отчётов: [01](01-architecture-design.md#finding-3-high-postgresstorageschema_name-sql-injection-surface-без-валидации-несмотря-на-заявленную-в-docstring-безопасность),
   [02](02-code-quality-python-practices.md#high-postgresstorageschema_name-динамический-sql-без-валидации-обоснование-безопасности-не-подкреплено-кодом),
   [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов),
   [05](05-test-quality-coverage.md#high-postgresstorage-не-покрыт-вообще-ни-одним-тестом-self-graded-nosec-b608-не-подкреплён-валидацией-schema_name),
   [07](07-security.md#finding-2-high-schema_name-в-postgresstorage-неэкранированная-интерполяция-идентификатора-без-какой-либо-валидации-в-коде),
   [08](08-sql-postgres-storage.md#finding-5-schema_name-не-валидируется-перед-f-string-интерполяцией-в-sql-high),
   [09](09-http-api-llm-integration.md#7-medium-postgresstorageschema_name-sql-инъекционный-примитив-без-валидации-несмотря-на-заявление-безопасности-в-докстринге),
   [10](10-packaging-docs-cli-ci.md#4-medium-postgresstorageschema_name-не-валидируется-перед-f-string-интерполяцией-в-sql).
   Самый кросс-подтверждённый пункт всего аудита — практически каждая категория, коснувшаяся
   `postgres.py`, отметила именно это.
2. **PostgresStorage не имеет вообще никакого тестового покрытия в репозитории** (ни unit, ни
   integration; referenced `test_storage_protocol_postgres.py`/`test_smoke_postgres.py` не
   существуют) — подтверждено **5 из 10** отчётов:
   [02](02-code-quality-python-practices.md#medium-postgresstorage-не-покрыт-вообще-никаким-тестом),
   [05](05-test-quality-coverage.md#high-postgresstorage-не-покрыт-вообще-ни-одним-тестом-self-graded-nosec-b608-не-подкреплён-валидацией-schema_name),
   [07](07-security.md#finding-4-medium-sql-код-postgresstorage-не-покрыт-вообще-никаким-тестом-в-репозитории),
   [08](08-sql-postgres-storage.md#finding-16-в-репозитории-нет-ни-одного-реального-теста-postgresstorage-против-живой-бд-info),
   [10](10-packaging-docs-cli-ci.md#3-high-postgresstorage-не-имеет-вообще-никакого-тестового-покрытия-ни-unit-ни-integration).
   Именно это объясняет, почему Critical-баг из п.5 ниже (краш `prefetch_resume_cache`) не был
   замечен раньше — SQL-отчёт [08](08-sql-postgres-storage.md#finding-16-в-репозитории-нет-ни-одного-реального-теста-postgresstorage-против-живой-бд-info)
   прямо указывает на эту причинно-следственную связь.
3. **Per-stage winner ("M_X") promotion — заявленная как ключевая фича — нигде не подключена к
   раннеру: `persist_winners` пишет, `load_winner_substrate`/`load_winners` никогда не читают
   обратно** — независимо подтверждено (все — Critical) **4 из 10** отчётов:
   [01](01-architecture-design.md#finding-1-critical-per-stage-winner-promotion-мёртвый-код-никогда-не-подключён-к-раннеру),
   [02](02-code-quality-python-practices.md#critical-per-stage-«m_x»-substrate-promotion-не-подключена-к-раннеру),
   [03](03-correctness-edge-cases.md#critical-m_x-per-stage-winner-substrate-promotion-write-only),
   [09](09-http-api-llm-integration.md#1-critical-per-stage-winner-m_x-substrate-promotion-объявлен-но-никогда-не-выполняется).
   Каждый отчёт цитирует один и тот же явный docstring-предупреждающий текст в
   `per_stage_winners.py:93-98` ("Without this wiring... silently uses each candidate's own
   upstream output") — то есть авторы кода сами предвидели этот ровно тот баг.
4. **Layer-1 "модель не валидирует сама себя" (`Stage.is_validator` / cross-family swap) никогда
   не применяется раннером** — независимо подтверждено (все — Critical) **3 из 10** отчётов:
   [02](02-code-quality-python-practices.md#critical-layer-1-независимость-валидатора-«модель-не-судит-сама-себя»-не-применяется),
   [03](03-correctness-edge-cases.md#critical-validator-независимость-layer-1-no-self-judgment-никогда-не-применяется),
   [09](09-http-api-llm-integration.md#2-critical-cross-family-validator-independence-is_validator-никогда-не-применяется-protocol-не-даёт-для-этого-данных).
   [09] дополнительно отмечает, что `PromptBuilder` Protocol физически не передаёт ни producer
   model_id, ни пул кандидатов — то есть это невозможно реализовать даже на стороне консьюмера без
   форка раннера.
5. **`record_call`'s идемпотентность (`ON CONFLICT DO NOTHING` / dict-key-exists) навсегда теряет
   успешный повтор вызова, если под тем же PK уже лежит провалившаяся строка** — независимо
   подтверждено **3 из 10** отчётов, два из них с исполняемыми repro-скриптами:
   [01](01-architecture-design.md#finding-2-critical-record_call-не-умеет-перезаписать-провалившуюся-строку-успешным-повтором-inmemorystorage-postgresstorage),
   [03](03-correctness-edge-cases.md#critical-успешный-повтор-вызова-молча-теряетсязатеняется-старой-неудачной-записью-с-тем-же-pk)
   (repro на InMemoryStorage и FileStorage),
   [08](08-sql-postgres-storage.md#finding-2-on-conflict-do-nothing-в-record_call-навсегда-хоронит-успешный-повтор-ранее-провалившегося-вызова-critical)
   (детальный SQL-разбор). Прямо противоречит заявленной ключевой фиче "resume cache".
6. **Resume-cache попадания не засчитываются в coverage-gate → полностью резюмированный
   (100% cache-hit) раунд элиминирует ВСЕХ кандидатов** — независимо подтверждено (оба —
   Critical, оба с работающими repro-скриптами) **2 из 10** отчётов:
   [03](03-correctness-edge-cases.md#critical-resumeretry-ломает-coverage-gate-закэшированный-раунд-теряет-всех-кандидатов),
   [05](05-test-quality-coverage.md#critical-round_runnerpy-resume-after-interrupt-обнуляет-весь-пул-кандидатов-через-coverage-gate).
   Оба репро дают идентичный вывод (`below coverage_min: 0/N = 0%`) на независимо написанном коде —
   самая надёжная находка всего аудита по силе подтверждения (Critical + Critical + двойное repro).
7. **`BudgetGate.check()` — TOCTOU-гонка: нет резервирования "in-flight" трат, конкурентные вызовы
   одного `op` коллективно проходят проверку до того, как кто-то из них запишет свою стоимость** —
   независимо подтверждено **4 из 10** отчётов (3 как корректностный/concurrency-баг, 1 как
   перформанс-паразитная нагрузка того же метода):
   [03](03-correctness-edge-cases.md#high-budgetgate-не-резервирует-«in-flight»-расходы-конкурентные-вызовы-одного-op-коллективно-превышают-cap)
   (repro: $4.50 факт против cap $1.00),
   [04](04-async-concurrency.md#high-budgetgatecheck-toctou-race-конкурентные-вызовы-одного-op-могут-совместно-превысить-cap_usd),
   [09](09-http-api-llm-integration.md#4-high-budgetgatecheck-toctou-гонка-под-реальной-конкурентностью),
   [06](06-performance-efficiency.md#3-high-budgetgatecheck-заново-агрегирует-весь-потраченный-бюджет-из-storage-перед-каждым-вызовом-стадии)
   (тот же метод, угол атаки — лишняя нагрузка на storage на каждый вызов).
8. **`prefetch_resume_cache` не имеет поддерживающего индекса и делает неограниченный full scan
   растущей без границ таблицы** — подтверждено **2 из 10** отчётов с разных углов (перформанс и
   SQL-корректность), причём SQL-отчёт дополнительно обнаружил, что этот же метод банально
   ПАДАЕТ на реальном Postgres (см. п.16 сводной таблицы, Critical): [06](06-performance-efficiency.md#4-high-postgresstorageprefetch_resume_cache-полный-скан-без-поддерживающего-индекса-растущий-без-ограничения-по-мере-накопления-истории),
   [08](08-sql-postgres-storage.md#finding-6-prefetch_resume_cache-нет-индекса-под-фильтр-нет-ограничения-объёма-full-scan-неограниченная-загрузка-в-память-high).
9. **Консольный скрипт `llm-bench` зарегистрирован в `pyproject.toml`, но `llm_bench.cli.main`
   физически не существует** — подтверждено **3 из 10** отчётов, один — с эмпирическим запуском
   команды: [01](01-architecture-design.md#finding-7-medium-console-script-entry-point-llm-bench-ссылается-на-несуществующий-модуль),
   [02](02-code-quality-python-practices.md#info-console_scripts-точка-входа-указывает-на-несуществующий-модуль),
   [10](10-packaging-docs-cli-ci.md#1-high-консольный-скрипт-llm-bench-объявлен-в-pyprojecttoml-но-не-существует-падает-при-каждом-запуске)
   (`llm-bench` реально запущен в установленном editable-пакете → `ModuleNotFoundError`).
10. **README Quick Start не запускается «как есть»** (пропущен обязательный `candidates=`
    у `run_phase`, а также `op=` у второго `Stage`) — подтверждено **2 из 10** отчётов, один —
    эмпирической интроспекцией сигнатур: [01](01-architecture-design.md#finding-6-medium-readme-quick-start-вызывает-run_phase-без-обязательного-candidates-сломанный-copy-paste),
    [10](10-packaging-docs-cli-ci.md#2-high-пример-quick-start-в-readmemd-не-запускается-«как-есть»-два-независимых-typeerror).
11. **`docs/architecture.md`, на который ссылается README, физически не существует** —
    подтверждено **4 из 10** отчётов: [01](01-architecture-design.md#finding-9-low-readme-ссылается-на-несуществующий-docsarchitecturemd),
    [02](02-code-quality-python-practices.md#info-readme-заявляет-функциональность-для-которой-отсутствует-ссылочная-документация),
    [03](03-correctness-edge-cases.md#low-readme-ссылается-на-несуществующий-docsarchitecturemd),
    [10](10-packaging-docs-cli-ci.md#8-low-readmemd-ссылается-на-docsarchitecturemd-которого-не-существует).
12. **Офлайн pytest-сьют (команда `pytest -m "not live"` из самого README) прямо сейчас красный** —
    три агента независимо запустили ровно эту команду и получили один и тот же сбой
    (`docstring_args_incomplete` в `cost/estimator.py:161`, параметр `refresh` не задокументирован):
    [01](01-architecture-design.md#finding-13-info-test_code_audit_baseline-уже-красный-на-чистом-чекауте-вне-категории-этого-аудита),
    [05](05-test-quality-coverage.md#low-офлайн-сьют-сейчас-не-полностью-зелёный),
    [10](10-packaging-docs-cli-ci.md#9-low-офлайн-pytest-сьют-команда-из-самого-readme-сейчас-красный-1-упавший-meta-тест).
13. **`max_connections=8` (дефолт `PostgresStorage`) меньше `global_concurrency=30` (дефолт
    `RoundConfig`)** — подтверждено **3 из 10** отчётов: [04](04-async-concurrency.md#low-рассогласование-дефолтов-postgresstoragemax_connections8-vs-roundconfigbenchmarkglobal_concurrency30),
    [06](06-performance-efficiency.md#5-medium-postgresstorage-пул-соединений-max8-меньше-дефолтного-global_concurrency-30),
    [08](08-sql-postgres-storage.md#finding-9-дефолтный-размер-пула-8-меньше-дефолтного-global_concurrency-30-усугубляется-частыми-spend-агрегатами-medium).
14. **`Stage.budget_per_call` документирован как вход в проекцию `BudgetGate`, но нигде не
    читается** — подтверждено **2 из 10** отчётов (оба High): [03](03-correctness-edge-cases.md#high-stagebudget_per_call-документирован-но-нигде-не-используется),
    [09](09-http-api-llm-integration.md#5-high-stagebudget_per_call-документирован-но-никогда-не-используется-гейтом).
15. **`mean_latency_sec` считается для выбора winner'а этапа, но никогда не агрегируется/передаётся
    в `Halving.promote()` для halving cut** — подтверждено **2 из 10** отчётов: [02](02-code-quality-python-practices.md#high-latency-aware-tiebreak-учитывается-при-выборе-winnerа-этапа-но-не-при-halving-cut),
    [03](03-correctness-edge-cases.md#medium-mean_latency_sec-не-передаётся-из-round_runnerpy-в-halvingpromote).
16. **`classify_provider_error` не покрыт тестами, несмотря на задокументированный исторический
    инцидент неправильной классификации ("lesson from the live run on 2026-05-05")** —
    подтверждено **2 из 10** отчётов: [05](05-test-quality-coverage.md#high-classify_provider_error-покрыт-только-1-из-6-веток-при-том-что-регрессия-здесь-уже-случалась-в-проде),
    [09](09-http-api-llm-integration.md#11-medium-нулевое-тестовое-покрытие-classify_provider_error-несмотря-на-задокументированный-исторический-баг).
17. **`cost/openrouter.py`'s парсинг сырого JSON от OpenRouter (маппинг в `CatalogueEntry`) не
    покрыт вообще никаким тестом** — подтверждено **2 из 10** отчётов: [05](05-test-quality-coverage.md#medium-costopenrouterpy-конкретный-адаптер-каталога-openrouter-не-покрыт-ни-одним-тестом),
    [09](09-http-api-llm-integration.md#10-medium-нулевое-тестовое-покрытие-маппинга-costopenrouterpy-парсинг-сырого-openrouter-json).
18. **`delete_experiment`'s связанные удаления (Postgres: два `DELETE`; File: `rmtree` + SQLite
    delete) не атомарны** — подтверждено **2 из 10** отчётов: [04](04-async-concurrency.md#medium-delete_experiment-связанные-удаления-не-атомарны),
    [08](08-sql-postgres-storage.md#finding-7-delete_experiment-не-атомарен-между-двумя-delete-high).
19. **`StageContext.quarantined`/`quarantine_reason` — документированный "storm-detection"
    механизм — нигде не реализован** — подтверждено **2 из 10** отчётов: [04](04-async-concurrency.md#medium-stagecontextquarantinedquarantine_reason-задокументированный-механизм-отмены-выполнения-не-реализован),
    [09](09-http-api-llm-integration.md#6-medium-stagecontextquarantinedquarantine_reason-документированы-но-никогда-не-устанавливаются).
20. **Разрыв между тем, что pre-commit называет "blocking", и тем, что реально блокирует CI**
    (lint/bandit-шаги в `ci.yml` — `continue-on-error: true`) — подтверждено **2 из 10** отчётов:
    [07](07-security.md#finding-3-medium-security-гейты-в-github-actions-ci-advisory-only-continue-on-error-true-реально-блокирует-только-необязательный-local-pre-commit),
    [10](10-packaging-docs-cli-ci.md#5-medium-реальный-ci-не-блокирует-то-что-pre-commit-документирует-как-«blocking»-заметный-gap-между-локальными-хуками-и-github-actions).
21. **Хардкод `provider="openrouter"` в generic-раннере независимо от реального
    `provider_factory`** — подтверждено **2 из 10** отчётов: [01](01-architecture-design.md#finding-5-high-хардкод-openrouter-в-generic-раннере-ломает-заявленную-расширяемость-через-provider_factory),
    [03](03-correctness-edge-cases.md#medium-provideropenrouter-захардкожен-независимо-от-provider_factory).

## Пробелы аудита

Следующее ни один из 10 агентов не смог проверить эмпирически (инструмент недоступен, файл/сеть
отсутствуют) — человеку стоит перепроверить эти пункты напрямую, прежде чем считать связанные
находки окончательно подтверждёнными или окончательно неактуальными.

- **Ни один агент не запускал `PostgresStorage` против настоящего Postgres** (`LLM_BENCH_TEST_DB_URL`
  нигде не был установлен). Это касается ВСЕХ находок про `postgres.py` в отчётах
  [01](01-architecture-design.md), [02](02-code-quality-python-practices.md),
  [05](05-test-quality-coverage.md), [06](06-performance-efficiency.md), [07](07-security.md),
  [08](08-sql-postgres-storage.md), [09](09-http-api-llm-integration.md),
  [10](10-packaging-docs-cli-ci.md) — включая Critical-баг о падении `prefetch_resume_cache`
  ([08, Finding 1](08-sql-postgres-storage.md#finding-1-prefetch_resume_cache-гарантированно-падает-на-реальном-postgres-critical)),
  который выведен из чтения исходников установленного `asyncpg==0.31.0` (`_check_ready`/`_top_xact`),
  а не из реального запуска против сервера. Само по себе это довольно сильная косвенная улика (не
  голословное предположение), но однозначного live-подтверждения нет ни у одного агента — это
  единственный по-настоящему быстрый и дешёвый способ закрыть сразу несколько находок 08/06/07/05
  разом: поднять `docker run postgres:16`, выставить `LLM_BENCH_TEST_DB_URL`, прогнать
  `PostgresStorage.initialize()` + `prefetch_resume_cache()` вручную.
- **Ни один агент не запускал `pytest -m live`** (платные вызовы к реальным LLM намеренно
  исключены из всех 10 прогонов) — поведение при реальных отказах модели/провайдера, реальный
  текст сообщений об ошибках (важно для находки про хрупкий substring-matching в
  `classify_provider_error`, [09, п.13](09-http-api-llm-integration.md#13-low-classify_provider_error-хрупкий-substring-matching-без-табличного-покрытия-реальных-сообщений)),
  и фактическое поведение `pyutilz`'s HTTP/retry-слоя (connection reuse, timeouts, backoff) — вне
  зоны видимости всех 10 отчётов, поскольку `pyutilz` — sibling-репозиторий, не входящий в аудит
  (явно отмечено как out-of-scope в [09, Info №15-16](09-http-api-llm-integration.md#15-info-прямых-обращений-к-httpx-в-srcllm_bench-нет)
  и в [04](04-async-concurrency.md#info-позитивные-находки-отсутствие-ожидавшихся-классов-багов)).
- **Версионный дрейф sibling-репозиториев может влиять на находку "офлайн-сьют красный"**: агенты
  [05](05-test-quality-coverage.md#low-офлайн-сьют-сейчас-не-полностью-зелёный) и
  [10](10-packaging-docs-cli-ci.md#9-low-офлайн-pytest-сьют-команда-из-самого-readme-сейчас-красный-1-упавший-meta-тест)
  явно отмечают, что запускали `pytest` против локального editable-чекаута `py-ci-shared`/`pyutilz`,
  который может быть новее, чем коммит, на который последний раз обновлялся
  `_code_audit_baseline.json` в `llm_bench`. Оба рекомендуют проверить, действительно ли настоящий
  CI (клонирующий `pyutilz` на фиксированный момент через `git clone`) тоже красный, прежде чем
  считать это активной регрессией, а не артефактом локального окружения.
- **GitHub branch protection / required status checks не проверялись** — агент
  [07, Finding 3](07-security.md#finding-3-medium-security-гейты-в-github-actions-ci-advisory-only-continue-on-error-true-реально-блокирует-только-необязательный-local-pre-commit)
  явно пишет, что не может проверить реальные настройки защиты ветки на GitHub (нет `.git`, нет
  сетевого доступа/токена в среде аудита) — находка про `continue-on-error: true` может быть
  полностью компенсирована required-status-checks на уровне репозитория, а может и не быть; это
  нужно свериться напрямую в Settings → Branches репозитория на GitHub.
- **Постгрес-специфичные perf-находки в [06](06-performance-efficiency.md) основаны на текстовом
  сопоставлении `CREATE INDEX` с `WHERE`-условиями, а не на живом `EXPLAIN ANALYZE`** — сам агент
  прямо пишет об этом ограничении в Scope & method; реальный план запроса на заполненной таблице
  может отличаться от того, что подсказывает чтение DDL.
- **Осознанность нескольких "недоделанных фич" не установлена** — сразу несколько находок несут
  явную пометку "alternative reading: возможно, это намеренно отложено до будущей фазы, а не
  забыто" и рекомендацию спросить автора, а не однозначный вердикт "баг": fast-abort игнорирует
  `RateLimited` ([02](02-code-quality-python-practices.md#medium-fast-abort-«3-dead-errors»-в-одном-пайплайне-игнорирует-ratelimited-стоит-подтвердить-у-автора)),
  possibility that per-stage winner promotion и validator-independence сознательно отложены до
  будущего Phase, а не забыты при рефакторинге ([01, Finding 1](01-architecture-design.md#finding-1-critical-per-stage-winner-promotion-мёртвый-код-никогда-не-подключён-к-раннеру)),
  последний раунд halving способен вернуть >1 финального победителя — возможно намеренно
  ([03](03-correctness-edge-cases.md#medium-последний-раунд-может-вернуть-больше-одного-«финального-победителя»)).
  Для всех перечисленных находок серьёзность (Critical/High/Medium) выставлена в предположении
  "это баг", но человеку стоит явно свериться с автором/roadmap, прежде чем начинать исправление —
  особенно для двух Critical-находок про per-stage winner promotion и validator independence,
  вокруг которых выстроена значительная часть всего аудита.
