# Аудит: асинхронность и конкурентность (llm_bench)

## Scope & method (что прочитано, что запускалось)

Прочитаны целиком (не выдержками) все `.py`-файлы под `src/llm_bench/`:

- `core/{types,hashing,scoring}.py`
- `cost/{catalogue,estimator,filter,openrouter}.py`
- `halving/{alive_filter,assignment,driver,pairing,pruner,schedule}.py`
- `pool/base.py`
- `ranking/{per_stage_winners,ranker}.py`
- `runner/{benchmark,budget,classify,resume,round_runner}.py`
- `stage/base.py`
- `storage/{base,file,memory,postgres}.py`
- пустые стабы `cli/`, `confirmation/`, `discovery/`, `provider/` (только докстринг, кода нет — не аудировались за отсутствием содержимого)

Дополнительно: `tests/unit/test_storage_protocol.py`, `tests/integration/test_preflight.py` (единственные тесты, где есть `Semaphore`/`gather`/`asyncio.Lock`), `pyproject.toml`, `../py-ci-shared/configs/ruff-base.toml` (select-список правил ruff), `examples/job_app_cover_letter/{run.py,job_pool.py}` (референсный потребитель — использован только чтобы понять реальные паттерны использования, не аудировался как часть репозитория).

Команды (все read-only, ничего не изменялось):
- `grep`/`Grep` по `async def|await|asyncio\.|create_task|Semaphore|Lock\(|time\.sleep|open\(|with open|requests\.|httpx\.|\.cursor\(|transaction\(` по всему `src/`
- `grep` по `except:|except Exception|except BaseException|CancelledError` по всему `src/`
- `python -m ruff check src --select ASYNC` — **не** входит в реально включённый select репозитория (см. `../py-ci-shared/configs/ruff-base.toml`, строка `select = [...]`, там нет `ASYNC`), запущено только чтобы независимо подтвердить находки по блокирующему I/O в `storage/file.py`; результат встроен в соответствующие находки ниже.

Отдельно: в задании явно упомянут `# nosec B608` / `self._schema` f-string в `storage/postgres.py` как паттерн, требующий независимой оценки. Я прочитал класс целиком (см. ниже) — вопрос SQL-инъекции через `schema_name` относится к категории "SQL/security", а не "асинхронность и конкурентность"; здесь фиксирую только факт (см. Info-4), содержательная оценка — задача security-категории аудита.

## Summary table

| Severity | File:Line | Кратко |
|---|---|---|
| Critical | runner/round_runner.py:109-117, 284 | Широкий `except Exception` в `_one_pair` тихо проглатывает сбои `storage.record_call`/`resume_cache.put`/`budget_gate` ПОСЛЕ успешного платного LLM-вызова → потеря результата и денег без каких-либо признаков ошибки на уровне `run_phase` |
| High | storage/file.py:18-19, 79-87, 124-125, 153-154 | Заявленная в докстринге кросс-процессная безопасность "OS-locked via SQLite WAL mode" не обеспечена: нет `PRAGMA busy_timeout`, нет retry на `database is locked`, сырые JSONL-записи вообще не сериализуются между процессами |
| High | runner/budget.py:49-73 | `BudgetGate.check()` — классический TOCTOU race: N параллельных вызовов одного `op` могут одновременно пройти проверку и суммарно превысить `cap_usd` |
| High | pool/base.py:23-25, runner/benchmark.py:239,299-304 | Протокол `TaskPool.sample()` документирован как async-aware, но `_sample_units()` синхронный и никогда не `await`-ит — падает при follow-документации реализации |
| High | storage/file.py:236-269, runner/budget.py:63 | `query_rows`/`query_spend_by_op` блокирующе читают весь `results.jsonl` под `asyncio.Lock`, замораживая весь event loop; вызывается на каждый stage-call при включённом `BudgetGate` — эффект накапливается |
| Medium | storage/file.py:124-125,153-154,317-321 (ruff ASYNC230-подтверждено) | Синхронный `open()/write()` внутри `async def` под локом — блокировка event loop на каждую запись строки |
| Medium | storage/postgres.py:425-441; storage/file.py:342-362 | `delete_experiment` — два связанных удаления не обёрнуты в одну транзакцию/атомарную операцию → возможна частично-удалённая несогласованность при обрыве |
| Medium | storage/postgres.py:175-186; storage/file.py:79-87 | `initialize()` — TOCTOU race без лока при создании pool/connection; параллельные первые вызовы могут утечь лишний pool/connection |
| Medium | storage/file.py:89-92 | `close()` не берёт `self._lock` — может закрыть aiosqlite-соединение параллельно с операцией, удерживающей лок |
| Medium | stage/base.py:22,38-39 | `StageContext.quarantined`/`quarantine_reason` — документированный механизм "storm-detection... cancels the rest of the pipeline" нигде не реализован (0 использований в коде) |
| Low | storage/file.py:80-83 (в составе 79-87) | `initialize()` безусловно делает `mkdir()` на каждый hot-path вызов (`record_call`/`upsert_prompts`/…) — лишние блокирующие syscalls |
| Low | cost/estimator.py:211, cost/openrouter.py:186-197 | Полностью синхронный (вероятно, сетевой) путь получения каталога моделей в асинхронном по духу фреймворке — нет обёртки/рекомендации для вызова из event loop |
| Low | ranking/ranker.py:180-198 | `compute_ranking` скорит строки строго последовательно даже когда `RowScorer`/`GoldChecker` асинхронны — упущенная возможность распараллелить через `gather` |
| Low | storage/postgres.py:161 vs runner/round_runner.py:71 | `PostgresStorage(max_connections=8)` по умолчанию vs `RoundConfig.global_concurrency=30` по умолчанию — под нагрузкой пул станет узким местом (не баг, а рассогласование дефолтов) |
| Info | src/ (весь) | `asyncio.create_task` нигде не используется — класс багов "fire-and-forget задача собрана GC" неприменим к этому репозиторию |
| Info | src/ (весь) | Bare `except:`/`except BaseException` нигде не найдены — `CancelledError` (BaseException с Python 3.8) корректно пробрасывается через все `except Exception` блоки |
| Info | round_runner.py:119; benchmark.py:192 | `asyncio.gather` без `return_exceptions=True`, но т.к. каждая корутина сама ловит все исключения, поведение эквивалентно — см. Critical-находку про цену этой изоляции |
| Info | storage/postgres.py:137-153 | `# nosec B608`/`self._schema` — вопрос SQL-инъекции вне категории "async/concurrency"; `schema_name` не валидируется в `__init__` (155-167), но содержательная оценка — задача security-аудита |

## Findings

### [Critical] Широкое `except Exception` в `_one_pair` тихо теряет уже оплаченные результаты и может обнулить весь раунд без сигнала наверх

**File:Line:** `src/llm_bench/runner/round_runner.py:109-119` (обёртка), `168-313` (`_run_pipeline`), особенно `221-234` (сам LLM-вызов) vs `284-300` (запись после вызова)

**Описание:**

```python
async def _one_pair(model: str, task_unit: TaskUnit) -> None:
    try:
        await _run_pipeline(...)
    except Exception:
        logger.exception("[round %d] pipeline failed for %s/%s", ...)

await asyncio.gather(*(_one_pair(m, u) for m, u in pairs))
```

Внутри `_run_pipeline` вызов самого провайдера (`_call_llm`, строка 220) обёрнут в собственный `try/except` (строки 218-234) — это ожидаемо и правильно: сетевые/провайдерские ошибки конвертируются в `error_class`/`error_message` и пишутся как обычная (неуспешная) строка.

Но всё, что идёт ПОСЛЕ успешного вызова — `await cfg.storage.record_call(row)` (строка 284), `await cfg.resume_cache.put(...)` (286-298), `await cfg.budget_gate.record_cost(...)` (300) — НЕ имеет собственной обработки ошибок. Любое исключение здесь (обрыв соединения с Postgres, `sqlite3.OperationalError: database is locked` у FileStorage — см. High-находку ниже, ValueError в кастомном `RowScorer`/etc.) прокатывается наверх и глушится единственным `except Exception:` в `_one_pair` — просто строкой в лог.

**Почему это важно / конкретный сценарий отказа:**

1. Модель успешно отвечает (реальные деньги потрачены на LLM-вызов). Сразу после этого `storage.record_call(row)` падает из-за временного сбоя БД/файловой системы. Результат теряется безвозвратно: строка не попадёт ни в БД/JSONL, ни в resume-cache (`resume_cache.put` до которого дело не дошло). При повторном (resume) запуске тот же самый вызов будет **оплачен повторно** — фреймворк, чья основная фича — "resume cache", в этой ситуации не может её обеспечить, и никакого предупреждения оператору, кроме одной строки в логе, не будет.
2. Если сбой систематический (баг в конфигурации storage, неверная сигнатура кастомного `RowScorer`, исчерпание диска и т.п.) — **каждая** пара `(model, task_unit)` в раунде проваливается таким образом. `run_round` (строка 119) благополучно завершает `gather`, идёт в `compute_ranking` (строка 122) над пустым набором строк, `Halving.promote` получает `scores={}` и возвращает вырожденный `RoundResult(candidates_out=[])` (halving/driver.py:95-100). Весь каскад тихо останавливается с нулём выживших кандидатов, и единственный след — лог-файл, а не исключение, долетевшее до вызывающего `run_phase`/`run_round`.

Это ровно тот случай "silent wrong result in normal, non-adversarial operation" — временный сбой БД/диска не является чем-то экзотическим при многочасовом прогоне бенчмарка.

**Recommendation:** обернуть отдельным `try/except` именно блок "запись результата" (`record_call`/`resume_cache.put`/`budget_gate.record_cost`), с явным различением "код провайдера" и "код инфраструктуры хранения": инфраструктурные сбои должны как минимум увеличивать счётчик и (при превышении порога) прерывать раунд с явным исключением, а не растворяться в `logger.exception`. Дополнительно — `run_round` должен уметь отличить "0 успешных строк из-за системного сбоя storage" от "0 успешных строк, потому что все модели дохлые", например через агрегированный счётчик инфраструктурных исключений, поднимаемый как явное предупреждение/исключение в `PhaseReport`.

---

### [High] Заявленная кросс-процессная безопасность FileStorage ("OS-locked via SQLite WAL mode") не обеспечивается кодом

**File:Line:** `src/llm_bench/storage/file.py:14-24` (докстринг модуля), `18-19` конкретно, `44-61` (`_INDEX_DDL`, нет `PRAGMA busy_timeout`), `124-125`/`153-154` (сырой JSONL-append)

**Описание:**

Докстринг модуля прямо утверждает:
> "Concurrent writers within a process serialise via asyncio.Lock; multi-process is OS-locked via the SQLite WAL mode."

Что реально происходит:
1. `initialize()` (строки 79-87) выставляет только `PRAGMA journal_mode=WAL;` — нигде не устанавливается `PRAGMA busy_timeout`. WAL позволяет параллельным READER'ам не блокировать WRITER'а, но **не превращает конкурентную запись двух процессов в автоматическое ожидание** — без `busy_timeout` второй процесс, пытающийся писать в тот же момент, получит немедленное исключение `sqlite3.OperationalError: database is locked` (через `aiosqlite`). Нигде в `record_call`/`upsert_prompts`/`delete_experiment` это исключение не перехватывается и не ретраится — оно просто улетит наверх (и, как показано в Critical-находке выше, будет тихо проглочено в `_one_pair`).
2. Сама индексная (`resume_cache.sqlite`) защита не распространяется на «сырые» данные — `results.jsonl`/`prompts.jsonl`/`_prompts.jsonl` дописываются обычным `open(path, "ab")` (строки 124-125, 153-154) БЕЗ какой-либо межпроцессной блокировки файла (`fcntl`/`msvcrt.locking` и т.п. не используются). На POSIX единичный `write()` в режиме `O_APPEND` часто (не всегда) атомарен между процессами; на Windows (целевая платформа этого окружения) буферизованный режим `"a"` в CPython не даёт той же гарантии atomic-seek-and-write — конкурентная запись из двух процессов может перемежать/повреждать строки JSONL.

**Почему это важно / конкретный сценарий отказа:**

Пользователь параллельно запускает два независимых процесса `llm-bench`/скрипта с одним и тем же `root`-каталогом (например, чтобы ускорить прогон, разделив кандидатов на две группы, или случайно не заметив, что предыдущий фоновый прогон ещё не завершился — ровно та ситуация, о которой явно предупреждает memory-заметка про "never duplicate background tasks"). При коллизии по времени записи в индекс один из процессов падает с `OperationalError`, а строки в `results.jsonl` рискуют оказаться перемешанными на Windows. Модуль-докстринг создаёт ложное ощущение безопасности ("OS-locked via WAL") у консьюмера, который по этому докстрингу примет решение параллелить процессы.

**Recommendation:** либо (а) явно задокументировать FileStorage как **строго single-writer-process** без каких-либо оговорок про WAL-multiprocess, либо (б) реализовать заявленное: `PRAGMA busy_timeout=<N>` + retry-с-backoff на `database is locked` в местах записи индекса, и для сырых JSONL — либо использовать файловую блокировку (`portalocker`/аналог, кроссплатформенно), либо явно предупредить, что JSONL-файлы не защищены от многопроцессной записи.

---

### [High] `BudgetGate.check()` — TOCTOU race: конкурентные вызовы одного `op` могут совместно превысить `cap_usd`

**File:Line:** `src/llm_bench/runner/budget.py:49-73` (`check`, без лока), `47` (`_lock` объявлен, но не используется в `check`), `75-79` (`record_cost`, лок используется только тут)

**Описание:**

```python
async def check(self, storage, experiment_tag, op) -> tuple[bool, float, float]:
    budget = self.budgets_by_op.get(op)
    if budget is None:
        return True, 0.0, float("inf")
    spent_by_op = await storage.query_spend_by_op(experiment_tag=experiment_tag)
    spent = spent_by_op.get(op, 0.0)
    last = self.last_call_cost_by_op.get(op, 0.0)
    projected = max(last * budget.headroom_factor, 0.01)
    if spent + projected > budget.cap_usd:
        return False, spent, budget.cap_usd
    return True, spent, budget.cap_usd
```

`spent` читается из storage (уже персистентных, завершённых вызовов) — но расход тех вызовов, которые **прямо сейчас** находятся в полёте (LLM-запрос уже начат, но `record_call`/`record_cost` ещё не случились, потому что вызов ещё выполняется), в это число не входит. `self._lock` (строка 47) существует в датаклассе, но используется **только** в `record_cost` (75-79) — сам `check()` не сериализован вообще: пока не в проверке нет никакого резервирования бюджета ("reserve then commit"), только "прочитать факт — сравнить — разрешить".

Вызывается это из `round_runner.py:202-207` перед КАЖДЫМ LLM-вызовом стадии, под `global_sem` (по умолчанию 30) и без обязательного `per_op_concurrency` (по умолчанию пусто — то есть без per-op ограничения).

**Почему это важно / конкретный сценарий отказа:**

Round 1: 30 кандидатов одновременно проходят стадию `enrich` (общий `op`). Все 30 корутин почти одновременно вызывают `budget_gate.check(storage, tag, "enrich")`. Ни одна ещё не записала свой расход в storage (они все ещё ждут LLM-ответа или только начинают). Каждая видит одно и то же `spent` (скажем, $0.90 из cap $1.00) и один и тот же `projected` (например $0.05) → все 30 проходят проверку `0.90+0.05 <= 1.00`. Реальный совокупный расход после завершения всех 30 вызовов может превысить `cap_usd` на порядок — вместо $0.10 перерасхода (при headroom, рассчитанном на единственного вызывающего) фактический перерасход ограничен только числом параллельных вызовов того же `op`. Это прямо противоречит заявленному в докстринге модуля назначению — "protect Phase 1's $20 budget envelope from runaway reasoning models" — при том, что конкурентность включена по умолчанию (`global_concurrency=30`), а не является редким/adversarial режимом.

**Recommendation:** превратить `check` в atomic "reserve": под `self._lock` держать внутримодульный счётчик "зарезервировано, но ещё не подтверждено" по `op`, инкрементировать его внутри `check()` (при успехе) и декрементировать/заменять на факт в `record_cost()`/при ошибке вызова. Либо явно задокументировать, что `BudgetGate` не rассчитан на `per_op_concurrency > 1` для платёжно-чувствительных `op`, и советовать выставлять `per_op_concurrency[op] = 1` при использовании `BudgetGate` с жёстким `cap_usd`.

---

### [High] `TaskPool.sample()` документирован как async-aware, но реализация вызова — синхронная и никогда не `await`-ит результат

**File:Line:** `src/llm_bench/pool/base.py:23-25` (докстринг протокола), `src/llm_bench/runner/benchmark.py:239` (точка вызова), `299-304` (`_sample_units`)

**Описание:**

`pool/base.py`, докстринг класса `TaskPool` (строки 23-25):
> "Implementations MAY be async-aware (a coroutine method works too — the runner ``await``s any awaitable result)."

Реальный код, `runner/benchmark.py:299-304`:
```python
def _sample_units(self) -> list[TaskUnit]:
    """Pull the working pool from ``task_pool`` once at phase start."""
    n = self.halving_schedule.pool_size
    units = self.task_pool.sample(n)
    logger.info("[run_phase] sampled %d/%d task units", len(units), self.task_pool.total_size())
    return units
```

`_sample_units` — обычная синхронная функция (`def`, не `async def`), нигде не проверяет и не `await`-ит результат `self.task_pool.sample(n)`. Вызывается синхронно из `run_phase` (строка 239: `units = units or self._sample_units()`).

Для сравнения — `RowScorer` (ranking/ranker.py, докстринг Protocol строка 53 "Async-aware: returning an Awaitable is OK; the ranker awaits it") реализован ПРАВИЛЬНО (ranker.py:181-185: `isinstance(...)` + `await` при необходимости), как и `PromptBuilder` (round_runner.py:315-326, через `asyncio.iscoroutine`). Только `TaskPool.sample()` нарушает собственный задокументированный контракт.

**Почему это важно / конкретный сценарий отказа:**

Консьюмер, следуя докстрингу Protocol, реализует `async def sample(self, n): ...` (например, `TaskPool`, тянущий задачи из внешнего API/БД асинхронно — вполне естественный кейс для async-first фреймворка). При вызове `self.task_pool.sample(n)` в `_sample_units` это вернёт объект-корутину, а не `list[TaskUnit]`. `len(units)` на строке 303 упадёт с `TypeError: object of type 'coroutine' has no len()`, плюс Python выдаст `RuntimeWarning: coroutine 'sample' was never awaited`. Это гарантированный краш при первом же запуске `run_phase` с такой реализацией — то есть при точном следовании официально задокументированному контракту.

В прилагаемом референс-примере (`examples/job_app_cover_letter/job_pool.py:70`) `sample` реализован синхронно, поэтому баг не проявляется в текущих примерах — но контракт, зафиксированный в самом Protocol-докстринге, ломается для любой другой реализации.

**Recommendation:** привести `_sample_units`/`Benchmark.run_phase` к тому же паттерну, что уже используется для `PromptBuilder`/`RowScorer` — сделать `_sample_units` `async def`, проверять `inspect.isawaitable(units)`/`asyncio.iscoroutine(units)` и `await`-ить при необходимости (аналогично для `total_size()`, если он тоже должен поддерживать async — сейчас докстринг это явно не обещает только для него, но стоит сверить с намерением автора).

---

### [High] `FileStorage.query_rows`/`query_spend_by_op` блокирующе сканируют весь `results.jsonl` под общим `asyncio.Lock`, замораживая весь event loop; вызывается на каждый stage-call при включённом `BudgetGate`

**File:Line:** `src/llm_bench/storage/file.py:236-269` (`query_rows`), `271-277` (`query_spend_total`), `279-285` (`query_spend_by_stage`), `287-294` (`query_spend_by_op`); точки вызова — `src/llm_bench/runner/round_runner.py:122` (`compute_ranking` раз в раунд) и `src/llm_bench/runner/budget.py:63` (`query_spend_by_op` — на КАЖДЫЙ stage-вызов, если сконфигурирован `BudgetGate`)

**Описание:**

```python
async def query_rows(self, *, experiment_tag, stage=None):
    ...
    async with self._lock:
        try:
            with open(results_path, "rb") as f:
                for raw in f:
                    ...  # orjson.loads на КАЖДУЮ строку
        except FileNotFoundError:
            return
    for r in rows_to_yield:
        yield r
```

Комментарий в коде утверждает "Read fully under a snapshot — no lock held while caller iterates" — это верно только для фазы `yield` (она действительно вне лока), но САМО чтение+декодирование ВСЕГО файла происходит целиком синхронно (`open`, построчный цикл, `orjson.loads` на каждую строку) внутри `async with self._lock:`. Это не микро-пауза (как для одиночной записи строки) — при заявленном в докстринге модуля масштабе "~50K rows" (`file.py:23`) это может быть заметное по длительности CPU+IO-bound действие без единой точки `await`, то есть **весь event loop полностью замирает** на это время — не только другие ожидающие тот же лок корутины, а вообще все — ни один HTTP-callback, ни один другой `await` во всём процессе не может продвинуться.

Вызывается это:
1. Один раз за раунд из `compute_ranking` (`round_runner.py:122`) — ожидаемо, но с ростом `results.jsonl` от раунда к раунду сканирование в каждом следующем раунде становится всё длиннее.
2. **На каждый stage-вызов** для каждой пары `(model, task_unit)`, если сконфигурирован `BudgetGate` (`round_runner.py:202-207` → `budget.py:63` → `query_spend_by_op` → `query_rows`). Это превращает точечную O(1)-по-смыслу проверку бюджета в O(n_rows_so_far) блокирующее сканирование файла **на каждый** вызов — при глобальной конкурентности по умолчанию 30 это O(n_calls × n_rows) суммарной блокирующей работы за раунд.

**Почему это важно / конкретный сценарий отказа:**

Комбинация "`FileStorage` + `BudgetGate` + `global_concurrency=30`" (все — дефолтные/типичные настройки) на прогоне с накопленными за предыдущие раунды тысячами строк приведёт к тому, что event loop будет проводить заметную долю времени в синхронном чтении файла вместо обработки сетевых ответов — итоговая пропускная способность конкурентных LLM-вызовов деградирует непропорционально росту данных, прямо противоречя заявленному в модуле design-цели ("Concurrency: asyncio.gather... with a global semaphore" — round_runner.py:13-16).

**Recommendation:** (1) вынести блокирующее чтение файла в `asyncio.to_thread`/`loop.run_in_executor`, чтобы не морозить event loop целиком; (2) для `query_spend_by_op`/`query_spend_total`/`query_spend_by_stage` держать инкрементальный in-memory аккумулятор расхода по `(tag, op)`, обновляемый прямо в `record_call` — тогда `BudgetGate.check()` не должен будет пересканировать файл на каждый вызов вообще.

---

### [Medium] Синхронный блокирующий `open()`/`write()` внутри `async def` под локом на каждую запись (подтверждено независимо через `ruff --select ASYNC`, правило не входит в реально включённый select репозитория)

**File:Line:** `src/llm_bench/storage/file.py:124-125` (`upsert_prompts`), `153-154` (`record_call`), `317-321` (`persist_winners`, вне лока — см. отдельно ниже), `329-330` (`load_winners`)

**Описание:** каждая запись строки в `results.jsonl`/`_prompts.jsonl` делается через штатный синхронный `open(path, "ab")` + `f.write(line)`, БЕЗ `aiofiles`/`asyncio.to_thread`. В `record_call`/`upsert_prompts` это происходит внутри `async with self._lock:` (см. выше High-находку про `query_rows` — тот же механизм, но здесь объём одной операции мал: одна JSON-строка). Запустил `python -m ruff check src --select ASYNC` (не входит в реальный select — см. `../py-ci-shared/configs/ruff-base.toml`) — независимо подтверждает 5 срабатываний `ASYNC230` (`Async functions should not open files with blocking methods like open`) ровно на этих строках плюс `317`, `329`.

**Почему это важно / конкретный сценарий отказа:** на быстром локальном SSD стоимость одной такой блокировки — микросекунды, вряд ли заметно на практике. На сетевом диске (докстринг модуля прямо допускает "runs on any disk") или под давлением на диск (в заметках пользователя уже зафиксирована история с Windows paging under load) единичная блокировка растягивается, и — поскольку она держит `self._lock` — все конкурентные `record_call`/`get_cached`/`prefetch_resume_cache` дополнительно сериализуются за ней, а event loop целиком не продвигается на её длительность.

**Recommendation:** обернуть все синхронные файловые операции в `asyncio.to_thread(...)` (Python ≥3.9) — минимальное изменение, сохраняющее текущую логику лока.

---

### [Medium] `delete_experiment` — связанные удаления не атомарны

**File:Line:** `src/llm_bench/storage/postgres.py:425-441`; `src/llm_bench/storage/file.py:342-362`

**Описание:**

Postgres-версия:
```python
async def delete_experiment(self, *, experiment_tag) -> int:
    async with self._pool.acquire() as conn:
        n = await conn.fetchval(
            "WITH d AS (DELETE FROM {s}.benchmark_results WHERE experiment_tag=$1 RETURNING 1) "
            "SELECT COUNT(*) FROM d", experiment_tag,
        )
        await conn.execute(
            "DELETE FROM {s}.benchmark_winners WHERE experiment_tag=$1", experiment_tag,
        )
    return int(n or 0)
```
Два DELETE выполняются как отдельные автокоммитящиеся statements на одном соединении (`async with self._pool.acquire()`), но **не** обёрнуты в `conn.transaction()` — в отличие от `upsert_prompts` (тот же файл, строки 195-221), который для трёх связанных INSERT корректно использует `async with conn.transaction():`. Разрыв соединения/сбой между двумя DELETE оставит `benchmark_winners`-строки без соответствующих `benchmark_results` для этого тега — частично удалённое, несогласованное состояние.

FileStorage-версия: `query_rows` (подсчёт) → `shutil.rmtree(tag_dir)` (синхронный блокирующий вызов, удаляет и `results.jsonl`, и `winners/`) → `async with self._lock: DELETE FROM resume_cache WHERE source_tag=?`. Если процесс упадёт между `rmtree` и удалением индексных строк, `resume_cache.sqlite` останется с "осиротевшими" записями, указывающими на тег, чьи JSONL-файлы уже удалены — не приводит к краху (кэш по-прежнему адресуется по `(composite_hash, provider, model, thinking)`, а не по тегу), но создаёт "мёртвые" cache-hit'ы для данных, у которых больше нет audit-следа на диске.

**Почему это важно:** `delete_experiment` явно помечен в контракте (`storage/base.py:161-164`) как "DESTRUCTIVE" — именно в деструктивных операциях частичный сбой наиболее болезненен (нет способа откатить или доделать вручную, не зная внутреннего состояния).

**Recommendation:** обернуть оба DELETE в Postgres-версии в `async with conn.transaction():`; для FileStorage — либо поменять порядок (сначала удалить индексные строки, потом `rmtree`), либо принять и явно задокументировать эту гонку как best-effort cleanup.

---

### [Medium] `initialize()` — TOCTOU race без лока на создание pool/connection

**File:Line:** `src/llm_bench/storage/postgres.py:175-186`; `src/llm_bench/storage/file.py:79-87`

**Описание:**

Postgres:
```python
async def initialize(self) -> None:
    if self._pool is not None:
        return
    import asyncpg
    self._pool = await asyncpg.create_pool(dsn=self._url, min_size=self._min, max_size=self._max)
    async with self._pool.acquire() as conn:
        for stmt in _ddl(self._schema):
            await conn.execute(stmt)
```
File:
```python
async def initialize(self) -> None:
    self.root.mkdir(parents=True, exist_ok=True)
    index_dir = self.root / "_index"
    index_dir.mkdir(exist_ok=True)
    if self._db is None:
        self._db = await aiosqlite.connect(index_dir / "resume_cache.sqlite")
        ...
```
Оба — классический "check-then-act" без лока, с точкой `await` между проверкой (`if self._pool is not None`/`if self._db is None`) и присвоением. Если `initialize()` вызывается конкурентно из двух корутин (или инициализация не выполнена явно и оба первых вызова `record_call`/`upsert_prompts` в FileStorage одновременно попадают на свою внутреннюю защитную `await self.initialize()` — обе `record_call` (`file.py:131`) и `upsert_prompts` (`file.py:105`) делают это на каждый вызов), обе корутины пройдут проверку `is None`, обе создадут pool/connection, и присвоение `self._pool`/`self._db` последней из них "выиграет" — первый pool/connection никогда не закрывается (утечка соединений).

`Protocol.initialize()` (`storage/base.py:45-48`) документирован как "Safe to call repeatedly" — что верно для последовательных вызовов (DDL идемпотентны через `IF NOT EXISTS`), но не для конкурентных.

**Почему это важно:** для PostgresStorage сценарий требует нетипичного вызова `initialize()` из нескольких корутин параллельно — маловероятно при штатном использовании через `Benchmark.initialize()` (вызывается один раз перед `run_round`). Для FileStorage риск выше: `record_call`/`upsert_prompts` каждый раз сами вызывают `await self.initialize()` — если storage используется НЕ через `Benchmark`-фасад (что явно разрешено — `BenchmarkStorage` Protocol самодостаточен), а напрямую с `asyncio.gather` по нескольким строкам без предварительного явного `initialize()`, гонка реальна.

**Recommendation:** добавить `asyncio.Lock` вокруг блока создания pool/connection в обоих `initialize()` (аналогично тому, как это уже сделано для операций записи).

---

### [Medium] `FileStorage.close()` не берёт `self._lock`

**File:Line:** `src/llm_bench/storage/file.py:89-92`

**Описание:**
```python
async def close(self) -> None:
    if self._db is not None:
        await self._db.close()
        self._db = None
```
Все остальные методы, трогающие `self._db` (`record_call`, `upsert_prompts`, `prefetch_resume_cache`, `get_cached`, `delete_experiment`), корректно оборачивают доступ в `async with self._lock:`. `close()` — исключение: если он выполняется конкурентно с корутиной, которая внутри `async with self._lock: await self._db.execute(...)` ещё ждёт своей очереди/выполняется, `close()` может закрыть `aiosqlite.Connection` прямо во время активного использования этим же объектом в другой задаче — `aiosqlite` ничего не знает про прикладной `asyncio.Lock`.

**Почему это важно / сценарий:** реалистичный триггер — вызывающий код ловит исключение/таймаут в середине `run_phase` (например, `KeyboardInterrupt`→"graceful shutdown" обработчик или внешний watchdog) и вызывает `await bench.aclose()`, пока часть `gather()`-нутых пар ещё физически заканчивает свои `record_call`. Результат — необработанное исключение `aiosqlite`/`sqlite3` в "хвостовых" корутинах вместо чистого завершения.

**Recommendation:** взять `self._lock` внутри `close()` перед закрытием соединения (или дождаться завершения всех pending-операций иным образом, например через отдельный счётчик активных операций).

---

### [Medium] `StageContext.quarantined`/`quarantine_reason` — задокументированный механизм отмены выполнения не реализован

**File:Line:** `src/llm_bench/stage/base.py:14-39`, особенно строки 22 (докстринг) и 38-39 (сами поля); подтверждено grep по всему `src/` — ноль использований вне объявления датакласса

**Описание:** докстринг `StageContext` (строки 21-24) прямо обещает:
> "Carries quarantine state (set by storm-detection logic in the runner) so a runaway reasoning chain on one stage cancels the rest of the pipeline for that pair."

Поля `quarantined: bool = False` и `quarantine_reason: str | None = None` существуют в датаклассе, но нигде в `round_runner.py` (или где-либо ещё в `src/`) не устанавливаются и не читаются. Единственный существующий "circuit breaker" в `_run_pipeline` — это счётчик `error_count >= 3` по классам `DEAD_ERROR_CLASSES` (`round_runner.py:306-312`), который реагирует только на явные ошибки, а не на "runaway" (дорогой/медленный, но технически успешный) вызов — при том, что `duration = time.monotonic() - t0` (строка 223) уже вычисляется, но нигде не сравнивается ни с каким порогом.

**Почему это важно:** для конкурентного/cost-sensitive фреймворка это прямая недоделанность именно safety/cancellation-механизма — заявленная защита от "модели, зациклившейся в дорогом reasoning" отсутствует, притом что `budget_gate` (см. High-находку выше) сам по себе ненадёжен под конкурентностью, так что заявленный "storm-detection" был бы важным дополнительным рубежом.

**Recommendation:** либо реализовать заявленное (порог по `duration`/по накопленной стоимости внутри одной pipeline-цепочки, устанавливающий `ctx.quarantined = True` и прерывающий `for stage in cfg.stages.topo_order():`), либо убрать неиспользуемые поля/докстринг, чтобы не вводить в заблуждение консьюмеров, читающих контракт `StageContext`.

---

### [Low] `FileStorage.initialize()` безусловно вызывает `mkdir()` на каждый hot-path вызов

**File:Line:** `src/llm_bench/storage/file.py:80-83` (внутри `79-87`)

**Описание:** `self.root.mkdir(parents=True, exist_ok=True)` и `index_dir.mkdir(exist_ok=True)` выполняются БЕЗ условия каждый раз, когда вызывается `initialize()` — а `initialize()`, в свою очередь, вызывается на каждый `record_call` (строка 131) и `upsert_prompts` (строка 105), то есть потенциально тысячи раз за прогон. Условие `if self._db is None:` защищает только создание соединения, но не два `mkdir()`-вызова перед ним.

**Recommendation:** проверять дешёвый флаг (`self._initialized`, отсутствующий сейчас в FileStorage) до похода к файловой системе, аналогично `Benchmark._initialized`.

---

### [Low] Полностью синхронный путь получения каталога моделей в асинхронном фреймворке

**File:Line:** `src/llm_bench/cost/estimator.py:211` (`catalogue.list_models(...)`), `src/llm_bench/cost/openrouter.py:179-197` (`OpenRouterCatalogue.list_models`, синхронный вызов `pyutilz.llm.list_openrouter_models`)

**Описание:** `estimate_top_n_by_cost`/`OpenRouterCatalogue.list_models` — обычные `def`, вызывающие (судя по имени и докстрингу файла) сетевой запрос к OpenRouter через pyutilz синхронно. В остальном фреймворк последовательно async-first. Сейчас это не проявляется багом внутри `llm_bench`, так как `discovery/` — пустой стаб, и ничто в `src/llm_bench` не вызывает `estimate_top_n_by_cost` изнутри корутины. Но если/когда `discovery/` будет реализован (а пакет для этого явно зарезервирован), вызов синхронного HTTP из середины async-оркестрации без `asyncio.to_thread` заморозит event loop на время сетевого похода.

**Recommendation:** зафиксировать в докстринге `estimate_top_n_by_cost`, что вызывающая сторона должна оборачивать вызов в `asyncio.to_thread`, если он делается из корутины — до того, как `discovery/` перестанет быть стабом.

---

### [Low] `compute_ranking` скорит строки строго последовательно

**File:Line:** `src/llm_bench/ranking/ranker.py:180-198`

**Описание:** цикл `for row in rows: score_or_aw = scorer(row); ... row_score = float(await score_or_aw)` обрабатывает каждую строку по очереди, даже если `scorer`/`gold_checker.score` асинхронны и независимы друг от друга. Для больших `n_rows` (докстринг `storage/file.py:23` упоминает масштаб "~50K rows") это упущенная возможность распараллелить через `asyncio.gather` с ограничивающим `Semaphore`.

**Recommendation:** не критично для корректности; при обнаружении, что скоринг — узкое место в реальных прогонах, батчить через `asyncio.gather(*, ограниченный семафором)`.

---

### [Low] Рассогласование дефолтов: `PostgresStorage.max_connections=8` vs `RoundConfig/Benchmark.global_concurrency=30`

**File:Line:** `src/llm_bench/storage/postgres.py:155-166` (параметр `max_connections: int = 8`); `src/llm_bench/runner/round_runner.py:71` и `src/llm_bench/runner/benchmark.py:97` (`global_concurrency: int = 30`)

**Описание:** это не гонка и не краш — `asyncpg`-пул корректно ставит в очередь `acquire()`, вызывающие не падают. Но при дефолтных настройках обеих сторон до 30 конкурентных пар одновременно попытаются писать через пул из 8 соединений — часть операций (в первую очередь `record_call`/`upsert_prompts`, которые не гейтятся `global_sem`, см. Info-находку про порядок операций в `round_runner.py`) будет проводить время в очереди на соединение, что при высокой сквозной конкурентности превращается в лишнюю задержку/бэкпрешер, компенсирующий часть выигрыша от `global_concurrency=30`.

**Recommendation:** документально связать эти два дефолта (например, в докстринге `RoundConfig.global_concurrency` упомянуть, что при использовании PostgresStorage стоит поднимать `max_connections` соразмерно, либо явно рекомендовать `max_connections >= global_concurrency` при большом числе кандидатов).

---

### [Info] Позитивные находки / отсутствие ожидавшихся классов багов

- **`asyncio.create_task` нигде не используется** (grep по всему `src/` — ноль совпадений). Весь fan-out идёт только через `asyncio.gather` (`round_runner.py:119`, `benchmark.py:192`). Классический баг "fire-and-forget задача, собранная GC на середине выполнения, потому что ссылка не удержана" — неприменим к этому репозиторию.
- **Bare `except:` / `except BaseException` нигде не найдены** (grep по всему `src/` — только `except Exception`/`except Exception as e`, 7 мест). Поскольку `asyncio.CancelledError` наследуется от `BaseException` (с Python 3.8), ни один из этих блоков его не перехватывает — отмена корректно пробрасывается через все `try/except` в кодовой базе, включая семафор-гейтед LLM-вызов в `round_runner.py:218-234` и `asyncio.wait_for`-обёртку в `benchmark.py:167-190` (preflight).
- **`async with self._pool.acquire() as conn:` используется единообразно** во всём `storage/postgres.py` — ручного `acquire()`/`release()` без контекстного менеджера нигде нет, риска утечки соединения при исключении не обнаружено. `upsert_prompts` (195-221) и `query_rows` (319-334) корректно используют `conn.transaction()` там, где это семантически необходимо (первое — атомарность трёх связанных INSERT, второе — обязательное требование asyncpg для server-side cursor) — это делает отсутствие транзакции в `delete_experiment` (см. Medium-находку) тем более заметным как непоследовательность, а не как "стиль этого файла".
- **Семафоры (`global_sem`/`per_op_sems`) в `round_runner.py:213-234` и `Semaphore` в `benchmark.py:153-192` (preflight) корректно освобождаются в `finally`/через `async with`, включая путь отмены** — между успешным `await sem.acquire()` и входом в `try:` нет точки `await`, так что отмена не может "перепрыгнуть" освобождение уже занятого семафора.
- Вопрос `# nosec B608`/`self._schema` f-string в `storage/postgres.py:137-153` (докстринг класса) прочитан целиком по просьбе из общего контекста аудита: `schema_name` (конструктор, `postgres.py:155-167`) действительно нигде не валидируется/не сверяется со списком разрешённых значений внутри класса. Это вопрос SQL-инъекции/security, а не асинхронности/конкурентности — оставляю содержательную оценку категории "SQL/security" аудита, здесь фиксирую только факт для полноты картины.
