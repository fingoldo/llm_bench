# Корректность и граничные случаи — llm_bench

## Scope & method (что прочитано, что запускалось)

Полностью прочитаны (построчно, не выборочно):

- `src/llm_bench/halving/schedule.py`, `driver.py`, `pairing.py`, `pruner.py`, `alive_filter.py`, `assignment.py`, `__init__.py`
- `src/llm_bench/core/scoring.py`, `hashing.py`, `types.py`
- `src/llm_bench/ranking/ranker.py`, `per_stage_winners.py`, `__init__.py`
- `src/llm_bench/cost/estimator.py`, `filter.py`, `catalogue.py`, `openrouter.py`
- `src/llm_bench/runner/budget.py`, `classify.py`, `resume.py`, `round_runner.py`, `benchmark.py`
- `src/llm_bench/storage/base.py`, `memory.py`, `file.py`, `postgres.py` (в объёме, необходимом для проверки корректности resume-cache — прямое указание в задании)
- `src/llm_bench/pool/base.py`, `src/llm_bench/stage/base.py`
- `README.md`, `CHANGELOG.md` (проверка заявленных гарантий против кода)

Команды:

- `python -m pytest tests/unit tests/integration -m "not live" --no-cov -q` → **112 passed** (offline-сьют зелёный; ни один найденный ниже баг не покрыт существующими тестами — это ожидаемо, т.к. это интеграционные пробелы, а не юнит-логика, которую тестируют изолированно).
- Собственные discriminating-репродукции (см. каждую находку) — короткие скрипты на реальном `Benchmark`/`InMemoryStorage`/`FileStorage`/`BudgetGate`, запущенные через `python`, а не гипотезы «на глаз». Файлы лежат в scratchpad (`repro_coverage_gate.py`, `repro_stale_failure.py`, `repro_filestorage_dedup.py`, `repro_budget_race.py`, `repro_winner_mismatch.py`) — не в репозитории, ничего в репозитории не изменялось/не создавалось кроме этого отчёта.
- `find`/`grep` по всему `src/` для проверки, что предполагаемые «неиспользуемые» символы (`load_winner_substrate`, `is_validator`, `budget_per_call`, `mean_latency_sec`, `RoundResult.scores`) действительно нигде не читаются — 0 совпадений там, где сообщается «мертвый код».

Ни один файл в репозитории не менялся, не форматировался и не удалялся.

## Summary table: Severity | File:Line | One-line summary

| Severity | File:Line | Summary |
|---|---|---|
| Critical | `runner/round_runner.py` (весь `_run_pipeline`, нет упоминания `is_validator`) | Layer-1 "no self-judgment" для validator-стадий никогда не применяется в реальном раннере — каждый кандидат валидирует сам себя |
| Critical | `runner/round_runner.py:160-164` vs `ranking/per_stage_winners.py:81-114` | M_X promotion write-only: `persist_winners` вызывается, `load_winner_substrate` — никогда; раунд k+1 всегда использует собственный (возможно плохой) апстрим кандидата |
| Critical | `runner/round_runner.py:194-199`, `303` + `halving/driver.py:107-118` | Resume/retry ломает coverage-gate: попадания в resume-cache не увеличивают `n_stages_attempted` → полностью «закэшированный» раунд теряет всех кандидатов (подтверждено запуском) |
| Critical | `storage/memory.py:68-80`, `storage/postgres.py:225-256`, `storage/file.py:236-269` | Успешный повтор вызова после провала с тем же PK (`composite_hash,provider,model,thinking`) молча теряется/затеняется старой неудачной строкой — деньги потрачены, результат отброшен (подтверждено запуском, 2 варианта) |
| High | `runner/budget.py:49-73`, `runner/round_runner.py:104-119` | У BudgetGate нет резервирования "in-flight" трат — конкурентные вызовы одного `op` коллективно проходят проверку до того, как кто-то из них запишет свою стоимость; overshoot до `global_concurrency`x (подтверждено запуском: $4.50 факт против cap $1.00) |
| High | `ranking/per_stage_winners.py:58-60` vs `ranking/ranker.py:248-257` | `select_per_stage_winners` выбирает победителя по чистому score, игнорируя `cost_tiebreak_key` — расходится с `StageRanking.winner` при равенстве очков (подтверждено запуском) |
| High | `runner/round_runner.py:151` vs `194-199,206-207` | Знаменатель coverage-gate (`total_stages`) не масштабируется на `units_per_arm` — порог 70% тривиально проходится почти при любом реальном участии |
| High | `core/types.py:91-94` vs `runner/budget.py:60-73` | `Stage.budget_per_call` документирован как основа прогноза бюджета, но нигде не читается — прогноз реально строится на стоимости ПРЕДЫДУЩЕГО вызова с полом $0.01 |
| Medium | `runner/round_runner.py:145-152` vs `core/scoring.py:30-36` | `mean_latency_sec` считается в `compute_ranking`, но никогда не агрегируется/не передаётся в `Halving.promote()` — latency-aware tiebreak не влияет на реальный halving cut между раундами |
| Medium | `halving/driver.py:174-181` | Multi-specialty bonus (+0.05) вычисляется, кладётся в `RoundResult.scores`, но это поле никем не читается — «for downstream rounds» не работает |
| Medium | `runner/round_runner.py:191,254,297,360` | `provider="openrouter"` захардкожен независимо от реального `provider_factory` — риск коллизий resume-cache между разными бэкендами на одном `model_id` |
| Medium | `halving/pruner.py:125,130` | `mad == 0.0` / `spread == 0.0` — точное сравнение float без допуска; чувствительно к шуму накопления (`aggregate_scores[m] /= len(...)`) |
| Medium | `halving/schedule.py` (весь класс) | Нет валидации `round_sizes`/`units_per_arm` (длины, монотонность, положительность) — рассинхрон длин даёт голый `IndexError`, а `0` в `round_sizes` тихо убивает весь пул |
| Medium | `ranking/ranker.py:181-186` | `RowScorer`/`GoldChecker` не валидируются/не клэмпятся в `[0,1]` — NaN/inf от кастомного скорера молча портит сортировку без диагностики |
| Medium | `halving/schedule.py:63-77` vs `halving/driver.py:151-172` | В последнем раунде specialty preservation может вернуть >1 «финального победителя», хотя докстринг `next_round_size` явно обещает «a single final winner» |
| Medium | `cost/estimator.py:126-129` | `estimate_call_cost` полностью исключает модели с ценой `<=0`, а не трактует их как бесплатные/самые дешёвые — легитимно бесплатные (не `:free`) модели пропадают из TOP-N |
| Low | `halving/pairing.py:137` | Мёртвый `.sort()` — немедленно перекрывается `shuffle()` и повторным `.sort()`, эффекта на результат не имеет |
| Low | `halving/schedule.py:41-56` | Дефолтный `pool_size` в `n_calls_for_stage()` может разойтись с реальным размером пула, который возвращает `task_pool.sample(n)` — только стоимостные прогнозы, не сама раздача задач |
| Low | `halving/driver.py:112` | Строка причины исключения может показывать >100% coverage (напр. «400%») — косметическое следствие High-находки про знаменатель |
| Low | `stage/base.py:18-19` | Докстринг `StageContext` ссылается на несуществующее поле `parent_outputs`; реальное поле называется `outputs` |
| Low | `README.md:26` | Ссылка на `docs/architecture.md`, которого нет в репозитории (директории `docs/` не существует вообще) — упомянуто только как подтверждение, что нигде не задокументирован осознанный пропуск wiring из Critical-находок 1/2 |
| Info | `storage/postgres.py:484` (`_record_to_row`) vs DDL `NUMERIC` | Стоимость везде — обычный `float`, включая явный `float()`-каст `NUMERIC` из Postgres; при реальных суммах (доли цента — десятки долларов) это не даёт практических ошибок |
| Info | `runner/budget.py:66-73` | На границе `cap_usd` и при `cap_usd<=0` gate корректно скипает, не падает — claim README "skips, doesn't crash" на уровне одного вызова подтверждён; проблема именно в конкурентности (см. High-находку) |

## Findings

### Critical — Validator-независимость (Layer-1 "no self-judgment") никогда не применяется

**Severity:** Critical
**File:** `src/llm_bench/runner/round_runner.py` (весь `_run_pipeline`, строки 168-313; `_build_prompt`, строки 315-326) в сопоставлении с `src/llm_bench/halving/pairing.py` (полностью) и `src/llm_bench/core/types.py:79-83`

**Описание.** `core/types.py` документирует `Stage.is_validator` как жёсткую, безусловную гарантию: «self-judgment is forbidden: model M cannot validate its own output... Layer 1 (HARD): validator's model_id != producer's model_id. No exceptions.» Модуль `halving/pairing.py` полностью реализует эту логику (`allowed_validator_pairs`, `select_validator_for_producer`), экспортирует её из `llm_bench/__init__.py` и `halving/__init__.py`, README показывает `is_validator=True` как рабочий пример.

Однако единственный реальный оркестратор — `runner/round_runner.py` — нигде не проверяет `stage.is_validator` и никогда не вызывает `allowed_validator_pairs`/`select_validator_for_producer` (проверено `grep -r "is_validator\|validator" src/llm_bench/runner` → 0 совпадений). `_run_pipeline` строит промпт для каждой (модель, task_unit)-пары через собственный, приватный `ctx = StageContext(task_unit=task_unit)` (строка 178), который содержит **только выходы этой же самой модели** на предыдущих стадиях (`ctx.outputs`, `ctx.parent_prompt_hashes`). Нет никакого механизма подставить другую модель в качестве валидатора.

**Почему это важно / сценарий отказа.** Любой пайплайн со стадией `is_validator=True` (пример из самого README: `Stage(id="validate_draft", parent_stage="draft", is_validator=True, ...)`) в реальности заставляет каждого кандидата валидировать собственный же вывод — то есть именно то, что документация называет «forbidden, no exceptions». Смещение (bias) от self-judgment не устраняется, и результат ранжирования по validator-стадиям систематически завышает уверенность модели в собственных ошибках — тихо, без единого предупреждения в логах, при полностью штатном запуске.

**Recommendation.** Либо (а) реализовать wiring: перед сборкой промпта для `stage.is_validator=True` вызывать `select_validator_for_producer(model, candidates, eff_cost=...)`, подставлять чужой апстрим-вывод в `ctx`, либо (б) если это осознанно отложено до будущей версии — явно понизить в README/докстринге до «API готов, но не подключён к `Benchmark.run_phase`», чтобы потребители не полагались на несуществующую гарантию.

**Alternative reading:** можно было бы предположить, что framework ожидает, что кастомный `prompt_builder` сам вызовет `select_validator_for_producer`, — но `PromptBuilder` Protocol (`stage/base.py:47-57`) не получает ни пула кандидатов, ни `eff_cost`, ни доступ к чужим `ctx`, так что технически это невозможно реализовать со стороны потребителя без изменений в самом раннере.

---

### Critical — M_X per-stage-winner substrate promotion: write-only

**Severity:** Critical
**File:** `src/llm_bench/runner/round_runner.py:160-164` (только `persist_winners`, `load_winner_substrate` нигде не вызывается) в сопоставлении с `src/llm_bench/ranking/per_stage_winners.py:81-114`

**Описание.** `per_stage_winners.py` реализует и `select_per_stage_winners` (кто выиграл стадию), и `load_winner_substrate` — вторая функция явно документирована как обязательная часть механики: «The round driver invokes this BEFORE building any candidate's prompt for stage k+1... Without this wiring, "M_X promotion" is write-only and stage k+1 silently uses each candidate's own upstream output.»

`round_runner.py` вызывает `select_per_stage_winners` и `cfg.storage.persist_winners(...)` (строки 154-164) — но `load_winner_substrate` не импортируется и не вызывается нигде в `runner/` (`grep -r "load_winner_substrate" src/llm_bench/runner` → 0 совпадений). `_build_prompt` в `_run_pipeline` строит промпт исключительно из `ctx.outputs` этой же модели.

**Почему это важно / сценарий отказа.** Собственный докстринг модуля предугадывает именно этот баг и явно называет его «silently» — это дословное описание фактического поведения кода. Весь центральный дизайн-принцип многораундового Halving («каждый кандидат в раунде k+1 валидируется/строится на основе ЛУЧШЕГО, а не собственного, апстрима предыдущей стадии — иначе плохой enrich у сильного в translate кандидата маскирует его реальный потенциал») — не работает. Framework тихо деградирует к «каждый кандидат несёт свои собственные ошибки через весь пайплайн», что и есть тот самый failure mode, который M_X promotion был призван устранить.

**Recommendation.** Перед `_build_prompt` для стадий с `parent_stage` вызывать `load_winner_substrate(storage, winners=..., stage=parent_stage, task_unit_id=...)` (загружая `WinnerSet` предыдущего раунда через `storage.load_winners`) и инжектить `response`/`parsed` в `ctx.outputs[parent_stage]` до построения промпта следующей стадии.

---

### Critical — Resume/retry ломает coverage-gate: закэшированный раунд теряет всех кандидатов

**Severity:** Critical
**File:** `src/llm_bench/runner/round_runner.py:194-199` (cache-hit `continue`), `:206-207` (budget-skip `continue`), `:303` (`n_stages_attempted` increment — достижим только по «живому» пути вызова) + `src/llm_bench/halving/driver.py:107-118` (coverage gate)

**Описание.** `n_stages_attempted[model]` увеличивается **только** после реального похода в LLM (строка 303) — на ветке cache-hit (строки 194-199, `continue` до этой строки) и на ветке budget-skip (строки 206-207, тоже `continue`) счётчик не трогается вовсе. `n_stages_attempted` — локальный словарь, создаётся заново в каждом вызове `run_round()` (строка 107: `n_stages_attempted: dict[str, int] = {}`). `Halving.promote()` использует его как `attempted = n_stages_attempted.get(m, 0)` (driver.py:109) и отбрасывает всех, у кого `attempted/total_stages < coverage_min` (по умолчанию 0.7, и `RoundConfig`/`Benchmark` нигде не дают потребителю способ переопределить `coverage_min` — оно жёстко зашито дефолтом в сигнатуре `promote()`).

**Подтверждено запуском** (`repro_coverage_gate.py`): раунд 1 — два кандидата успешно проходят стадию `enrich` живыми вызовами, оба выживают. Раунд 2 (новый `Benchmark`, тот же `storage`+тег, тот же `round_idx=1` — типичный «процесс перезапущен после сбоя, тег не менялся») — оба вызова 100% попадают в resume-cache (0 живых вызовов, деньги не тратятся повторно — это работает верно), но:

```
resume fresh_calls (expect 0): 0
resume final_candidates (candidates that succeeded via cache): []
resume eliminated_reasons: {'cheap_a': 'below coverage_min: 0/1 = 0% < 70%', 'cheap_b': 'below coverage_min: 0/1 = 0% < 70%'}
```

Оба кандидата, полностью и успешно завершившие все стадии (просто через кэш), объявляются «below coverage_min» и элиминируются целиком.

**Почему это важно.** Это прямое противоречие заявленной ценности проекта («cross-tag resume cache so an interrupted run doesn't re-pay for completed calls»): экономия денег работает, но результат ранжирования становится молча пустым/неверным для любого раунда, который (полностью или частично) обслуживается кэшем — то есть именно в целевом сценарии use-case. Не адверсариальный, самый обычный "перезапустили процесс после сбоя с тем же тегом" сценарий.

**Recommendation.** Инкрементировать `n_stages_attempted[model]` и на cache-hit ветке (строка после 199), и (по крайней мере для целей coverage) не увеличивать его на budget-skip ветке отдельно обсуждаемо — но однозначно на cache-hit это баг, а не дизайн-решение.

---

### Critical — Успешный повтор вызова молча теряется/затеняется старой неудачной записью с тем же PK

**Severity:** Critical
**File:** `src/llm_bench/storage/memory.py:68-80`, `src/llm_bench/storage/postgres.py:225-256` (варинат А — блокировка на запись); `src/llm_bench/storage/file.py:236-269` (`query_rows`, вариант Б — неверный порядок дедупликации)

**Описание.** PK строки — `(composite_hash, provider, model, thinking)` (`storage/base.py:11-13`). `composite_hash` детерминирован только от текста промпта (`core/hashing.py`), не зависит от раунда/попытки. Framework явно спроектирован так, что один и тот же (stage, task_unit) может повториться в более позднем раунде для выжившего кандидата (`assign_task_units_for_round` всегда берёт `pool[:n_per_arm]`, и раунд k+1 берёт больший префикс — то есть строго надмножество юнитов раунда k, `assignment.py:9-16,65`), а также при обычном resume того же тега после сбоя.

**Вариант А (InMemoryStorage, PostgresStorage).** `record_call` — блокирующая идемпотентность по ПОЛНОМУ PK, независимо от `error_class` существующей записи:
```python
# storage/memory.py:73-74
if key in self._rows:
    return row.composite_hash
```
```sql
-- storage/postgres.py:235-236
ON CONFLICT (composite_hash, provider, model, thinking) DO NOTHING
```
Если ПЕРВАЯ запись по этому PK — провал (`error_class` заполнен, например транзиентный `RateLimited`), а следующая попытка (следующий раунд/ресурс same tag) РЕАЛЬНО УСПЕШНА — новая успешная строка молча отбрасывается: `record_call` возвращает `composite_hash` как будто всё ок, но в хранилище остаётся только провальная запись навсегда.

**Подтверждено запуском** (`repro_stale_failure.py`, InMemoryStorage):
```
resume cache entries: 0
stored rows count: 1
  stored row: error_class='RateLimited' response=None

BUG CONFIRMED: the successful retry was silently discarded
```

**Вариант Б (FileStorage).** Здесь `record_call`'s идемпотентность проверяется по `resume_cache`-индексу, который заполняется только для успешных строк (`file.py:159`) — поэтому повторная попытка после провала ДЕЙСТВИТЕЛЬНО дописывается новой строкой в `results.jsonl`, и индекс `resume_cache` корректно получает успешный ответ. Но `query_rows()` (используется `compute_ranking` для всех расчётов очков/coverage) дедуплицирует построчно в порядке файла и оставляет **первое** вхождение ключа:
```python
# storage/file.py:262-264
if key in seen:
    continue
seen.add(key)
```
т.е. в точности НАОБОРОТ — должен оставаться последний/успешный вариант, а не первый хронологический.

**Подтверждено запуском** (`repro_filestorage_dedup.py`):
```
resume cache entries: 1
cache has key: True
query_rows count: 1
  query_rows row: error_class='RateLimited' response=None

BUG CONFIRMED: query_rows() returns the STALE FAILED row, even though the resume-cache index correctly holds the success.
```
т.е. resume-cache (для будущих вызовов) уже видит правильный успешный ответ, а вот ранжирование/coverage (`compute_ranking`) — нет.

**Почему это важно.** Во всех трёх бэкендах итог одинаков: модель, которая реально преуспела после сбойного первого захода (заплатив за оба вызова), навсегда остаётся в данных ранжирования как «провалившая» эту (stage, task_unit)-пару. Это напрямую бьёт по Halving cut (заниженный score) и по O1 median/spread (контаминация статистики стабильно-неверными нулями) — молчаливо, в самом обычном multi-round прогоне с хотя бы одним транзиентным сбоем где-то в середине.

**Recommendation.** Для Postgres/Memory: `ON CONFLICT ... DO UPDATE` (или явную upsert-логику в Python), которая перезаписывает провальную запись успешной (но не наоборот — успех не должен перезаписываться повторным провалом, раз уж кэш уже отдал хороший ответ). Для File: при дедупликации в `query_rows()` оставлять последнее вхождение по PK (или явно предпочитать запись без `error_class`), а не первое.

---

### High — BudgetGate не резервирует «in-flight» расходы: конкурентные вызовы одного `op` коллективно превышают cap

**Severity:** High
**File:** `src/llm_bench/runner/budget.py:49-73` (`check`, `record_cost`), `src/llm_bench/runner/round_runner.py:104-119` (concurrency: `global_sem = asyncio.Semaphore(cfg.global_concurrency)`, дефолт 30 в `RoundConfig.global_concurrency`/`Benchmark.global_concurrency`, `per_op_concurrency` по умолчанию **пустой** — без явной настройки cap на `op` вообще нет)

**Описание.** `check()` читает `spent = await storage.query_spend_by_op(...)` — это агрегат уже **завершённых и записанных** вызовов. `projected` строится из `last_call_cost_by_op` — стоимости ПРЕДЫДУЩЕГО завершённого вызова (обновляется только в `record_cost`, который вызывается ПОСЛЕ того, как LLM-вызов завершился и был записан). Между `check()` и `record_cost()` для одного вызова проходит вся сетевая латентность (`_run_pipeline`, строки 218-234 — LLM-вызов внутри `async with global_sem`). Ни резервирования, ни «зарезервированной» суммы в `BudgetGate` не существует — только `last_call_cost_by_op` и spend, читаемый из storage.

Поскольку `asyncio.gather` в `run_round()` запускает ВСЕ (model, task_unit)-пары раунда конкурентно, ограниченные только `global_sem` (по умолчанию 30) и (если не настроен явно потребителем) без per-op-предела, до 30 параллельных вызовов одного и того же `op` могут пройти `check()` одновременно, пока ни один ещё не записал свою стоимость.

**Подтверждено запуском** (`repro_budget_race.py`, 30 «одновременных» вызовов по $0.15 против `cap_usd=1.00`):
```
n_admitted=30, actual_spend=$4.50
BUG CONFIRMED: actual spend $4.50 exceeds cap $1.00 by 4.5x
```

**Почему это важно.** Это происходит при ДЕФОЛТНОЙ конфигурации (`global_concurrency=30`, `per_op_concurrency={}`) — не требует специальной «adversarial» настройки. Прямо противоречит духу README-claim про защиту бюджета («protect Phase 1's $20 budget envelope from runaway reasoning models») — технически «не падает», но и не защищает бюджет при реалистичной конкурентности.

**Recommendation.** Ввести в `BudgetGate` счётчик «reserved/in-flight» стоимости на `op` (инкремент в `check()` при `ok=True`, декремент в `record_cost()` после фактической записи, с учётом возможного расхождения оценки и факта), либо дефолтно применять `per_op_concurrency` cap, вычисленный из `cap_usd / (typical_call_cost)`.

---

### High — `select_per_stage_winners` расходится с `StageRanking.winner` при равенстве очков (игнорирует `cost_tiebreak_key`)

**Severity:** High
**File:** `src/llm_bench/ranking/per_stage_winners.py:58-60` vs `src/llm_bench/ranking/ranker.py:248-257`

**Описание.** `core/scoring.py:26-29` явно формулирует инвариант: «Both halving's `promote()` and the ranker's winner-pick MUST go through this [`cost_tiebreak_key`] — duplicating the sort lambda diverges over time.» `compute_ranking` (`ranker.py`) действительно вызывает `cost_tiebreak_key` для `StageRanking.winner`. Но `select_per_stage_winners` пересчитывает победителя **заново**, своей собственной сортировкой только по `-score`:
```python
# per_stage_winners.py:58-60
sorted_models = sorted(
    sr.model_scores.items(), key=lambda kv: -kv[1],
)
```
При равенстве очков порядок определяется стабильностью сортировки Python — то есть порядком вставки в `scores_by_model` при обходе строк из `storage.query_rows()`, чей порядок сам Protocol объявляет «unspecified» (`storage/base.py:105`).

**Подтверждено запуском** (`repro_winner_mismatch.py`, две модели с одинаковым score=1.0, но разной стоимостью):
```
StageRanking.winner (cost_tiebreak_key): cheap
select_per_stage_winners winner: pricey

BUG CONFIRMED: StageRanking.winner and select_per_stage_winners() disagree on the same stage's winner under a score tie
```

**Почему это важно.** `default_row_scorer` возвращает ровно `{0.0, 1.0}` — при типичном использовании (несколько моделей, все успешно ответили на стадию) равенство очков — не редкий edge case, а НОРМА. `WinnerSet.winners` (персистится и, после исправления Critical-находки про M_X wiring, станет реальным источником substrate для следующей стадии/раунда) может указывать на более дорогую модель, чем официально объявленный `sr.winner` в отчёте — расхождение между «кто победил» в разных частях системы для одной и той же стадии одного и того же раунда.

**Recommendation.** `select_per_stage_winners` должен переиспользовать уже посчитанный `sr.winner` (он уже есть в `report.stages[stage].winner`) вместо повторной сортировки, либо явно вызывать тот же `cost_tiebreak_key`.

---

### High — Знаменатель coverage-gate не масштабируется на `units_per_arm`

**Severity:** High
**File:** `src/llm_bench/runner/round_runner.py:151` (`total_stages=len(cfg.stages.stages)`) vs `:194-199,206-207,303` (numerator — суммируется по ВСЕМ task-юнитам раунда) + `src/llm_bench/halving/driver.py:107-118`

**Описание.** `total_stages` — просто количество РАЗНЫХ стадий в графе (например 5), безотносительно того, сколько task-юнитов назначено кандидату в этом раунде (`units_per_arm`, по умолчанию `(4, 8, 12, 12)`). Но `n_stages_attempted[model]` суммирует попытки по ВСЕМ task-юнитам раунда (каждый `_run_pipeline(model, task_unit)` инкрементирует его независимо). Значит порог `cov = attempted/total_stages >= 0.7` требует лишь `0.7*total_stages` попыток из потенциальных `units_per_arm*total_stages` — при `units_per_arm=4` это всего `17.5%` от максимума. Модель, полностью выполнившая ОДИН из 4 (или 12 — в поздних раундах) назначенных task-юнитов и не сделавшая по остальным вообще ни одной попытки (budget-skip/cache-skip/ранний abort пайплайна при 3+ dead-ошибках, `round_runner.py:308-312`), уже покажет `cov=total_stages/total_stages=100%` — гейт её не поймает.

**Почему это важно.** Docstring `driver.py:5-7` заявляет цель гейта: «models that attempted < 70% of stages are dropped BEFORE statistical pruning so they don't contaminate the median.» При текущей арифметике гейт фактически способен поймать только полный отказ (см. Critical-находку про resume — где `attempted=0` для ВСЕХ task-юнитов сразу), но не частичный, что было явной целью механизма. Это ослабляет защиту O1-median от контаминации частично неудачными кандидатами.

**Recommendation.** Знаменатель должен быть `total_stages * n_units_assigned_this_round_to_this_model` (или явно передавать `n_units_per_arm` в `promote()` и делить на него).

---

### High — `Stage.budget_per_call` документирован, но нигде не используется

**Severity:** High
**File:** `src/llm_bench/core/types.py:91-94` vs `src/llm_bench/runner/budget.py:60-73`

**Описание.** `core/types.py` объявляет типизированное поле с явным контрактом:
```python
budget_per_call: float = 0.05
"""Soft per-call cost projection used by the budget gate. The gate
checks ``stage_total_spent + budget_per_call > stage_cap`` before
each call; over-cap triggers SKIP, not crash."""
```
`grep -rn "budget_per_call" src/llm_bench` даёт ровно 2 совпадения — оба в `core/types.py`, ни одного в `runner/budget.py` или где-либо ещё. Реальная реализация `BudgetGate.check()` вообще не знает о `Stage`/`stage.budget_per_call` — она строит `projected` из `last_call_cost_by_op` (стоимости последнего фактического вызова того же `op`, с полом `max(last*headroom, 0.01)`), см. `budget.py:65-66`.

**Почему это важно.** Это публичное, типизированное, задокументированное поле API `Stage` — консьюмер, задающий `Stage(..., budget_per_call=0.50)` для дорогой reasoning-стадии (как и делает README-пример, `README.md:47,50`, где реально проставлены разные значения `0.50`/`0.10` для разных стадий), разумно ожидает, что это влияет на прогноз бюджета до первого фактического вызова. На деле для ПЕРВОГО вызова любого `op` (когда `last_call_cost_by_op` ещё пуст) прогноз всегда — фиксированные `$0.01`, независимо от того, что стадия объявлена как условно дорогая (`$0.50`). Тихий no-op конфигурации.

**Recommendation.** Либо использовать `stage.budget_per_call` как начальный/минимальный прогноз до появления реальных данных (`projected = max(last_actual * headroom, stage.budget_per_call, 0.01)`), либо убрать поле/докстринг, если оно окончательно устарело.

---

### Medium — `mean_latency_sec` не передаётся из `round_runner.py` в `Halving.promote()`

**Severity:** Medium
**File:** `src/llm_bench/runner/round_runner.py:145-152` vs `src/llm_bench/core/scoring.py:30-36`

**Описание.** `compute_ranking` считает `StageRanking.model_avg_latency_sec` (`ranker.py:245`) и корректно передаёт его в `cost_tiebreak_key` при выборе `sr.winner` (`ranker.py:254`). Но при построении `aggregate_eff_cost`/вызове `cfg.halving.promote(...)` (`round_runner.py:136-152`) latency нигде не агрегируется и не передаётся — сигнатура вызова не содержит `mean_latency_sec=`. `Halving.promote()` принимает этот параметр (`driver.py:83`) и корректно использует его внутри `cost_tiebreak_key` для halving cut (`driver.py:135-138`), но получает `None` всегда в реальных прогонах через `Benchmark`.

**Почему это важно.** Docstring `core/scoring.py:30-36` рекламирует «Pareto-style throughput-aware ranking» как часть общего halving-механизма («Both halving's `promote()` and the ranker's winner-pick MUST go through this»), но фактически latency-множитель работает только для отчётного `sr.winner`, а не для реального решения «кто выживает в следующий раунд».

**Recommendation.** В `run_round()` собрать `aggregate_latency` аналогично `aggregate_eff_cost` и передать в `promote(..., mean_latency_sec=aggregate_latency)`.

---

### Medium — Multi-specialty bonus не влияет ни на одно реальное решение о продвижении

**Severity:** Medium
**File:** `src/llm_bench/halving/driver.py:174-181`

**Описание.** Docstring модуля (`driver.py:17-18`) заявляет: «Multi-specialty bonus: a model winning >= 2 stages in a round gets +0.05 added to its score for downstream rounds.» Код вычисляет `scores_with_bonus` **после** того, как halving cut (`sorted_models`/`halving_survivors`, строки 133-140) и specialty-preservation (строки 151-172) уже приняли решение — бонус кладётся только в возвращаемое поле `RoundResult.scores` (строка 206). `grep -rn "round_result.scores\|scores_with_bonus\|RoundResult(" src` показывает единственного «потребителя» этого поля — юнит-тест (`tests/unit/test_halving.py:397`), который просто проверяет само значение поля. `Benchmark.run_phase()` берёт из `RoundResult` только `candidates_out` (`benchmark.py:284`) — следующий раунд пересчитывает `aggregate_scores` заново из `compute_ranking()` этого нового раунда, полностью игнорируя бонус прошлого.

**Почему это важно.** Бонус не влияет ни на halving cut ЭТОГО же раунда (вычисляется уже после отсечения), ни тем более на следующий раунд (пересчёт с нуля). Заявленный defensive-механизм («модель-специалист по нескольким редким стадиям должна иметь преимущество ниже по каскаду») фактически не существует в исполняемом пути — притом что force-promote «specialty preservation» (соседний, отдельный механизм в том же блоке кода) работает корректно.

**Recommendation.** Либо убрать бонус как нереализуемую в текущей архитектуре идею (раз каждый раунд пересчитывает scores с нуля), либо реально прокидывать carry-over бонус в следующий вызов `promote()` (например, суммируя его в `aggregate_scores` следующего раунда для этой модели).

---

### Medium — `provider="openrouter"` захардкожен независимо от `provider_factory`

**Severity:** Medium
**File:** `src/llm_bench/runner/round_runner.py:191` (resume-cache lookup), `:254` (`RunRow.provider`), `:297` (`resume_cache.put`), `:360` (fallback provider создание)

**Описание.** `RoundConfig.provider_factory` — документированная точка расширения: «Callable `(model_id) -> LLMProvider`... override for tests with a fake provider» (`round_runner.py:75-78`, `benchmark.py:79-80`). Но идентификатор `provider`, который становится частью PK строки и ключа resume-cache (`(composite_hash, provider, model, thinking)`), везде в `_run_pipeline`/`_call_llm` — буквальная строка `"openrouter"`, независимо от того, что реально возвращает `provider_factory`. В `RoundConfig`/`Benchmark` нет поля для передачи реального имени провайдера.

**Почему это важно.** Если консьюмер подставляет НЕ-OpenRouter провайдер (что framework явно поддерживает и даже поощряет для тестов/альтернативных бэкендов) с тем же `model_id`, что и у реального OpenRouter-прогона в той же БД, их записи и resume-cache попадания коллизируют по одному и тому же PK — ответ, полученный от одного бэкенда, может быть выдан за кэш-хит для другого backend'а с иной ценой/поведением.

**Recommendation.** Добавить `provider_label: str = "openrouter"` в `RoundConfig`/`Benchmark` и использовать его вместо литерала.

---

### Medium — `mad == 0.0` / `spread == 0.0`: точное сравнение float без допуска

**Severity:** Medium
**File:** `src/llm_bench/halving/pruner.py:125,130`

**Описание.**
```python
if mad == 0.0:
    ...
    if spread == 0.0:
```
Значения `values`, из которых считается `median`/`mad`/`stdev`, приходят из `aggregate_scores` (`round_runner.py:129-135`, содержит `/= len(report.stages)` — деление float) и/или из `statistics.fmean` (`ranker.py:201`). Оба источника — накопление через деление/суммирование, которое для «концептуально одинаковых» входов (например, все кандидаты успешны на всех стадиях) может дать значения, отличающиеся на несколько ULP (например `0.6000000000000001` vs `0.6`), а не буквально `0.0` на разности. При этом ветка "запасной путь при полном совпадении" (`pruner.py:123-140`, более мягкий множитель `1.5x` вместо `2.5x` порога) не сработает, и вместо неё пойдёт основной bootstrap-путь с крошечным `spread` (например `1e-16`), из-за чего `threshold ≈ median` — экстремально узкий порог, способный отсеять кандидатов, которые «на самом деле» связаны.

**Recommendation.** Заменить на допуск, например `math.isclose(mad, 0.0, abs_tol=1e-9)`.

---

### Medium — `HalvingSchedule` не валидирует свою конфигурацию

**Severity:** Medium
**File:** `src/llm_bench/halving/schedule.py` (весь класс, строки 19-77)

**Описание.** `HalvingSchedule` — обычный `@dataclass` без `__post_init__`-валидации. Возможные несогласованности:
- `units_per_arm` короче `round_sizes` → `n_calls_for_stage()` (строка 52 проверяет границы по `len(self.round_sizes)`, но обращается к `self.units_per_arm[i]`, строка 54) даёт голый `IndexError` вместо понятной ошибки конфигурации.
- `round_sizes`, содержащий `0` (например, опечатка) → `next_round_size` вернёт `0`, halving cut в `driver.py:140` возьмёт `sorted_models[:0] = []`, и, если для этого раунда нет `per_stage_winners`, весь пул кандидатов молча обнуляется без explicit-ошибки.
- Не проверяется, что `round_sizes` монотонно убывает — framework не помешает сконфигурировать «halving», который на самом деле растит пул.

**Recommendation.** `__post_init__`, который проверяет равенство длин `round_sizes`/`units_per_arm`, положительность и (опционально, warning) монотонность `round_sizes`.

---

### Medium — `RowScorer`/`GoldChecker` не валидируются в `[0, 1]`

**Severity:** Medium
**File:** `src/llm_bench/ranking/ranker.py:181-186` (`row_score = float(score_or_aw)`), `:223-227` (`gold_score = await gold_checker.score(...)`)

**Описание.** Контракт `RowScorer`/`GoldChecker` в докстрингах требует `[0, 1]` (`ranker.py:48`, `stage/base.py:86`), но нигде не проверяется на выходе — `float(score_or_aw)` примет любое значение, включая `nan`/`inf`/отрицательные. `gold_score` из `GoldChecker.score()` аналогично не клэмпится перед `gold_scores[row.model].append(gold_score)` и блендом (`ranker.py:237`).

**Почему это важно.** Python допускает сравнение/сортировку с `nan` без исключения, но результат непредсказуем (`nan < x` всегда `False`) — сортировка `sorted(..., key=cost_tiebreak_key)` при `nan` в score молча даёт произвольный, не детерминированный порядок победителя вместо ошибки. Поскольку кастомный `RowScorer`/`GoldChecker` — основная точка расширения framework (VocabApp/JobApp используют свои), это реалистичный путь отказа при баге в чужом плагине, а framework никак не защищается и не сигнализирует об этом.

**Recommendation.** Клэмпить/валидировать `row_score`/`gold_score` в `[0, 1]` сразу после получения, с `logger.warning` при выходе за диапазон или `nan`.

---

### Medium — Последний раунд может вернуть больше одного «финального победителя»

**Severity:** Medium
**File:** `src/llm_bench/halving/schedule.py:63-77` (`next_round_size`, докстринг «For the LAST round, returns 1 (a single final winner)») vs `src/llm_bench/halving/driver.py:151-172` (specialty preservation, не завязана на `target_size`)

**Описание.** `next_round_size` для последнего раунда возвращает `target_size=1` — halving cut берёт `sorted_models[:1]`. Но specialty preservation (строки 151-172) выполняется БЕЗУСЛОВНО после halving cut и может force-promote любое количество моделей — победителей отдельных стадий, которых halving cut отсёк. Для последнего раунда это означает, что `promoted`/`candidates_out` может содержать 2+ моделей, хотя докстринг `next_round_size` прямо обещает «a single final winner».

**Alternative reading:** это может быть осознанным поведением («финальный раунд — тоже shortlist, не строгий singleton, финальный выбор потом делает `compute_ranking`/`report.final_ranking`») — но нигде явно не задокументировано как намеренное расхождение с докстрингом `schedule.py`.

**Recommendation.** Либо явно исключить specialty preservation из последнего раунда (раз докстринг обещает singleton), либо поправить докстринг, чтобы не вводить в заблуждение.

---

### Medium — `estimate_call_cost` исключает легитимно бесплатные модели

**Severity:** Medium
**File:** `src/llm_bench/cost/estimator.py:126-129`

**Описание.**
```python
if input_price_per_m is None or output_price_per_m is None:
    return None
if input_price_per_m <= 0 or output_price_per_m <= 0:
    return None
```
`_entry_from_or_row` (`openrouter.py:96`) уже отфильтровывает `":free"`-модели по тегу в id. Но каталог может содержать модель с явной ценой `$0`/`0.0` БЕЗ тега `:free` (например, временная промо-акция вендора) — она пройдёт `_entry_from_or_row`, но `estimate_call_cost` её отбросит целиком (`None`), а не оценит как `cost_usd=0.0` (самую дешёвую).

**Recommendation.** Трактовать `<= 0` цену как `0.0` для целей проекции стоимости (сохраняя фильтр `:free`-тега в `openrouter.py` как отдельный, самостоятельный признак).

---

### Low — Мёртвый `.sort()` в `pairing.py:137`

**Severity:** Low
**File:** `src/llm_bench/halving/pairing.py:137`

**Описание.**
```python
eligible.sort(key=lambda v: used_validators_overall.get(v, 0))   # (1)
rng.shuffle(eligible)                                              # (2)
eligible.sort(key=lambda v: (used_validators_overall.get(v, 0), ...))  # (3)
```
Строка (1) не влияет на итоговый порядок: (2) полностью перемешивает результат (1), а (3) — сортировка по расширенному варианту того же ключа, чей tie-break как раз использует порядок после (2). Не баг корректности (итог тот же, что и без строки 1), но мёртвый код, который может ввести в заблуждение при будущих правках.

**Recommendation.** Удалить строку 137.

---

### Low — Расхождение дефолтного `pool_size` с реальным размером пула

**Severity:** Low
**File:** `src/llm_bench/halving/schedule.py:41-56` (`n_calls_for_stage`) vs `src/llm_bench/runner/benchmark.py:299-304` (`_sample_units`)

**Описание.** `n_calls_for_stage(round_idx, pool_size=None)` без явного `pool_size` использует `self.pool_size` (статическое значение конфигурации). Реальный размер пула в `Benchmark._sample_units()` — `self.task_pool.sample(n)`, который может по контракту `TaskPool.sample` (`pool/base.py:27-29`, «Return up to n») вернуть МЕНЬШЕ юнитов, чем `n`. Сама раздача задач (`assignment.py:63`) корректно клэмпится к фактическому `len(pool)` — баг только в независимых стоимостных прогнозах (`estimate_top_n_by_cost`/подобных внешних вызовах `n_calls_for_stage` без явного `pool_size`), которые в этом случае будут оптимистичнее факта.

**Recommendation.** Документировать явно (или вычислять) необходимость передавать актуальный `pool_size` в любые cost-projection вызовы.

---

### Low — Coverage-строка причины исключения может показывать >100%

**Severity:** Low
**File:** `src/llm_bench/halving/driver.py:112`

**Описание.** Прямое косметическое следствие High-находки «знаменатель coverage-gate не масштабируется на `units_per_arm`»: `f"{cov:.0%}"` может показать, например, «400%», что для читателя лога выглядит абсурдно/нечитаемо, даже когда сам факт «below coverage_min» технически верен по текущей (ошибочной) формуле.

**Recommendation.** Исправляется автоматически при исправлении High-находки про знаменатель.

---

### Low — Докстринг `StageContext` ссылается на несуществующее поле `parent_outputs`

**Severity:** Low
**File:** `src/llm_bench/stage/base.py:18-19` (докстринг) vs `:27` (реальное поле `outputs`)

**Описание.**
```python
"""...Each stage's PromptBuilder reads upstream stage results from
``parent_outputs`` (keyed by parent stage id). ..."""
task_unit: TaskUnit
outputs: dict[str, Any] = field(default_factory=dict)
```
Поле называется `outputs`, не `parent_outputs`. Чисто документационная неточность, не влияет на выполнение.

**Recommendation.** Поправить докстринг под реальное имя поля.

---

### Low — README ссылается на несуществующий `docs/architecture.md`

**Severity:** Low
**File:** `README.md:26`

**Описание.** `ls docs` → `No such file or directory`; директории `docs/` в репозитории нет вообще. Упомянуто в этом отчёте только потому, что подтверждает: нигде в проекте (ни в README, ни в CHANGELOG, ни в отсутствующем architecture.md) не задокументирован тот факт, что validator-independence и M_X-substrate-promotion (Critical-находки 1 и 2) реализованы, но не подключены к реальному раннеру — то есть это не «известное, задокументированное упрощение alpha-версии», а действительно скрытый пробел.

**Recommendation.** Вне scope этой категории аудита (вероятно, покрывается отдельным треком по документации/структуре репозитория) — упомянуто для полноты картины.

---

### Info — Cost accumulation: plain `float` везде, но не критично на данных масштабах

**Severity:** Info
**File:** `src/llm_bench/storage/postgres.py:484` (`cost_usd=float(r["cost_usd"]) if ...`) и аналогично для всех NUMERIC-полей; `storage/memory.py`/`storage/file.py` используют float с самого начала

**Описание.** Хотя Postgres DDL объявляет `cost_usd NUMERIC` (`postgres.py:81-82`, точная десятичная арифметика на стороне БД), Python-слой (`_record_to_row`, `RunRow.cost_usd: float`) явно конвертирует обратно в `float` при каждом чтении, и все агрегации (`query_spend_total`/`_by_stage`/`_by_op`, суммирование в `budget.py`, `round_runner.py`) — обычное float-сложение. При типичных масштабах проекта (единичные вызовы $0.0001–$5, суммарный бюджет прогона — десятки долларов) относительная ошибка float64 (~1e-15) многократно ниже любого практически значимого порога — накопление тысяч слагаемых такого масштаба не создаёт наблюдаемой проблемы. Отмечено только потому, что явно запрошено в задании аудита; действие не требуется.

---

### Info — BudgetGate корректно скипает на границе `cap_usd`, не падает

**Severity:** Info
**File:** `src/llm_bench/runner/budget.py:66-73`

**Описание.** Проверено чтением кода: `budget.cap_usd <= 0` (ноль/отрицательный бюджет) → `projected = max(last*headroom, 0.01) > 0 >= cap_usd` всегда истинно → чистый `SKIP`, без исключений. Граница `spent + projected > cap_usd` (обычное сравнение, не строгое `>=`) реализована последовательно с остальным кодом — на уровне ОДНОГО (не конкурентного) вызова claim из README «budget exhaustion skips, doesn't crash» подтверждается. Реальная проблема — не в этой границе, а в конкурентности (см. High-находку выше).

## Итог по количеству находок

- Critical: 4
- High: 4
- Medium: 8
- Low: 5
- Info: 2

Всего: 23 находки. Все Critical- и большая часть High-находок подтверждены запуском самостоятельно написанных дискриминирующих репродукций на реальном коде репозитория (не гипотезы «на глаз»), см. секцию Scope & method.
