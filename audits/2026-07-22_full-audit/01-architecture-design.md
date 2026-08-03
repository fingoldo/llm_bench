# Аудит llm_bench: Архитектура и дизайн

## Scope & method (что прочитано, что запущено)

Прочитаны целиком (не выборочно) все файлы `src/llm_bench/**/*.py` (32 файла, core/cost/halving/pool/
ranking/runner/stage/storage + пять stub-пакетов cli/provider/discovery/confirmation), `pyproject.toml`,
`README.md`, `tests/unit/test_storage_protocol.py`, `tests/unit/test_per_stage_winners.py`,
`tests/integration/test_smoke_in_memory.py`, `tests/test_meta/test_no_import_cycles.py`,
`examples/job_app_cover_letter/run.py`.

Команды:
- `grep -n "^from llm_bench" src/llm_bench/**/*.py` — построение полного графа внутренних импортов вручную
  (не только по папкам, а по факту `from ... import`).
- `python -m pytest -m "not live" --no-cov -q` — офлайн-сьют (121 passed, 1 pre-existing meta-failure не
  связанная с архитектурой, см. Finding #13).
- Написан и запущен небольшой discriminating-script (`repro_findings.py`, лежит в scratchpad), который
  на `InMemoryStorage`/`FileStorage` напрямую воспроизводит два Critical-финдинга ниже (`record_call` после
  retry и отсутствие вызова `load_winner_substrate`) — вывод скрипта приведён в соответствующих находках.
- `python -c "import os; print(os.path.exists('src/llm_bench/cli/main.py'))"` → `False`, подтверждая
  разрыв между `pyproject.toml`'s entry point и реальным деревом файлов.

Не запускались и не трогались: git, live-тесты (`pytest -m live`), реальный Postgres (нет
`LLM_BENCH_TEST_DB_URL`) — вывод по `PostgresStorage` сделан чтением SQL-текста, что для находок про
SQL/идемпотентность достаточно (сама конструкция запроса детерминирована и не зависит от живой БД).

## Summary table

| Severity | File:Line | Одна строка |
|---|---|---|
| Critical | `src/llm_bench/runner/round_runner.py` (весь `_run_pipeline`, 168-312) + `src/llm_bench/ranking/per_stage_winners.py:81-114` | Per-stage winner promotion — рекламируемая ключевая фича — нигде не подключена к реальному раунд-раннеру; `load_winner_substrate`/`storage.load_winners` не вызываются никогда |
| Critical | `src/llm_bench/storage/memory.py:68-80`, `src/llm_bench/storage/postgres.py:225-256`, `src/llm_bench/storage/base.py:11-15` | `record_call` не может перезаписать провалившуюся строку успешным повтором — resume cache и ranking навсегда "залипают" на первой ошибке для InMemoryStorage и PostgresStorage |
| High | `src/llm_bench/storage/postgres.py:137-172` и 20 f-string SQL-сайтов | `schema_name` подставляется в SQL через f-string с `# nosec B608` без какой-либо валидации/allowlist — заявленная в docstring гарантия безопасности ничем не обеспечена в коде |
| High | `src/llm_bench/storage/file.py:130-176` vs `236-269` | У `FileStorage` после retry-after-failure `get_cached`/`prefetch_resume_cache` (SQLite-индекс) и `query_rows`/аналитика/ranking (JSONL, first-wins dedup) расходятся МЕЖДУ СОБОЙ — и оба расходятся с поведением Postgres/InMemory |
| High | `src/llm_bench/runner/round_runner.py:191,254,297,360` | Хардкод строки `"openrouter"` в generic-раннере ломает рекламируемую расширяемость через `provider_factory`: `RunRow.provider` и ключ resume-cache врут о реальном провайдере |
| Medium | `README.md:56` vs `src/llm_bench/runner/benchmark.py:206-213` | Quick-start в README вызывает `run_phase(tag=..., rounds=...)` без обязательного keyword-only `candidates` — скопированный пример падает с `TypeError` |
| Medium | `pyproject.toml:70` | Console-script `llm-bench = "llm_bench.cli.main:main"` указывает на несуществующий модуль (`src/llm_bench/cli/main.py` отсутствует) |
| Medium | `src/llm_bench/runner/round_runner.py:75-78,365-384`; `src/llm_bench/provider/__init__.py:1` | Контракт "провайдера" (`generate()` + ~14 `last_*`-атрибутов телеметрии) нигде не оформлен как `Protocol`, в отличие от всех остальных точек расширения фреймворка — чистый `getattr(..., None)` duck typing |
| Low | `README.md:26` | Ссылка на `docs/architecture.md` мёртвая — папки `docs/` в репозитории нет вообще |
| Low | `src/llm_bench/pool/__init__.py:1`, `src/llm_bench/stage/__init__.py:1` | В отличие от storage/cost/halving/ranking/runner, эти два `__init__.py` не ре-экспортируют свои Protocol-ы (`TaskPool`, `PromptBuilder`, ...) |
| Low | `src/llm_bench/cost/openrouter.py:157` | `OpenRouterCatalogue(ModelCatalogue)` явно наследуется от Protocol-класса, тогда как остальные backend-реализации (storage) — чисто структурная типизация; стилевая непоследовательность |
| Info | `tests/test_meta/test_no_import_cycles.py` + собственная проверка графа импортов | Слоистость core→pool/stage/cost/storage/provider→halving/ranking/discovery→confirmation/runner→cli реально соблюдена и защищена мета-тестом; нарушений не найдено — положительная находка |
| Info | `tests/test_meta/test_code_audit_baseline.py` | На чистом чекауте уже падает (`docstring_args_incomplete` в `cost/estimator.py:161`) — не по теме этого аудита, но означает, что CI сейчас red вне зависимости от находок ниже |

## Findings

### Finding 1 — Critical: per-stage winner promotion — мёртвый код, никогда не подключён к раннеру

**File:Line**: `src/llm_bench/runner/round_runner.py` (файл целиком, особенно `_run_pipeline` 168-312 и
`_build_prompt` 315-326), `src/llm_bench/runner/benchmark.py:259-297` (цикл `run_phase`),
`src/llm_bench/ranking/per_stage_winners.py:81-114` (`load_winner_substrate`).

**Описание**: README подаёт "per-stage winner promotion" как одну из пяти опорных возможностей
фреймворка ("cost-rank discovery + Sequential Halving + **per-stage winner promotion** + gold-anchored
ranking + cross-tag resume cache"). Идея документирована и в `core/types.py:239-254` (docstring
`WinnerSet`: "Round N+1 uses these as the substrate... Layer-2 cross-family preference"), и особенно
подробно в `ranking/per_stage_winners.py:1-18` — там прямым текстом описан ожидаемый механизм:
победитель этапа k из раунда N становится upstream-контекстом для ВСЕХ кандидатов раунда N+1 на этапе
k+1 (а не собственный вывод каждого кандидата на этапе k). Там же в docstring самой функции
`load_winner_substrate` (строки 89-98) явно написано: "The round driver invokes this BEFORE building any
candidate's prompt for stage k+1... **Without this wiring, "M_X promotion" is write-only and stage k+1
silently uses each candidate's own upstream output.**"

Именно это и происходит. `round_runner.py::run_round` действительно вызывает `storage.persist_winners(...)`
после каждого раунда (строки 160-164) — запись работает. Но чтение никогда не происходит:
- `_run_pipeline` (168-312) создаёт свежий `StageContext(task_unit=task_unit)` на каждую пару
  (model, task_unit) (строка 178) и заполняет `ctx.outputs[stage.id]` результатом парсинга ОТВЕТА ЭТОГО ЖЕ
  кандидата (строка 246: `ctx.outputs[stage.id] = parsed`, где `parsed` получен из ответа именно текущей
  модели). Далее `_build_prompt` (315-326) передаёт этот `ctx` промпт-билдеру следующего этапа как есть.
- Ни в `round_runner.py`, ни в `benchmark.py::run_phase` (259-297) нет ни одного вызова
  `load_winner_substrate`, ни вызова `storage.load_winners(...)` — оба идентификатора отсутствуют в файле
  целиком (проверено `inspect.getsource` в дискриминирующем repro-скрипте, см. ниже).
- `PromptBuilder` Protocol (`stage/base.py:47-57`) физически не имеет канала, через который сторонний
  промпт-билдер мог бы получить `WinnerSet`/storage-хендл сам — сигнатура ограничена
  `(task_unit, ctx, lang)`. То есть эту недостающую проводку невозможно реализовать даже на
  стороне потребителя без форка `round_runner.py`.

Функция `load_winner_substrate` при этом полностью реализована, покрыта unit-тестами
(`tests/unit/test_per_stage_winners.py:106-149`, все три сценария проходят) и экспортируется в публичном
API (`llm_bench/__init__.py:66,110`) — но вызывается только из собственного теста, нигде из runtime-пути.

Дискриминирующая проверка (запущена, вывод приведён без изменений):
```
=== [2] round_runner never calls load_winner_substrate ===
CONFIRMED: 'load_winner_substrate' and 'load_winners' do not appear anywhere in
           round_runner.py's source -- the round driver never reads back a WinnerSet
           to substitute the round's per-stage winner's output into ctx.outputs before
           building the next stage's prompt. persist_winners() is write-only.
```

Проверен и smoke-тест `tests/integration/test_smoke_in_memory.py::test_smoke_winner_persisted_per_round`
(204-235) — он лишь проверяет, что `storage.load_winners(...)` после раунда возвращает непустой
`WinnerSet`, но НЕ проверяет, что содержимое `ctx.outputs` на этапе k+1 у не-победителя соответствует
победителю, а не его собственному выводу. Поэтому существующий сьют не ловит разрыв.

**Почему это важно / сценарий проявления**: Ровно тот сценарий, который framework сам объясняет как
причину существования этой фичи (`per_stage_winners.py:9-12`): "if candidate Y is good at stage k+1 but
bad at stage k, using Y's bad-stage-k output as upstream context masks Y's actual stage k+1 ability."
В реальном многоэтапном пайплайне (VocabApp: enrich → validate_enrich → translate → validate_translate)
каждый выживший в раунде N+1 кандидат оценивается на СВОЁМ собственном (потенциально слабом) выводе
этапа k вместо эталонного вывода победителя — то есть Sequential Halving тихо теряет заявленное
"apples-to-apples" сравнение начиная со второго и далее этапов пайплайна, при этом ни исключения, ни
лога, ни падения теста не возникает. Это "silent wrong result in normal, non-adversarial operation" —
происходит при любом обычном многораундовом прогоне с зависимыми (`parent_stage`) этапами, что и есть
основной заявленный use-case фреймворка.

**Альтернативная трактовка**: можно предположить, что авторы сознательно отложили эту проводку до
будущего Phase и `load_winner_substrate` — задел на будущее. Но это не подтверждается: и README, и
docstring `WinnerSet`, и docstring `per_stage_winners.py` описывают механизм как ДЕЙСТВУЮЩИЙ, без
пометок "planned"/"TODO"/"Phase N", а сама функция `load_winner_substrate` уже полностью реализована и
протестирована — то есть работа сделана, забыт только последний шаг подключения к раннеру. Стоит
подтвердить у автора, было ли это сознательным откладыванием или реальным пропуском при рефакторинге.

**Recommendation**: в `_run_pipeline` перед вызовом `_build_prompt` для этапов с `stage.parent_stage is
not None` подставлять в `ctx.outputs[stage.parent_stage]` результат `load_winner_substrate(storage,
winners=<WinnerSet раунда N-1>, stage=parent_stage, task_unit_id=task_unit.id)` (распарсенный тем же
`parser`, что и обычный путь), с fallback на собственный вывод кандидата только когда substrate
отсутствует (первый раунд / partial round — как и описано в docstring). Дополнить
`test_smoke_in_memory.py` ассерцией, что `ctx.outputs` у не-победителя реально равен выводу победителя, а
не собственному — иначе регрессия останется незамеченной снова.

---

### Finding 2 — Critical: `record_call` не умеет перезаписать провалившуюся строку успешным повтором (InMemoryStorage, PostgresStorage)

**File:Line**: `src/llm_bench/storage/base.py:11-15` (контракт), `src/llm_bench/storage/memory.py:68-80`,
`src/llm_bench/storage/postgres.py:225-256` (конкретно `ON CONFLICT (composite_hash, provider, model,
thinking) DO NOTHING` на строках 235-236).

**Описание**: PK строки — `(composite_hash, provider, model, thinking)`, без номера попытки/времени.
Контракт в `storage/base.py:11-13` требует: "`record_call` is idempotent on (composite_hash, provider,
model, thinking) — same key re-recorded is a no-op (NOT a duplicate row)". Формулировка "same key
re-recorded" не делает разницы между "тот же результат записан повторно" и "другой (более свежий,
успешный) результат для того же ключа записан впервые после провала" — а `resume_cache.get(...)` явно
исключает провалившиеся строки из кэша (docstring invariant #2, строки 14-15: "они получают повторную
попытку" / "get re-tried"), то есть повторная попытка для того же ключа — штатный, ожидаемый сценарий,
а не аномалия.

`InMemoryStorage.record_call` (memory.py:68-80) реализует это буквально: `if key in self._rows: return
row.composite_hash` — если ключ уже существует (пусть даже с `error_class` от давнего провала), НОВАЯ
успешная запись безусловно отбрасывается, ничего не логируется. `PostgresStorage.record_call`
(postgres.py:225-256) делает то же самое через SQL: `ON CONFLICT (...) DO NOTHING` — тоже безусловно, вне
зависимости от того, была ли конфликтующая строка провалом или успехом. Для сравнения — `benchmark_winners`
в этом же файле (строка 393-395) сознательно использует `ON CONFLICT ... DO UPDATE SET
payload=EXCLUDED.payload`, то есть авторы явно умеют делать overwrite-семантику там, где она нужна —
но не сделали этого для `benchmark_results`.

Дискриминирующая проверка (запущена, вывод приведён без изменений — `InMemoryStorage`):
```
=== [1a] InMemoryStorage: failed row then successful retry, same PK ===
record_call returned: H1
get_cached after 'successful' retry -> None
query_rows -> [('RateLimited', None)]
CONFIRMED: InMemoryStorage permanently loses a successful retry after one prior failure.
```
(`PostgresStorage` не тестировался вживую — нет доступной БД — но SQL однозначен: `DO NOTHING` даёт
идентичный эффект без какой-либо ветвистости, которая могла бы его смягчить.)

Тест `tests/unit/test_storage_protocol.py::test_record_call_idempotent_same_key` (121-129) проверяет
ТОЛЬКО повторную запись ИДЕНТИЧНОЙ (оба раза успешной) строки — сценарий "провал → успешный повтор" в
сьюте не покрыт вовсе, поэтому баг живёт незамеченным.

**Почему это важно / сценарий проявления**: Ключевая фича фреймворка по README — "Resume cache (an
interrupted run resumes without re-paying for completed calls)". Реальный сценарий: LLM-вызов падает с
`RateLimited`/`LLMCallTimeout` (штатная, ожидаемая ситуация — под неё специально заведён
`TRANSIENT_ERROR_CLASSES` в `halving/alive_filter.py:79-90`), процесс перезапускают с тем же тегом.
`ResumeCache.populate_from_storage` (resume.py:45-60) корректно НЕ находит эту строку в кэше (провалы
исключены), пайплайн честно делает новый платный запрос к LLM, он успешен — но `record_call` этот успех
молча выбрасывает, потому что PK уже занят строкой-провалом. Результат:
1. `compute_ranking` (`ranking/ranker.py:161-165`, читает через `storage.query_rows`) при подсчёте
   score для этой (model, stage) продолжает видеть ту самую первую провальную строку — `score=0.0`
   навсегда, хотя модель реально успешно ответила. Это напрямую искажает выбор победителя этапа
   (`compute_ranking`) и решение Sequential Halving (`Halving.promote`, использует эти score) —
   потенциально хорошая модель получает "мёртвый" score и выбывает.
2. При КАЖДОМ следующем resume того же тега (или вообще любого тега — кэш tag-agnostic) framework будет
   заново тратить деньги на тот же LLM-вызов и заново терять результат — по сути ключ навсегда
   "протекает" бюджет без какой-либо пользы от resume cache, до тех пор пока не изменится текст промпта
   (что меняет `composite_hash`).

Это "silent wrong result in normal, non-adversarial operation" в самом буквальном смысле: ни исключения,
ни warning, просто тихая потеря данных и искажение ranking.

**Recommendation**: сделать `record_call` upsert-подобным для строк, у которых существующая запись имеет
`error_class` (провал), но новая — успех: `PostgresStorage` — `ON CONFLICT (...) DO UPDATE SET ... WHERE
benchmark_results.error_class IS NOT NULL` (или отдельный `UPDATE ... WHERE error_class IS NOT NULL`
перед `INSERT ... DO NOTHING`); `InMemoryStorage` — заменять значение в `self._rows[key]`, если
`self._rows[key].error_class` не пусто, а новый `row.error_class` пусто. Обновить контракт в
`storage/base.py` явно (текущая формулировка "same key re-recorded is a no-op" вводит в заблуждение
любого будущего автора 4-го backend-а — баг воспроизведётся заново, потому что это баг САМОГО
контракта, а не одной реализации). Добавить regression-тест "provider fails once, then succeeds на том
же PK" во все три backend-а через `test_storage_protocol.py`'s parametrized fixture.

---

### Finding 3 — High: `PostgresStorage.schema_name` — SQL-injection surface без валидации, несмотря на заявленную в docstring безопасность

**File:Line**: `src/llm_bench/storage/postgres.py:137-172` (class docstring + `__init__`), 20 f-string
SQL-сайтов по всему файлу (например 203, 209, 215, 230, 266, 295-297, 324, 342-343, 356-357, 392, 405,
431, 437).

**Описание**: `self._schema` (строка 164, из аргумента конструктора `schema_name: str = "llm_bench"`)
f-string-подставляется в имя таблицы КАЖДОГО запроса в файле, с комментарием `# nosec B608` на каждом
сайте и обоснованием в class docstring (146-152): "`self._schema` is set once at construction time from
an operator-supplied config value, not per-request user input, so this isn't the SQL-injection pattern
bandit's B608 heuristically flags". Проверено через `grep -n "schema_name|_schema\b"` по всему
репозиторию (не только storage/postgres.py) — нигде нет ни regex-проверки, ни `.isidentifier()`, ни
allowlist, ни любой другой формы валидации `schema_name`. Утверждение о безопасности — чисто
документационное обещание, не подкреплённое кодом.

Сам docstring этого же класса (144) прямо признаёт, что значение приходит "по конфигу" и меняется между
потребителями: "VocabApp overrides to `llm` for backwards compat with its current Postgres data)" — то
есть `schema_name` уже сегодня — не константа "llm_bench", а параметр, который реально прокидывается
через слой конфигурации хотя бы одного известного потребителя.

**Почему это важно / сценарий проявления**: Пока `schema_name` жёстко прописан литералом в исходниках
интеграции (как, судя по всему, сейчас и есть) — риска нет. Но: (а) framework — переиспользуемая
библиотека с несколькими текущими и будущими потребителями (задача явно упоминает "два reference
consumers... и неизвестные будущие"); (б) ничто в самом API `PostgresStorage.__init__` не мешает
потребителю прокинуть `schema_name=os.environ["PG_SCHEMA"]` или `schema_name=f"tenant_{tenant_id}"` в
multi-tenant сценарии — оба паттерна совершенно естественны и НЕ являются "по конфигу" нарушением с
точки зрения кода, который это разрешает. В обоих случаях строка с `'; DROP TABLE...` (или просто
пробел/спецсимвол, ломающий DDL при `initialize()`) пройдёт напрямую в `CREATE SCHEMA IF NOT EXISTS {s};`
(строка 43) и во все остальные запросы без единой проверки. Это ровно тот класс bandit-suppression,
который задание просило проверить независимо, а не принимать на веру — обоснование в docstring не
подтверждается кодом.

**Альтернативная трактовка**: если ЕДИНСТВЕННЫЙ реалистичный источник `schema_name` — буквальный литерал
в коде интеграции (как у нынешнего VocabApp), риск чисто теоретический, и docstring по сути верен для
сегодняшнего дня — стоит уточнить у автора, гарантируется ли это организационно (например, code review
запрещает прокидывать `schema_name` из внешнего input) или это просто пока никто не попробовал сделать
иначе.

**Recommendation**: добавить дешёвую валидацию в `__init__` (`if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
schema_name): raise ValueError(...)`, либо использовать `asyncpg`'s `quote_ident`/аналог) — это
одновременно закрывает и теоретическую инъекцию, и куда более вероятный класс багов "опечатка/пробел в
имени схемы ломает DDL с непонятной ошибкой на старте". Стоит нескольких строк кода и делает
docstring-обещание реально верным, а не декларативным.

---

### Finding 4 — High: `FileStorage` — кэш и аналитика расходятся между собой после retry-after-failure, и оба расходятся с Postgres/InMemory

**File:Line**: `src/llm_bench/storage/file.py:130-176` (`record_call`, индексация в SQLite строки
159-175), `src/llm_bench/storage/file.py:236-269` (`query_rows`, dedup "первый встреченный побеждает" —
строки 258-264).

**Описание**: В отличие от InMemoryStorage/PostgresStorage (Finding 2), `FileStorage.record_call`
проверяет идемпотентность ТОЛЬКО по SQLite-индексу `resume_cache` (строки 141-148), а в этот индекс
попадают ТОЛЬКО успешные строки (159: `if not row.error_class and row.response and len(row.response) >=
20`). Поэтому после провала тот же PK в индексе отсутствует, и следующий успешный retry ЗАПИСЫВАЕТСЯ —
и в JSONL (append), и в индекс. То есть, в отличие от InMemory/Postgres, `FileStorage` действительно
СОХРАНЯЕТ успешный повтор — но только для read-путей, завязанных на SQLite-индекс
(`get_cached`/`prefetch_resume_cache`). Read-путь, завязанный на JSONL (`query_rows`, а через него —
`query_spend_total`/`query_spend_by_stage`/`query_spend_by_op`/`delete_experiment`/`compute_ranking`),
дедуплицирует построчно "первый встреченный ключ побеждает" (258-264: `if key in seen: continue`) — а
поскольку JSONL append-only и пишется в хронологическом порядке, "первый встреченный" — это САМАЯ
СТАРАЯ (провальная) попытка, а не свежая успешная.

Дискриминирующая проверка (запущена, вывод приведён без изменений):
```
=== [1b] FileStorage: failed row then successful retry, same PK ===
get_cached (SQLite index) -> CachedResponse(composite_hash='H1', response='{"result": "this is a real successful response!!"}', ...)
query_rows (JSONL, first-wins dedup) -> [('RateLimited', None)]
CONFIRMED: FileStorage internally diverges -- resume cache sees the fresh success,
           but query_rows/analytics/ranking still see the stale FAILED row (first-wins).
```

**Почему это важно / сценарий проявления**: Это прямое нарушение принципа "consumer swaps FileStorage
for PostgresStorage expecting identical guarantees", о котором явно предупреждает сам
`storage/base.py:1-24` ("Three implementations must satisfy this contract"). При использовании
FileStorage сценарий "провал → успешный resume" даёт КАЧЕСТВЕННО ДРУГОЕ поведение, чем на
Postgres/InMemory: сама LLM-стоимость больше не теряется зря на каждом resume (кэш реально работает) —
но `compute_ranking`, `query_spend_*` и `delete_experiment` всё равно молча видят и используют
устаревшую провальную строку. Практическое следствие: на FileStorage модель, которая упала один раз, а
потом стабильно отвечала успешно, будет НАВСЕГДА получать `score=0.0` за тот конкретный
(task_unit, stage) в ranking (тот же механизм, что в Finding 2, только "снизу" картина иная — spend
корректно не задваивается, потому что сумма считается по первой, а не по обеим строкам, но и корректный
успешный ответ для ranking недостижим). Консультант, ожидающий "тот же контракт что у Postgres",
получит на FileStorage не более похожий, а просто ПО-ДРУГОМУ неверный результат.

**Recommendation**: тот же fix, что и в Finding 2 (перезаписывать провальную строку успешным повтором),
но здесь дополнительно нужно синхронизировать поведение `query_rows`'s dedup с ролью источника истины:
либо dedup должен брать ПОСЛЕДНЮЮ (не первую) запись по ключу, либо — раз уж SQLite-индекс уже хранит
единственную актуальную версию каждого успешного ключа — `query_rows` для успешных ключей стоит сверяться
с индексом, а не полагаться только на порядок строк в JSONL.

---

### Finding 5 — High: хардкод `"openrouter"` в generic-раннере ломает заявленную расширяемость через `provider_factory`

**File:Line**: `src/llm_bench/runner/round_runner.py:191`, `:254`, `:297`, `:360`.

**Описание**: `Benchmark.provider_factory` (`runner/benchmark.py:79,95`) документирован как точка
расширения: "defaults to `pyutilz.llm.get_llm_provider`; override for tests **with a fake provider**" —
и действительно принимает произвольный callable `(model_id) -> Any с async .generate(...)`. Однако
внутри `_run_pipeline` (являющегося "generic"-исполнителем пайплайна — задание прямо просило искать
именно такие протечки) провайдер-идентификатор ЗАПИСЫВАЕТСЯ буквальной строкой `"openrouter"` независимо
от того, что реально вернул `provider_factory`:
- строка 191: ключ поиска в resume-кэше `cfg.resume_cache.get(composite_hash=comp_h,
  provider="openrouter", ...)`
- строка 254: `RunRow(... provider="openrouter", ...)` — то, что реально уходит в storage
- строка 297: `cfg.resume_cache.put(..., provider="openrouter", model=model)`
- строка 360 — единственное МЕСТО, где `"openrouter"` уместен по смыслу: это дефолтный путь БЕЗ
  `provider_factory` (`get_llm_provider("openrouter", model=model)`) — но остальные три сайта
  срабатывают ВСЕГДА, включая случай, когда `provider_factory` подставлен и реально дергает что-то
  другое.

`RunRow.provider` — часть составного PK записи (`core/types.py:186-190`, docstring: "Identity:
(composite_hash, provider, model, thinking)") — то есть поле задумано как значащее измерение identity, а
не декоративное. Формальный `Protocol` для провайдера не существует нигде (`provider/__init__.py` —
пустой докстринг-стаб), поэтому ничто в типах не заставляет автора кастомного `provider_factory` вообще
задуматься о том, что framework запишет "openrouter" вместо истины.

**Почему это важно / сценарий проявления**: пункт "how easy would it genuinely be to add a
non-OpenRouter provider" из задания — ответ: формально легко (просто дать другой `provider_factory`), но
результат будет молча НЕВЕРНЫМ. Персистентные строки во всех трёх storage-бэкендах будут утверждать
`provider="openrouter"` даже когда реальный вызов шёл, например, через прямой Anthropic-клиент или
внутренний LLM-роутер — искажая любую последующую аналитику/аудит по колонке `provider`. Более
серьёзный побочный эффект: если в одном и том же процессе/тэге когда-либо реально смешиваются OpenRouter
и non-OpenRouter вызовы с одинаковым `model` id и идентичным текстом промпта (тот же `composite_hash`),
composite-ключ `(composite_hash, "openrouter", model, thinking)` СОВПАДЁТ для обоих — и один провайдер
получит из resume-кэша ответ, реально сгенерированный другим. Это узкий (нужны совпадающие id моделей
между провайдерами и идентичный текст промпта), но реальный collision-риск, вытекающий именно из
недобросовестного хардкода, а не из дизайна PK как такового.

**Альтернативная трактовка**: возможно, `provider_factory` задуман исключительно как инструмент
подмены HTTP-транспорта ДЛЯ ТЕСТОВ (как буквально сказано в docstring: "override for tests with a fake
provider"), а не как настоящая точка мультипровайдерного расширения "в проде" — тогда хардкод не баг, а
осознанное упрощение на alpha-стадии. Но тогда сам факт, что `RunRow.provider` — часть PK и заявлен как
identity-поле, а `preflight()` (`benchmark.py:122-204`) тоже полностью полагается на произвольный
`provider_factory` как на боевой путь — говорит скорее в пользу того, что мультипровайдерность
задумывалась всерьёз, просто эта деталь не была доведена до конца.

**Recommendation**: либо (а) прокидывать реальный провайдер-идентификатор через `provider_factory`'s
результат (например, обязать возвращаемый объект иметь `.provider_name` атрибут, дефолтящийся к
`"openrouter"`), либо (б) явно задокументировать и в docstring `provider_factory`, и в README, что на
сегодняшний день framework жёстко моно-провайдерный по OpenRouter, а `provider_factory` — только про
подмену транспорта для тестов, не про мультипровайдерность.

---

### Finding 6 — Medium: README quick-start вызывает `run_phase()` без обязательного `candidates` — сломанный copy-paste

**File:Line**: `README.md:38-57` (конкретно строка 56), `src/llm_bench/runner/benchmark.py:206-213`.

**Описание**: README:
```python
report = await bench.run_phase(tag="exp_v1", rounds=[1, 2, 3])
```
Сигнатура `run_phase` (`benchmark.py:206-213`):
```python
async def run_phase(
    self, *, tag: str | ExperimentTag, candidates: list[str],
    rounds: list[int] | None = None, units: list[TaskUnit] | None = None,
    preflight: bool = False,
) -> PhaseReport:
```
`candidates` — keyword-only, без значения по умолчанию, стоит сразу за `tag`. Вызов, буквально
скопированный из README, гарантированно упадёт с `TypeError: run_phase() missing 1 required
keyword-only argument: 'candidates'`. Для сравнения — реальный референс-пример `examples/job_app_
cover_letter/run.py:172` вызывает корректно: `await bench.run_phase(tag=tag, candidates=candidates)`, то
есть актуальный API известен авторам, просто README не был синхронизирован после появления обязательного
параметра.

**Почему это важно**: это флагманский, первый пример, который увидит любой новый пользователь при
установке пакета — если его буквально скопировать, он не работает. Не влияет на логику самого фреймворка,
чисто doc-drift, но напрямую бьёт по "первому впечатлению" и доверию к остальной документации.

**Recommendation**: добавить `candidates=[...]` в пример README (значение можно взять из соседнего
`CostFilter`/discovery-раздела, которого пока в README тоже нет — см. Finding 9 по духу).

---

### Finding 7 — Medium: console-script entry point `llm-bench` ссылается на несуществующий модуль

**File:Line**: `pyproject.toml:70`.

**Описание**: `[project.scripts]` объявляет `llm-bench = "llm_bench.cli.main:main"`. Проверено —
`src/llm_bench/cli/main.py` не существует (`os.path.exists` → `False`), `src/llm_bench/cli/__init__.py`
(единственный файл в пакете) содержит только `"""cli subpackage."""`. При `pip install llm-bench`
setuptools честно создаст исполняемый скрипт `llm-bench`, который при запуске упадёт с
`ModuleNotFoundError: No module named 'llm_bench.cli.main'`.

**Почему это важно**: единственное место в репозитории (проверено grep по всему дереву, включая
README/examples/tests), где вообще упоминается `llm_bench.cli.*` — это эта строка в `pyproject.toml`.
README ничего не говорит о CLI-использовании вообще (Quick start — чисто программный API), так что
пользовательского обещания как такового нет — но пакетный уровень (`pip install` + entry point) обещание
всё равно даёт неявно самим фактом регистрации команды.

**Recommendation**: либо удалить `[project.scripts]` до появления реальной CLI-реализации (честнее для
alpha-статуса), либо на скорую руку добавить `cli/main.py` с минимальным `main()` (даже
`raise SystemExit("llm-bench CLI is not implemented yet — use the Python API, see README")` было бы
честнее текущего состояния, где команда падает с внутренним `ModuleNotFoundError`).

---

### Finding 8 — Medium: контракт "провайдера" — неформальный duck-typing без `Protocol`, в отличие от остальных точек расширения

**File:Line**: `src/llm_bench/runner/round_runner.py:75-78` (docstring `provider_factory`), `:356-360`
(`_call_llm`, вызов `provider.generate(...)`), `:364-384` (14 `getattr(provider, attr, None)` для
телеметрии), `src/llm_bench/provider/__init__.py:1` (пустой stub).

**Описание**: Каждая другая точка расширения фреймворка формально оформлена как `@runtime_checkable
Protocol` c явной сигнатурой: `TaskPool` (`pool/base.py`), `PromptBuilder`/`ResponseParser`/`GoldChecker`
(`stage/base.py`), `ModelCatalogue` (`cost/catalogue.py`), `BenchmarkStorage` (`storage/base.py`),
`RowScorer` (`ranking/ranker.py`). Провайдер — единственное исключение: ожидаемая форма объекта
(`async generate(prompt=..., system=...) -> str`, плюс до 14 опциональных `last_*`-атрибутов телеметрии:
`last_input_tokens`, `last_cost_usd`, `last_cache_discount_usd`, ...) существует только в виде
свободного текста в docstring (`round_runner.py:75-78`) и в самом коде через `getattr(provider, attr,
None)` (365-384) — ни один атрибут не типизирован, ни один не обязателен, отсутствие любого просто
молча даёт `None`/пропуск ключа в телеметрии. Пакет `provider/`, судя по названию и по соседству с
`pool/`/`stage/` (у которых есть реальный `base.py`), явно задумывался как место для этого Protocol —
но остался пустым stub-докстрингом.

**Почему это важно**: если pyutilz когда-нибудь переименует/уберёт один из этих `last_*` атрибутов
(это внешняя, невидимая отсюда библиотека — ровно та зависимость, о клиентской хрупкости которой
просило задание), либо если консьюмер напишет свой `provider_factory`, слегка ошибившись в имени
атрибута — никакой ошибки не будет: `getattr(..., None)` тихо даст `None`, соответствующее поле
`RunRow` останется пустым, и никто не узнает о рассинхронизации до тех пор, пока кто-то вручную не
заметит дыры в аналитике cost/latency.

**Recommendation**: формализовать `provider/base.py` как `@runtime_checkable Protocol` с обязательным
`generate()` и опциональными `last_*`-полями (как минимум для документационной ценности — mypy всё равно
не сможет проверить duck-typed `getattr`, но явный Protocol зафиксирует контракт в одном месте вместо
разбросанного по докстрингам текста, и даст `isinstance()`-проверку в `preflight()`/тестах, как это уже
сделано для остальных пяти точек расширения).

---

### Finding 9 — Low: README ссылается на несуществующий `docs/architecture.md`

**File:Line**: `README.md:26`.

**Описание**: `` `0.1.0` — alpha. API is stabilizing. See [docs/architecture.md](docs/architecture.md). ``
— в репозитории нет директории `docs/` вообще (проверено `find . -maxdepth 1 -type d` — только
`.benchmarks .git .github .mypy_cache .pytest_cache .ruff_cache audits examples src tests`).

**Recommendation**: либо написать `docs/architecture.md` (материала для него в самом коде уже
достаточно — docstring-и `halving/driver.py`, `ranking/per_stage_winners.py`, `storage/base.py` фактически
уже являются черновиком архитектурного описания), либо убрать битую ссылку из README до появления файла.

---

### Finding 10 — Low: `pool/__init__.py` и `stage/__init__.py` не ре-экспортируют свои Protocol-ы

**File:Line**: `src/llm_bench/pool/__init__.py:1`, `src/llm_bench/stage/__init__.py:1`.

**Описание**: Оба файла содержат только однострочный докстринг (`"""pool subpackage."""` /
`"""stage subpackage."""`), тогда как `storage/__init__.py`, `cost/__init__.py`, `halving/__init__.py`,
`ranking/__init__.py`, `runner/__init__.py` — все явно ре-экспортируют публичные имена своих submodule'ей
(проверено grep импортов). При этом `pool/base.py` и `stage/base.py` содержат реальные, часто
используемые `Protocol`-ы (`TaskPool`, `PromptBuilder`, `ResponseParser`, `GoldChecker`). В результате
`from llm_bench.pool import TaskPool` / `from llm_bench.stage import PromptBuilder` не сработают (нужно
`from llm_bench.pool.base import TaskPool`), хотя по аналогии с остальными пятью subpackage'ами
пользователь ожидал бы единообразия. Верхнеуровневый `llm_bench/__init__.py:22-23` эти имена
ре-экспортирует корректно, так что стандартный `from llm_bench import TaskPool` работает — баг
затрагивает только прямой импорт из subpackage.

**Recommendation**: добавить в оба `__init__.py` те же ре-экспорты, что уже есть в остальных пяти
subpackage'ах, для единообразия API-поверхности.

---

### Finding 11 — Low: `OpenRouterCatalogue` явно наследуется от Protocol-класса, в отличие от storage-бэкендов

**File:Line**: `src/llm_bench/cost/openrouter.py:157`.

**Описание**: `class OpenRouterCatalogue(ModelCatalogue):` — явное наследование от
`@runtime_checkable Protocol`. Все три storage-бэкенда (`FileStorage`, `InMemoryStorage`,
`PostgresStorage`) реализуют `BenchmarkStorage` исключительно структурно, без наследования (проверяется
только через `isinstance(storage, BenchmarkStorage)` в тестах, `test_storage_protocol.py:100-102`).
Работает в обоих случаях одинаково (Python allows both), но стилистическая непоследовательность в
рамках одного и того же Protocol-based подхода.

**Recommendation**: не критично, но для единообразия стоит убрать явное наследование
(`class OpenRouterCatalogue:` без родителя) — либо, наоборот, задокументировать, почему здесь сделано
иначе.

---

### Finding 12 — Info: слоистость импортов реально соблюдена (позитивная находка)

**File:Line**: `tests/test_meta/test_no_import_cycles.py:1-113` + независимая проверка.

**Описание**: Заявленная в задании модель `core → pool/stage/cost/storage/provider → halving/ranking/
discovery → confirmation/runner → cli` фактически соблюдена без единого нарушения. Независимо (grep
`^from llm_bench` по каждому файлу `src/llm_bench/**/*.py`) построен полный граф внутренних импортов —
циклов и upward/sibling-нарушений не найдено; конкретно `core/types.py` и `core/scoring.py` (нижний
слой) не импортируют ничего из соседних subpackage'ов, как и требует их собственный docstring
(`core/types.py:3-6`). Мета-тест `test_no_upward_imports` (структурный AST-анализ, не требует
установленных опциональных зависимостей) активно защищает эту инвариант в CI. Отдельно стоит отметить:
`cost/catalogue.py` (Protocol) и `cost/openrouter.py` (конкретная OR-реализация) корректно разделены —
все OR-специфичные детали (форма JSON от OpenRouter API, нормализация uptime, выбор best upstream)
изолированы в `cost/openrouter.py` и не протекают в `cost/estimator.py`/`cost/filter.py`, которые
работают исключительно через `CatalogueEntry`/`ModelCatalogue` Protocol. Это хороший пример того, как
должна была бы выглядеть провайдер-независимость (см. Finding 5 про обратный пример).

---

### Finding 13 — Info: `test_code_audit_baseline` уже красный на чистом чекауте (вне категории этого аудита)

**File:Line**: `tests/test_meta/test_code_audit_baseline.py` (запуск), находка указывает на
`src/llm_bench/cost/estimator.py:161`.

**Описание**: `python -m pytest -m "not live" --no-cov -q` на чистом дереве без каких-либо правок
падает первым же прогоном на:
```
Failed: 1 new static-analysis finding(s) from pyutilz.dev.code_audit ...
  docstring_args_incomplete [Low] cost/estimator.py:161 -- `estimate_top_n_by_cost`'s docstring has an
  Args: section but omits parameter(s) ['refresh'] -- a caller reading the docstring has no idea these
  exist.
```
Это документационная (docstring-completeness) находка вне рамок категории "Архитектура и дизайн" —
фиксирую только как факт: офлайн-CI-сьют на этот момент НЕ зелёный независимо от чего-либо в этом
отчёте, остальные 121 теста проходят (см. Scope & method). Не разбиралось глубже — не в моей категории;
передаю на заметку тому, кто ведёт hygiene/lint-категорию аудита.

## Итог по категории

Слоистость модулей (core/cost/halving/pool/ranking/runner/stage/storage) спроектирована аккуратно и
реально не нарушается (Finding 12) — это сильная сторона. Но при близком чтении обнаружились два
по-настоящему серьёзных разрыва между тем, что framework заявляет о себе (в README и docstring-ах) и
тем, что реально происходит в runtime: per-stage winner promotion — нигде не подключённый мёртвый код
(Finding 1), и resume-cache/idempotency-контракт storage-слоя (Finding 2, 4), который тихо теряет
успешные повторные попытки и постоянно искажает ranking. Оба воспроизведены дискриминирующими
скриптами, оба относятся к штатной, ожидаемой эксплуатации (не к edge-case или adversarial input) —
поэтому оба Critical. Расширяемость по storage-бэкендам архитектурно проста (~13 async-методов
Protocol), но Finding 2 показывает, что сам Protocol-контракт содержит скрытый баг, который
добросовестно воспроизведёт любой будущий 4-й backend, если следовать текущей формулировке docstring
буквально. Расширяемость по провайдеру, вопреки заявленному дизайну (`provider_factory`), на практике
подрывается хардкодом `"openrouter"` (Finding 5) и отсутствием формального Protocol (Finding 8).
