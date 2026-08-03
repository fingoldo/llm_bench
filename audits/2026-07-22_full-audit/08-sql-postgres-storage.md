# Аудит: SQL и Postgres storage backend

## Scope & method

**Прочитано целиком:**
- `src/llm_bench/storage/postgres.py` (508 строк, весь файл)
- `src/llm_bench/storage/base.py` (Protocol, весь файл)
- `src/llm_bench/storage/memory.py`, `src/llm_bench/storage/file.py` (для сравнения контракта `record_call`/`prefetch_resume_cache` между тремя бэкендами)
- `src/llm_bench/core/types.py` (`RunRow`, `WinnerSet` — доменные типы, соответствие DDL)
- `src/llm_bench/runner/round_runner.py`, `src/llm_bench/runner/resume.py`, `src/llm_bench/runner/budget.py` (кто и как вызывает Storage-методы, какой concurrency-профиль)
- `src/llm_bench/ranking/ranker.py`, `src/llm_bench/ranking/per_stage_winners.py` (фактические вызовы `query_rows`)
- `tests/unit/test_storage_protocol.py`, `tests/conftest.py` (что реально тестируется)
- `pyproject.toml` (секции `[project.optional-dependencies].postgres`, `[tool.deptry]`)
- `examples/job_app_cover_letter/run.py` (единственный in-repo потребитель — использует `FileStorage`, не Postgres)

**Выполненные команды (read-only):**
- `python -c "import asyncpg; ..."` — версия установленного asyncpg (0.31.0) и инспекция исходников `asyncpg/cursor.py`, `asyncpg/connection.py` (проверка поведения `Connection.cursor()` вне транзакции — см. Finding 1)
- `python -m bandit -r src/llm_bench/storage/postgres.py -q` (полный вывод, без `-ll` фильтра, чтобы увидеть все severity, включая то, что реальный CI-гейт отфильтровывает)
- `python -m ruff check src/llm_bench/storage/postgres.py` → "All checks passed!"
- `python -m mypy src/llm_bench/storage/postgres.py --ignore-missing-imports` → "Success: no issues found"
- `grep`/`Glob` по всему репо на `alembic`, `migrations/`, `PostgresStorage(`, `schema_name` — подтверждение отсутствия миграционной инфраструктуры и отсутствия каких-либо in-repo вызовов конструктора с нестандартным `schema_name`
- Проверка `tests/test_meta/_code_audit_baseline.json` — единственная запись про `postgres.py` (`default_via_or::storage/postgres.py:227`) не пересекается ни с одной находкой ниже

Ruff/mypy по этому файлу чисты — все находки ниже про то, что статические тулы этого класса не ловят: логика идемпотентности, реальное поведение asyncpg API, соответствие индексов запросам, атомарность многошаговых операций, миграционная история.

## Summary table

| Severity | File:Line | Summary |
|---|---|---|
| Critical | postgres.py:275-286 | `prefetch_resume_cache` вызывает `conn.cursor()` без `conn.transaction()` — гарантированно падает с `asyncpg.exceptions.NoActiveSQLTransactionError` при любом реальном вызове |
| Critical | postgres.py:229-256 | `record_call`'s `ON CONFLICT DO NOTHING` навсегда теряет результат успешного повтора вызова, если под тем же PK уже лежит провалившаяся строка — тихо ломает resume-cache и учёт бюджета |
| High | postgres.py:179-183 | Нет `command_timeout`/`timeout` при создании пула — зависший запрос может держать соединение вечно; пул (по умолчанию 8) может быть полностью исчерпан без самовосстановления |
| High | postgres.py:39-120; pyproject.toml:47-50,216-218 | Нет пути миграции (только `CREATE ... IF NOT EXISTS`), `alembic` заявлен как зависимость, но `alembic/`-каталога нет нигде в репо, и это противоречит собственному докстрингу `postgres.py:14-17` ("No alembic dep") |
| High | postgres.py:155-167, 203-437 | `schema_name` не валидируется в конструкторе, но f-string-интерполируется в 13 местах с `# nosec B608`; ни один вызов `PostgresStorage(...)` нигде в репо не использует нестандартный `schema_name` — заявление "operator-supplied, not per-request" ничем не подкреплено в коде и не проверено ничем в этом репозитории |
| High | postgres.py:260-287 | `prefetch_resume_cache` не имеет поддерживающего индекса под свой фильтр и не ограничивает выборку — полный seq scan растущей без границ таблицы плюс безлимитная загрузка всех строк (включая тела `response`) в память при каждом старте прогона |
| High | postgres.py:425-441 | Два `DELETE` в `delete_experiment` не обёрнуты в общую транзакцию — падение процесса между ними оставляет осиротевшие строки `benchmark_winners` |
| Medium | postgres.py:67-106 | Нет `FOREIGN KEY` от `benchmark_results.composite_hash` к `benchmark_prompts.composite_hash`, хотя соседние `system_hash`/`user_hash` в том же DDL-блоке имеют `REFERENCES` |
| Medium | postgres.py:160-166; round_runner.py:71; budget.py:63 | `max_connections=8` по умолчанию явно меньше, чем `global_concurrency=30` по умолчанию в `RoundConfig`, и это усугубляется тем, что `query_spend_by_op` дергается перед каждым бюджетируемым вызовом |
| Medium | postgres.py:108-109 | `idx_benchmark_results_stage` как одиночный индекс не соответствует реальному паттерну запросов (`stage` всегда идёт вместе с `experiment_tag`) |
| Medium | postgres.py:110 | `idx_benchmark_results_task_unit` не используется ни одним запросом в этом файле |
| Medium | postgres.py:73; core/types.py:193 | `task_unit_id` в БД nullable, хотя в доменной модели `RunRow.task_unit_id: str` — обязательное поле |
| Medium | postgres.py:67-106 | Нет `CHECK`-констрейнтов против отрицательных cost/token значений |
| Low | postgres.py:199,226,263,292,322,339,352,381,402,428 | `assert self._pool is not None` как единственная защита от вызова до `initialize()` — пропадает под `python -O`; технически это bandit B101, но реальный CI-гейт (`bandit -r src/llm_bench -ll`) фильтрует Low-severity и не блокирует на этом |
| Low | postgres.py:81-96 | `NUMERIC`-колонки для денег/длительностей — правильный выбор типа, но значения приходят как нативные Python `float` (`RunRow: float \| None`) без `Decimal`, так что реальной точности это не даёт |
| Info | tests/unit/test_storage_protocol.py:33-40 | `PostgresStorage` не участвует ни в одном тесте контракта в этом репо (параметризация исключает Postgres до "Phase D follow-up", которого не существует); ни один example не создаёт `PostgresStorage` — этим объясняется, почему Critical Finding 1 не был замечен |

## Findings

### Finding 1 — `prefetch_resume_cache` гарантированно падает на реальном Postgres (Critical)

**File:Line:** `src/llm_bench/storage/postgres.py:275-286`

```python
async with self._pool.acquire() as conn:
    async for r in conn.cursor(sql, *params):
        out[(...)] = CachedResponse(...)
```

**Описание.** Метод открывает server-side cursor через `conn.cursor(sql, *params)` и итерирует его `async for` **без** обёртки `async with conn.transaction():`. Я проверил это не по документации, а по исходникам установленного в этом окружении `asyncpg==0.31.0`:

```
asyncpg/cursor.py:105-116
    def _check_ready(self):
        ...
        if not self._connection._top_xact:
            raise exceptions.NoActiveSQLTransactionError(
                'cursor cannot be created outside of a transaction')
```

`_top_xact` выставляется исключительно в `Transaction.start()` (`asyncpg/transaction.py:105-110`), то есть только через `conn.transaction()`; голый `pool.acquire()` его не трогает. Это значит, что **при любом реальном вызове** `prefetch_resume_cache` против настоящего Postgres будет выброшено `asyncpg.exceptions.NoActiveSQLTransactionError`.

Показательно, что чуть ниже в этом же файле `query_rows` (строки 331-334) делает ровно то же самое (`conn.cursor()` + `async for`), но **правильно**, обернув итерацию в `async with conn.transaction():` — то есть паттерн в кодовой базе известен, просто пропущен именно здесь.

**Почему это важно / сценарий отказа.** Согласно докстрингу `storage/base.py:76-89` и `runner/resume.py:9-10,45-60`, `prefetch_resume_cache` — это единственный способ загрузить resume-cache при старте прогона (`ResumeCache.populate_from_storage`, вызывается один раз в начале). `populate_from_storage` вызывает `storage.prefetch_resume_cache(...)` без `try/except` (resume.py:53-57) — исключение полетит наверх без перехвата. То есть **любой реальный запуск с `PostgresStorage` падает на старте**, ещё до первого LLM-вызова. Резюм-кэш — центральная фича фреймворка ("cross-tag resume cache so an interrupted run doesn't re-pay for completed calls" — из описания репозитория), и для Postgres-бэкенда она полностью нерабочая в текущем виде.

Это не было поймано ни одним тестом: `tests/unit/test_storage_protocol.py:33-40` явно исключает Postgres из параметризации ("PostgresStorage parametrization gated on LLM_BENCH_TEST_DB_URL — see test_storage_protocol_postgres.py (Phase D follow-up)"), а такого файла в репозитории не существует (проверено — `Glob` не находит). Ни один `examples/*` тоже не создаёт `PostgresStorage` (только `FileStorage`, `examples/job_app_cover_letter/run.py:136`).

**Recommendation.** Обернуть блок в `async with conn.transaction():`, как уже сделано в `query_rows`:
```python
async with self._pool.acquire() as conn:
    async with conn.transaction():
        async for r in conn.cursor(sql, *params):
            out[...] = CachedResponse(...)
```
И обязательно добавить реальный (не заглушенный) интеграционный тест против настоящего Postgres (`LLM_BENCH_TEST_DB_URL`), который бы поймал это за секунды — сейчас `PostgresStorage` в этом репозитории вообще ни разу не исполнялся против живой БД.

---

### Finding 2 — `ON CONFLICT DO NOTHING` в `record_call` навсегда хоронит успешный повтор ранее провалившегося вызова (Critical)

**File:Line:** `src/llm_bench/storage/postgres.py:229-256`, контракт — `storage/base.py:66-73,84-89`

**Описание.** PK строки — `(composite_hash, provider, model, thinking)` (постоянный, не зависит от попытки/раунда/тега). `record_call` пишет:
```sql
INSERT INTO ... VALUES (...) ON CONFLICT (composite_hash, provider, model, thinking) DO NOTHING
```
Сценарий: первая попытка вызова модели по конкретному промпту падает (rate-limit, timeout, parse-error) → строка с `error_class` уже занимает этот PK. Согласно самому контракту (`storage/base.py:14-15`: "Failed rows... are EXCLUDED from prefetch_resume_cache and get_cached — they get re-tried"), при следующем прогоне (тот же или другой `experiment_tag` — кэш tag-agnostic) `resume_cache.get(...)` даёт miss, и `round_runner._run_pipeline` (round_runner.py:220-234) делает реальный, платный LLM-вызов заново. Если на этот раз вызов **успешен**, `record_call` пытается вставить новую (успешную) строку под тем же PK — но строка там уже есть (провалившаяся), и `DO NOTHING` тихо отбрасывает новый, успешный результат. Старая провалившаяся строка остаётся единственным persisted состоянием навсегда.

Я сверил это с `storage/base.py:66-73` ("Idempotent... same key re-recorded is a no-op") — формально `DO NOTHING` соответствует букве контракта, но противоречит его же явно описанному поведению для failed rows ("they get re-tried" — подразумевается, что retry должен куда-то деться, а не потеряться).

Полезное сравнение — `persist_winners` в этом же файле (postgres.py:391-396) делает это правильно: `ON CONFLICT (...) DO UPDATE SET payload=EXCLUDED.payload, ts=NOW()`. Для `benchmark_winners` overwrite обоснован; для `benchmark_results` он не сделан вовсе, хотя ретрай именно failed→succeeded строк — штатный, документированный сценарий этого фреймворка.

Также я сравнил три бэкенда: `storage/memory.py` (InMemoryStorage) имеет тот же баг — комментарий "matches Postgres ON CONFLICT DO NOTHING semantics" на строке 71-72 подтверждает, что это осознанное кросс-бэкендное решение, а не оплошность одного файла. У `storage/file.py`, по случайности архитектуры (отдельный SQLite resume-index, куда failed-строки **не** попадают, `file.py:156-158`), идемпотентность проверяется именно по индексу "уже успешно закэшировано", а не "PK уже существует где-либо" — поэтому у FileStorage повторный успешный вызов после провала реально попадает в индекс (хотя это создаёт свой отдельный баг дедупликации в `query_rows`, вне scope этого аудита). Это показывает, что дизайн Postgres-варианта можно исправить, не нарушая общий Protocol-контракт.

**Почему это важно / сценарий отказа.** 
1. **Учёт затрат тихо занижается.** Успешный (оплаченный) повторный вызов не попадает в `benchmark_results`, значит `query_spend_total`/`query_spend_by_stage`/`query_spend_by_op` (используются `BudgetGate.check`, `runner/budget.py:63`) никогда не увидят эти реальные траты — бюджетный гейт систематически недооценивает потраченное.
2. **Resume-cache никогда не самовосстанавливается.** На следующем прогоне тот же вызов снова будет считаться failed → снова retry → снова успешный результат теряется. Это не разовая потеря, а перманентная утечка: фреймворк будет **платить за один и тот же вызов повторно на каждом последующем прогоне**, прямо противореча заявленной цели resume-cache.
3. Это происходит в штатной, неадверсариальной эксплуатации — ретрай после транзиентной сетевой ошибки абсолютно реалистичен и ожидаем самим фреймворком (`runner/classify.py`, `DEAD_ERROR_CLASSES` в halving/ — вся эта инфраструктура классификации ошибок существует именно для того, чтобы отличать retry-able ошибки от фатальных).

**Recommendation.** Заменить на условный upsert, который перезаписывает только когда прежняя строка была неуспешной (не трогая уже успешно закэшированные строки, чтобы сохранить детерминированность кэша):
```sql
INSERT INTO ... VALUES (...)
ON CONFLICT (composite_hash, provider, model, thinking) DO UPDATE
SET response = EXCLUDED.response,
    error_class = EXCLUDED.error_class,
    error_message = EXCLUDED.error_message,
    cost_usd = EXCLUDED.cost_usd,
    effective_cost_usd = EXCLUDED.effective_cost_usd,
    ... ,
    ts = EXCLUDED.ts
WHERE benchmark_results.error_class IS NOT NULL AND benchmark_results.error_class <> ''
```
Это же нужно исправить в `storage/memory.py` (тот же паттерн), т.к. проблема — контрактная, не только Postgres-специфичная, хотя моя формальная зона аудита — postgres.py.

---

### Finding 3 — Нет command/acquire timeout на пуле соединений (High)

**File:Line:** `src/llm_bench/storage/postgres.py:175-183`

```python
self._pool = await asyncpg.create_pool(
    dsn=self._url,
    min_size=self._min,
    max_size=self._max,
)
```

**Описание.** `asyncpg.create_pool`/`asyncpg.connect` поддерживают `command_timeout` (по умолчанию `None` — я проверил сигнатуру установленного `asyncpg.connect`: `command_timeout=None`), который здесь не передаётся. Также ни в одном месте файла `pool.acquire()` не вызывается с `timeout=`. Итог: ни один запрос, ни ожидание свободного соединения из пула не ограничены по времени.

**Почему это важно / сценарий отказа.** Если Postgres завис на одном запросе (lock wait из-за конкурентной транзакции, сетевой партишн, диск под нагрузкой) — это соединение зависает в `self._pool.acquire()`-блоке навсегда, без возможности клиентского таймаута его прервать. При `max_connections=8` по умолчанию (postgres.py:161) достаточно 8 одновременно зависших запросов (вполне реалистично при `global_concurrency=30` в `round_runner.py:71`, см. Finding 9), чтобы пул был полностью исчерпан — и все остальные `await self._pool.acquire()` тоже зависнут бесконечно, без самовосстановления вплоть до рестарта процесса.

**Recommendation.** Передать `command_timeout=<N>` в `create_pool(...)` (например, 30-60с в зависимости от ожидаемой длительности запросов) и/или `timeout=` на конкретных `acquire()`-вызовах там, где приемлем fail-fast. Сделать это настраиваемым параметром конструктора (аналогично `min_connections`/`max_connections`), а не хардкодить.

---

### Finding 4 — Нет пути миграции схемы; заявленная `alembic`-зависимость нигде не используется и противоречит докстрингу файла (High)

**File:Line:** `src/llm_bench/storage/postgres.py:1-22,39-120,175-186`; `pyproject.toml:47-50,216-218`

**Описание.** `_ddl()` (postgres.py:39-120) состоит исключительно из `CREATE SCHEMA IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` — ни одного `ALTER TABLE` нигде в файле. `initialize()` (строки 175-186) просто прогоняет этот список идемпотентных `CREATE`. Для Postgres `CREATE TABLE IF NOT EXISTS` — это полный no-op, если таблица уже существует, **вне зависимости от того, совпадает ли её текущий набор колонок** с тем, что описывает текущая версия кода.

При этом:
- `pyproject.toml:47-50` объявляет `alembic>=1.13` внутри `[project.optional-dependencies].postgres` с комментарием на строке 216 ("alembic: postgres-extra-only (Alembic migrations for PostgresStorage's schema)").
- В репозитории нет `alembic/`-каталога, `alembic.ini`, `env.py`, `versions/` — я проверил `Glob` по `**/alembic*` и `**/migrations/**` по всему репо, результат пуст в обоих случаях.
- Это прямо противоречит собственному докстрингу `postgres.py:14-17`: **"No alembic dep — keeps the framework's transitive deps small. Consumers wanting Alembic migrations point their existing toolchain at the schema."** — то есть один файл говорит "alembic не используется", а `pyproject.toml` тем не менее объявляет его как зависимость именно "for Alembic migrations for PostgresStorage's schema". Это внутреннее противоречие в самом репозитории, а не домысел.

**Почему это важно / сценарий отказа.** Как только в будущей версии кода в `_RESULTS_COLS`/`_ddl()`/`RunRow` добавится новая колонка (а `RunRow`'s докстринг сам называет себя "Phase-4 OpenRouter fields" — то есть эволюция схемы уже происходила и будет происходить), для **уже развёрнутой** продакшн-БД:
- `initialize()` при следующем запуске молча пропустит `CREATE TABLE IF NOT EXISTS benchmark_results (...)` целиком (таблица уже есть) — новая колонка не появится.
- Первый же `record_call`, использующий обновлённый `_RESULTS_COLS` с новым именем колонки, упадёт с `asyncpg.exceptions.UndefinedColumnError` — то есть **вся запись в БД перестанет работать** до тех пор, пока оператор вручную не выполнит `ALTER TABLE` в обход фреймворка.

Ни явного `ALTER TABLE`-пути, ни хотя бы предупреждения/проверки версии схемы в коде нет.

**Recommendation.** Либо (a) реально подключить `alembic` — создать `alembic/` с `env.py`/`versions/` и генерировать миграции при каждом изменении `_ddl()`, либо (b) убрать `alembic` из `pyproject.toml` и явно задокументировать (без внутреннего противоречия), что миграции — целиком забота потребителя, и добавить хотя бы schema-version bookkeeping (например, таблицу `schema_migrations`/`PRAGMA`-аналог с номером версии), которую `initialize()` может проверить и явно упасть с понятным сообщением "schema out of date, run migration X", вместо тихого пропуска или невнятного `UndefinedColumnError`.

---

### Finding 5 — `schema_name` не валидируется перед f-string-интерполяцией в SQL (High)

**File:Line:** `src/llm_bench/storage/postgres.py:155-167` (конструктор), 203,209,215,230,266,295,324,342,356,392,405,431,437 (13 сайтов `# nosec B608`)

**Описание.** Класс-докстринг (`postgres.py:146-152`) обосновывает подавление bandit B608 так: *"self._schema is set once at construction time from an operator-supplied config value, not per-request user input"*. Но:
1. **Конструктор ничего не проверяет.** `__init__` (строки 155-167) делает буквально `self._schema = schema_name` — нет regex/allowlist на допустимые символы идентификатора Postgres, нет проверки длины, нет `asyncpg`-эквивалента `quote_ident()`. Значение течёт напрямую в f-string 13 раз без единого барьера.
2. **"operator-supplied, not per-request" — это утверждение о том, как класс *используется снаружи*, а не о том, что он *гарантирует сам*.** Я проверил весь репозиторий (`Grep` на `PostgresStorage(` и `schema_name`) — ни один вызов конструктора нигде в `src/`, `tests/`, `examples/` не передаёт `schema_name` вообще (используется только `str = "llm_bench"` default на строке 159, и `examples/job_app_cover_letter/run.py:136` использует `FileStorage`, не Postgres). То есть заявление о "безопасном источнике" не подкреплено и не проверено ничем в этом репозитории — оно целиком зависит от того, как `schema_name` будет проведён через код VocabApp/другого потребителя, который находится **вне** этого репо и вне зоны видимости этого аудита.

**Почему это важно / сценарий отказа.** Сегодня это не активно эксплуатируемая уязвимость (нет ни одного in-repo call site с недоверенным значением). Но это реальная незакрытая граница доверия: любой будущий или внешний вызывающий код (например, мульти-тенантная обёртка, которая мапит `tenant_id` → `schema_name=f"tenant_{tenant_id}"`, где `tenant_id` пришёл из HTTP-запроса/JWT/конфига с более низким уровнем доверия) получает классический SQL-injection-примитив без единой защиты на уровне класса. Blanket `# nosec B608` на 13 местах опирается исключительно на документационное обещание, не проверяемое кодом — именно тот паттерн self-graded suppression, который стоит проверять независимо, а не принимать на веру.

**Recommendation.** Добавить в `__init__` валидацию `schema_name` по allowlist-паттерну (`^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` — с учётом лимита Postgres identifier 63 байта) и/или использовать `asyncpg`-safe `quote_ident` при построении каждого SQL-стейтмента вместо голой f-string-интерполяции. Это дёшево, полностью убирает нужду в 13 `# nosec` и превращает документационное обещание в реально проверяемый инвариант.

---

### Finding 6 — `prefetch_resume_cache`: нет индекса под фильтр, нет ограничения объёма — full scan + неограниченная загрузка в память (High)

**File:Line:** `src/llm_bench/storage/postgres.py:260-287`

**Описание.** Запрос (строки 265-274):
```sql
SELECT composite_hash, provider, model, thinking, response,
       input_tokens, output_tokens, reasoning_tokens, cost_usd, experiment_tag
FROM benchmark_results
WHERE response IS NOT NULL AND length(response) >= $1
  [AND (error_class IS NULL OR error_class = '')]
```
не фильтрует по `experiment_tag` (намеренно — кэш tag-agnostic, `base.py:19-21`) и не ограничен по `LIMIT`. Единственные индексы таблицы — по `experiment_tag`, `stage`, `task_unit_id` (строки 108-110); ни `response`, ни `error_class` не индексированы вообще, и условие `length(response) >= $1` в принципе не sargable для обычного B-tree индекса без выражения-индекса. Это значит **полный последовательный скан всей таблицы `benchmark_results`** при каждом вызове.

Использован server-side cursor (`conn.cursor(...)`, что технически правильный инструмент для стриминга с сервера без буферизации на wire-уровне), но сразу после этого каждая строка кладётся в Python `dict` без какого-либо предела (`out[(...)] = CachedResponse(...)`, строка 277) — то есть локальная память процесса всё равно растёт линейно с числом подходящих строк, включая полные тела `response` (потенциально длинные JSON/prose-ответы модели).

**Почему это важно / сценарий отказа.** По дизайну (докстринг `base.py:19-21`: кэш переиспользуется между произвольным числом прогонов и тегов, потенциально разными потребителями, разделяющими одну схему) таблица `benchmark_results` **обязана** расти без границ на протяжении месяцев эксплуатации — это не побочный эффект, а прямо заявленная цель фичи. При реалистичном масштабе (сотни тысяч — миллионы строк за несколько месяцев прогонов VocabApp/JobApp-подобных потребителей) `prefetch_resume_cache`, вызываемый **на старте каждого прогона** (`resume.py:9-10,45-60`), превращается в: (а) всё более медленный full table scan, (б) всё больший всплеск памяти процесса при старте, без какого-либо TTL/окна/пагинации/лимита. Это архитектурный дефект масштабирования флагманской фичи, а не гипотетический edge-case.

**Recommendation.** Минимум: частичный индекс под сам паттерн фильтра, например `CREATE INDEX ... ON benchmark_results (composite_hash, provider, model, thinking) WHERE error_class IS NULL OR error_class = ''` (partial index, дополнительно ускоряет и сам поиск дублей). Более концептуально: рассмотреть ограничение — например, по возрасту (`ts > now() - interval 'N days'`), с опциональным полным сканом по явному флагу, и/или чтение в потоковом виде с bounded LRU-кэшем в памяти вместо безусловной полной материализации в `dict`.

---

### Finding 7 — `delete_experiment` не атомарен между двумя DELETE (High)

**File:Line:** `src/llm_bench/storage/postgres.py:425-441`

```python
async with self._pool.acquire() as conn:
    n = await conn.fetchval(
        f"WITH d AS (DELETE FROM {self._schema}.benchmark_results "
        f"WHERE experiment_tag=$1 RETURNING 1) SELECT COUNT(*) FROM d",
        experiment_tag,
    )
    await conn.execute(
        f"DELETE FROM {self._schema}.benchmark_winners WHERE experiment_tag=$1",
        experiment_tag,
    )
return int(n or 0)
```

**Описание.** Оба `DELETE` выполняются на одном и том же соединении `conn`, но последовательно и **не** обёрнуты в `async with conn.transaction():`. Каждый выполняется в своей неявной auto-commit транзакции. Сравните с `upsert_prompts` (строки 200-221) в этом же файле, где ровно такая же последовательность из трёх связанных INSERT корректно обёрнута в `async with conn.transaction():` — то есть паттерн в кодовой базе известен и применяется избирательно.

**Почему это важно / сценарий отказа.** `delete_experiment` явно помечен как DESTRUCTIVE в контракте (`base.py:155-165`) и по природе вызывается как осознанное административное действие (часто вручную, интерактивно). Если процесс убит (Ctrl+C, OOM-kill, обрыв соединения с БД) **между** двумя `await`, останется: `benchmark_results` для этого тега удалены, `benchmark_winners` — нет. При повторном прогоне под тем же `experiment_tag` `load_winners` вернёт данные из раунда, для которого фактические строки результатов уже стёрты — несогласованное состояние, которое ничем не сигнализируется вызывающему коду (возврат `int(n or 0)` даже не проверяет, что второй `DELETE` действительно выполнился).

**Recommendation.** Обернуть оба стейтмента в `async with conn.transaction():`, аналогично `upsert_prompts`.

---

### Finding 8 — Нет FK `benchmark_results.composite_hash → benchmark_prompts.composite_hash` (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:59-106`

**Описание.** В том же DDL-блоке `benchmark_prompts` (строки 59-65) объявляет `system_hash TEXT NOT NULL REFERENCES benchmark_system_prompts(hash)` и `user_hash TEXT NOT NULL REFERENCES benchmark_user_prompts(hash)` — то есть referential integrity между уровнями явно проектировалась и применяется. Но `benchmark_results.composite_hash` (строка 68) объявлен просто как `TEXT NOT NULL`, без `REFERENCES benchmark_prompts(composite_hash)`.

**Почему это важно / сценарий отказа.** Protocol (`base.py`) не гарантирует порядок вызовов `upsert_prompts` → `record_call` — это просто два независимых async-метода. В текущем единственном вызывающем коде (`round_runner.py:211,284`) порядок соблюдён (upsert перед record_call в теле одной корутины), но это конвенция вызывающей стороны, а не что-то, что БД проверяет. Без FK любой будущий код (миграционный скрипт, другая реализация Storage, ручная вставка через psql, баг в порядке вызовов) может создать строку `benchmark_results` с `composite_hash`, для которого никогда не существовало строки `benchmark_prompts` — и Postgres не даст об этом знать. Это тихо ломает любую будущую аналитику/отчётность, которая захочет восстановить оригинальный текст промпта по `composite_hash` (сейчас `benchmark_results` не хранит текст промпта, только хэш).

**Recommendation.** Добавить `REFERENCES {s}.benchmark_prompts(composite_hash)` к `composite_hash` в `benchmark_results`, для консистентности с соседними FK в этом же DDL-блоке. Альтернативное прочтение: если у авторов есть намеренная причина не делать этот FK (например, ожидаемые edge-case вставки результатов без сохранённого текста промпта), стоит явно задокументировать это исключение в докстринге DDL — сейчас асимметрия выглядит как недосмотр, а не осознанное решение.

---

### Finding 9 — Дефолтный размер пула (8) меньше дефолтного `global_concurrency` (30); усугубляется частыми spend-агрегатами (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:160-166`; `src/llm_bench/runner/round_runner.py:71,202-207`; `src/llm_bench/runner/budget.py:49-73`

**Описание.** `PostgresStorage.__init__` (строка 161) по умолчанию ставит `max_connections=8`. `RoundConfig.global_concurrency` (round_runner.py:71) по умолчанию — 30 одновременных пар `(model, task_unit)`. Каждая пара на каждый stage делает минимум 2 обращения к БД (строка 211 `upsert_prompts`, строка 284 `record_call`), и если для этого `op` настроен `BudgetGate`, ещё и `query_spend_by_op` **перед** каждым вызовом (round_runner.py:202-207 → budget.py:63 → `postgres.py.query_spend_by_stage:349-363`, полный `GROUP BY stage` агрегат по всем накопленным строкам тега). При 30 параллельных корутинах против пула из 8 соединений подавляющая часть DB-операций будет находиться в очереди на `acquire()` в любой момент времени — то есть заявленный I/O-параллелизм раунда фактически частично сериализуется на уровне БД, а не только на уровне LLM-провайдера.

**Почему это важно.** Не крэш и не порча данных — но заметная деградация throughput относительно того, что подразумевает `global_concurrency=30`, и источник постоянного contention именно в hot path (budget-check перед каждым вызовом). Нет ни валидации соотношения этих двух чисел, ни документации о том, что их стоит согласовывать.

**Recommendation.** Либо поднять дефолт `max_connections` (например, до значения, сравнимого с типичным `global_concurrency`), либо явно задокументировать в докстринге `PostgresStorage`/`RoundConfig` рекомендуемое соотношение "pool size ≈ global_concurrency" и/или добавить в `BudgetGate` простое кэширование spend с коротким TTL, чтобы не пересчитывать `GROUP BY` агрегат перед каждым отдельным вызовом.

---

### Finding 10 — `idx_benchmark_results_stage` не соответствует реальному паттерну запросов (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:109`; фактическое использование — `postgres.py:319-334`; `ranking/ranker.py:161`; `ranking/per_stage_winners.py:106`

**Описание.** Единственное место в файле, где `stage` фигурирует в `WHERE`, — это `query_rows` (postgres.py:320-330), и там он **всегда** идёт вместе с `experiment_tag=$1` в одном запросе. Оба вызывающих места (`ranker.py:161`, `per_stage_winners.py:106`) тоже всегда передают `experiment_tag` (`stage` — опционален). Ни одного запроса, фильтрующего чисто по `stage` без `experiment_tag`, в кодовой базе нет. При таком паттерне одиночный `idx_benchmark_results_stage` (строка 109) в лучшем случае комбинируется планировщиком с `idx_benchmark_results_tag` через Bitmap AND — что менее эффективно, чем один составной индекс `(experiment_tag, stage)`.

**Recommendation.** Заменить (или дополнить) на `CREATE INDEX ... ON benchmark_results (experiment_tag, stage)`.

---

### Finding 11 — `idx_benchmark_results_task_unit` не используется ни одним запросом в этом файле (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:110`

**Описание.** Я прошёл по всем SQL-запросам в файле (`get_cached`, `prefetch_resume_cache`, `query_rows`, `query_spend_total`, `query_spend_by_stage`, `delete_experiment`) — ни один не фильтрует и не джойнит по `task_unit_id`. Индекс существует, но чистый write-overhead: каждый `INSERT` в `benchmark_results` (а это самая горячая таблица во всём фреймворке) поддерживает 4 индексных структуры (составной PK + 3 secondary index), одна из которых, судя по видимому в этом репо коду, не даёт никакой read-выгоды.

**Альтернативное прочтение.** Возможно, индекс намеренно добавлен для внешней ad-hoc аналитики (оператор подключается напрямую через psql/BI-инструмент и фильтрует по `task_unit_id`, минуя Python API) — в таком случае это осознанное решение, а не мёртвый груз. Стоит уточнить у автора; если такого внешнего use-case нет, индекс можно убрать.

---

### Finding 12 — `task_unit_id` nullable в БД, хотя в домене обязателен (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:73`; `src/llm_bench/core/types.py:193`

**Описание.** DDL: `task_unit_id TEXT,` (строка 73, без `NOT NULL`). Доменный тип `RunRow.task_unit_id: str` (`core/types.py:193`) — обязательное поле dataclass без default, то есть на уровне Python любой `RunRow` **обязан** иметь непустой `task_unit_id`. БД же это не проверяет.

**Почему это важно.** Расхождение между "домен гарантирует X" и "БД допускает не-X" — классический источник тихо проникающих плохих данных, если когда-нибудь появится путь записи в обход текущего `RunRow`-конструктора (прямой SQL, другой клиент той же схемы, будущий рефакторинг, ослабляющий домен). Учитывая, что на этой колонке к тому же висит индекс (`idx_benchmark_results_task_unit`, Finding 11), NULL-значения там были бы особенно незаметны.

**Recommendation.** Добавить `NOT NULL` к `task_unit_id` в DDL, синхронно с доменной моделью.

---

### Finding 13 — Нет `CHECK`-констрейнтов против отрицательных cost/token значений (Medium)

**File:Line:** `src/llm_bench/storage/postgres.py:81-97`

**Описание.** `cost_usd`, `effective_cost_usd`, `cache_discount_usd` (NUMERIC) и `input_tokens`, `output_tokens`, `reasoning_tokens`, `cache_hit_tokens`, `cache_write_tokens` (INTEGER) не имеют ни одного `CHECK`-констрейнта (например, `CHECK (cost_usd >= 0)`). Это данные из внешнего API (OpenRouter/провайдер), формально ничто не мешает баговому апстриму записать отрицательное или аномально большое значение — и оно молча попадёт в агрегаты `query_spend_total`/`query_spend_by_stage`, искажая бюджетный учёт без единого сигнала на уровне БД.

**Recommendation.** Добавить неблокирующие defensive `CHECK`-констрейнты на неотрицательность там, где это осмысленно (cost/tokens). Приоритет ниже остальных Medium-находок — это "nice to have"-защита, а не обнаруженный реальный баг с конкретным сценарием воспроизведения.

---

### Finding 14 — `assert self._pool is not None` как единственная защита от вызова до `initialize()` (Low)

**File:Line:** `src/llm_bench/storage/postgres.py:199,226,263,292,322,339,352,381,402,428` (10 сайтов)

**Описание.** Каждый публичный метод (кроме `initialize`/`close`/`schema`) начинается с `assert self._pool is not None[, "call initialize() first"]`. Под `python -O` (assert-стрипинг) это молча исчезает, и следующая строка (`async with self._pool.acquire()`) упадёт с сырым `AttributeError: 'NoneType' object has no attribute 'acquire'` вместо понятного сообщения.

Формально это `bandit` B101 (`assert_used`) — я прогнал `bandit -r src/llm_bench/storage/postgres.py -q` без `-ll`-фильтра и увидел все 10 срабатываний как `Severity: Low`. Реальный CI-гейт этого репозитория — `.pre-commit-config.yaml:129`: `bandit -r src/llm_bench -ll` — флаг `-ll` у bandit означает "репортить только Medium+ severity", то есть **Low-severity B101 не блокирует CI** в текущей конфигурации. Формально инструмент это "видит", но собственный гейт проекта его отфильтровывает — поэтому фиксирую как отдельную находку, а не дублирование уже гейтящегося правила.

**Recommendation.** Заменить на явную проверку с `raise RuntimeError("call initialize() first")` вместо `assert`, либо (компактнее) обернуть в вызываемый `self._require_pool() -> Pool` хелпер в начале каждого метода.

---

### Finding 15 — `NUMERIC`-колонки корректны по типу, но точность не реализована сквозным образом (Low / Info)

**File:Line:** `src/llm_bench/storage/postgres.py:81-96`; `core/types.py:203-204,211,221`

**Описание.** `cost_usd`, `effective_cost_usd`, `cache_discount_usd`, `mean_attempt_duration_sec` объявлены как `NUMERIC` (без precision/scale) — это правильный выбор SQL-типа для денежных величин (в отличие от `FLOAT`/`DOUBLE PRECISION`, что было бы реальной проблемой). Однако сами значения приходят во весь путь как нативные Python `float` (`RunRow.cost_usd: float | None` и т.д., `core/types.py`), без промежуточного `decimal.Decimal`, и передаются в `INSERT` как обычный `float`-параметр (postgres.py:241 `row.cost_usd`). asyncpg корректно закодирует `float` в `NUMERIC`, но точность самого значения уже ограничена тем, как оно было вычислено выше по стеку (в `pyutilz`/провайдере) как `float`. То есть выбор колонки `NUMERIC` — хорошая практика на будущее (не потеряет точность при чтении обратно через `Decimal`), но сегодня не даёт сквозной гарантии точности, поскольку источник данных уже `float`.

Не поднимаю выше Low/Info, так как стоимости отдельных LLM-вызовов — это доли цента, где `float`-погрешность (~1e-15 относительная) практически никогда не будет заметна на суммах, с которыми оперирует этот фреймворк.

---

### Finding 16 — В репозитории нет ни одного реального теста `PostgresStorage` против живой БД (Info)

**File:Line:** `tests/unit/test_storage_protocol.py:33-40`; `tests/conftest.py`; `examples/job_app_cover_letter/run.py:136`

**Описание.** Фикстура `storage` в контрактном тесте (`test_storage_protocol.py:33-40`) параметризована только `"memory"` и `"file"`; комментарий ссылается на несуществующий `test_storage_protocol_postgres.py` как "Phase D follow-up". Единственный in-repo consumer-пример (`examples/job_app_cover_letter/run.py:136`) использует `FileStorage`. Итог: `PostgresStorage` в этом репозитории **никогда не исполнялся против настоящего Postgres** как часть тестов или примеров — отсюда и то, что Finding 1 (100%-воспроизводимый краш) остался незамеченным. Это не отдельная новая рекомендация сверх того, что уже сказано в Finding 1 — фиксирую как контекст/причину, почему остальные находки (особенно 1 и 7) не были пойманы раньше.

## Итог

Всего находок: **2 Critical, 5 High, 6 Medium, 2 Low, 1 Info** (16 итого).

Главный вывод: `PostgresStorage` спроектирован разумно на уровне схемы (правильные типы, частично корректные constraints, идемпотентные upsert'ы для prompts/winners, корректное использование транзакций в `upsert_prompts`/`query_rows`), но практически не тестировался против реального Postgres — из-за чего в файл проникли два действительно серьёзных, гарантированно воспроизводимых дефекта (Finding 1 — падение на каждом вызове `prefetch_resume_cache`; Finding 2 — тихая потеря успешных ретраев), которые напрямую бьют по заявленной центральной ценности фреймворка (resume-cache, точный учёт бюджета). Индексация в целом разумна для текущего набора запросов, но с двумя конкретными нестыковками (Finding 10, 11) и без защиты для главного write-amplification риска — безлимитного роста `prefetch_resume_cache` (Finding 6).
