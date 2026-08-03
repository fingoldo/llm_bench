# Аудит качества кода и Python-практик — llm_bench

## Scope & method (что прочитано, что запущено)

Прочитаны **полностью** (не выборочно) все файлы `src/llm_bench/**/*.py` (39 файлов, включая пустые
stub-пакеты `cli/`, `provider/`, `discovery/`, `confirmation/`), весь `tests/**/*.py` (все unit-,
integration- и test_meta-тесты, а также `tests/property/` — см. находку M4), оба файла примера
`examples/job_app_cover_letter/{run.py,stages.py,job_pool.py}`, `pyproject.toml`, `README.md`,
`CHANGELOG.md`, `.github/workflows/mypy-full.yml`, `.pre-commit-config.yaml` (фрагмент про mypy),
`tests/test_meta/_code_audit_baseline.json`.

Команды (read-only, ничего не изменялось):
- `python -m ruff check src` → 3 находки, все три — уже известные и осознанно разрешённые C901-исключения
  (`promote`, `mad_bootstrap_prune`, `compute_ranking`; см. комментарий в `pyproject.toml` и раздел
  «Complexity hotspots» ниже).
- `python -m mypy src` → `Success: no issues found in 39 source files`, плюс
  `note: unused section(s): module = ['tests.*']` (см. находку L3).
- `mypy --version` → 2.2.0; `ruff --version` → 0.15.20.
- `grep`/`Grep` по всему дереву для `DEAD_ERROR_CLASSES`, `load_winner_substrate`, `is_validator`,
  `per_task_unit_scores`, `mean_latency_sec`, `parse_failure_prefix`, `type: ignore`, `except Exception`,
  `postgres_test_url`, `hypothesis` и т.д. — для проверки, что «изолированный» код действительно (не)
  вызывается из продакшен-пути.

Иных мутирующих команд не запускалось; git не трогался; изменён/создан только этот отчёт.

## Summary table: Severity | File:Line | One-line summary

| Severity | File:Line | Summary |
|---|---|---|
| Critical | `src/llm_bench/runner/round_runner.py:168-313`, `src/llm_bench/ranking/per_stage_winners.py:81-114` | Per-stage «M_X»-substrate promotion (заявленная как центральная идея фреймворка) нигде не вызывается из продакшен-пайплайна — каждый кандидат на самом деле использует свой собственный, а не winner-substrate, вывод предыдущего этапа. |
| Critical | `src/llm_bench/runner/round_runner.py:168-313`, `src/llm_bench/halving/pairing.py:1-24,97-185` | Layer-1 «hard»-правило независимости валидатора («модель не может валидировать сама себя») нигде не применяется — раннер не смотрит на `Stage.is_validator` вообще, модель всегда валидирует собственный вывод. |
| High | `src/llm_bench/halving/driver.py:130`, `src/llm_bench/halving/pruner.py:41-193` | `mad_bootstrap_prune`'s cost-penalty/variance-penalty/paired-bootstrap (заявленная «фишка» модуля) никогда не активируется — единственный вызывающий код не передаёт `per_task_unit_scores`/`eff_cost`. |
| High | `src/llm_bench/runner/round_runner.py:129-152`, `src/llm_bench/core/scoring.py:10-44` | Latency-aware Pareto tiebreak (`cost_tiebreak_key(mean_latency_sec=...)`) используется при выборе winner'а этапа, но не при halving cut — раннер не агрегирует и не передаёт `mean_latency_sec` в `Halving.promote()`. |
| High | `src/llm_bench/storage/memory.py:90-92,114-117`, `src/llm_bench/storage/file.py:159,194-196,225`, `src/llm_bench/storage/postgres.py:266-274,304-307`, `src/llm_bench/ranking/ranker.py:66-70` | Предикат «строка успешна» (`no error_class` + `len(response) >= 20`) независимо продублирован в 8 местах в 4 файлах с «магическим» порогом `20` вместо одной общей константы/хелпера. |
| High | `src/llm_bench/stage/base.py:64-66`, `src/llm_bench/core/types.py:228`, `src/llm_bench/runner/round_runner.py:252-283` | `RunRow.parse_failure_prefix`/`logs`/`logs_compressed`/`http_status_sequence`/`per_attempt_durations_sec` полностью прошиты через все storage-бэкенды, но никогда не заполняются единственным писателем; docstring `stage/base.py` прямо и ложно утверждает обратное. |
| High | `src/llm_bench/storage/postgres.py:39-167,203-441` | `schema_name` f-string-интерполируется в 15+ SQL-запросов (`# nosec B608` на каждом) без какой-либо валидации/allow-list на входе конструктора — docstring-обоснование безопасности ничем не подкреплено в коде. |
| High | `src/llm_bench/runner/round_runner.py:116-117,184-185,341-343` | «log-only» broad-except в продакшен hot path — уже задетектированы собственным pyutilz code_audit-сканером, но лишь занесены в baseline, а не исправлены. |
| Medium | `src/llm_bench/runner/budget.py:63` | `# type: ignore[arg-type]` маскирует реальное несоответствие типов `str`/`ExperimentTag`, решаемое одной явной приведением типа. |
| Medium | `src/llm_bench/cost/estimator.py:136` | `# type: ignore[operator]` — mypy не может сузить `Optional` через промежуточный булев флаг; решаемо небольшим рефакторингом без подавления. |
| Medium | `tests/unit/test_storage_protocol.py:5,33-40`, `tests/conftest.py:36-43` | `PostgresStorage` (509 строк, самый рискованный бэкенд) не покрыт вообще никаким тестом; `postgres_test_url`-фикстура и маркер `pytest.mark.postgres` объявлены, но не используются нигде. |
| Medium | `tests/property/__init__.py` | Директория `property/` пуста (только `__init__.py`); `hypothesis>=6.0` объявлен как dev-зависимость, но нигде не используется. |
| Medium | `src/llm_bench/runner/round_runner.py:308`, `src/llm_bench/halving/alive_filter.py:44-72` | Fast-abort «3+ DEAD-ошибки» внутри одного пайплайна не учитывает `RateLimited` — вероятно намеренно, но стоит подтвердить у автора. |
| Low | `src/llm_bench/cost/openrouter.py:22,41` | Приватные хелперы `_per_token_to_per_m`/`_normalise_uptime` принимают параметр `value` вовсе без аннотации типа. |
| Low | `src/llm_bench/cost/openrouter.py:188` | `kwargs: dict = {...}` — «голый» `dict` вместо `dict[str, Any]`. |
| Low | `pyproject.toml` (`[[tool.mypy.overrides]] module = "tests.*"`) | Мёртвый конфиг: ни pre-commit, ни CI не запускают mypy на `tests/`, секция никогда не срабатывает (подтверждено `mypy`-notice). |
| Low | `tests/test_meta/test_no_unicode_in_console_output.py:49-52` | AST-проверка смотрит только на `ast.Constant`-строки как позиционные аргументы — f-строки/kwargs/конкатенацию не проверяет. |
| Low | `src/llm_bench/halving/alive_filter.py:75` | Комментарий «Subset of DEAD_ERROR_CLASSES that are TRANSIENT» неточен (не является подмножеством); подтверждено намеренным и покрыто тестом — просто неточная формулировка. |
| Low | `src/llm_bench/halving/pairing.py:38-82` | `_VENDOR_TO_FAMILY` — большая статичная руками сопровождаемая карта вендоров; будет тихо устаревать по мере появления новых вендоров в OpenRouter (fallback корректен, но требует внимания). |
| Info | `README.md:20`, отсутствие `docs/` | README рекламирует «Validator-pairing for cross-family scoring», а `docs/architecture.md`, на который ссылается README, физически отсутствует — подтверждает масштаб находок Critical #1/#2. |
| Info | `pyproject.toml` (`[project.scripts]`), `src/llm_bench/cli/` | Заявлен console-script `llm-bench = llm_bench.cli.main:main`, но `src/llm_bench/cli/main.py` не существует (только `__init__.py`-заглушка). |

## Findings

### [Critical] Per-stage «M_X»-substrate promotion не подключена к раннеру

**File:Line**: `src/llm_bench/runner/round_runner.py:168-313` (`_run_pipeline`, `run_round`),
`src/llm_bench/ranking/per_stage_winners.py:1-18,81-114` (`load_winner_substrate`),
`src/llm_bench/core/types.py:79-82` (docstring `Stage.is_validator`… нет, см. ниже отдельно),
`README.md:9,20`.

**Description**: Модуль `ranking/per_stage_winners.py` описывает это как «the central design constraint
of multi-round Halving»: когда кандидат Y выполняет этап k+1, апстрим-контекстом должен быть
распарсенный вывод **победителя** этапа k (M_k), а не собственный вывод Y на этапе k — чтобы плохой
результат Y на этапе k не маскировал его реальные способности на этапе k+1. Для этого существует
`load_winner_substrate()` (per_stage_winners.py:81-114), чей docstring прямо предупреждает: «Without
this wiring, "M_X promotion" is write-only and stage k+1 silently uses each candidate's own upstream
output.»

Именно это и происходит. `round_runner.py::_run_pipeline` (единственное место, где строится
`StageContext`) создаёт `ctx = StageContext(task_unit=task_unit)` заново для каждой пары
(model, task_unit) (строка 178) и заполняет `ctx.outputs[stage.id]` только из **собственных** вызовов
этой модели (строка 246). Нигде в `_run_pipeline`/`run_round`/`Benchmark.run_phase` не вызывается
`load_winner_substrate`, не читается `cfg.storage.load_winners(...)`/`persist_winners(...)`-результат
для инъекции в `ctx` другой модели. `select_per_stage_winners()` действительно вызывается
(round_runner.py:154-159) и результат персистится через `storage.persist_winners(...)` (строки 160-164)
— но это чисто «write-only» путь, как и предупреждает сам docstring: реально сохранённые winner'ы
никогда не читаются обратно для построения промптов.

Подтверждение через grep: `load_winner_substrate` вызывается **только** из
`tests/unit/test_per_stage_winners.py` (изолированный юнит-тест против `InMemoryStorage` напрямую) —
ни разу из `src/llm_bench/runner/*` или `src/llm_bench/__init__.py`-фасада `Benchmark`.

Референс-потребитель (`examples/job_app_cover_letter/stages.py:56,74,87`) подтверждает это на
практике: `build_draft_prompt`/`build_polish_prompt`/`build_validate_draft_prompt` читают
`ctx.outputs.get("research")`/`ctx.outputs.get("draft")` напрямую — то есть каждый кандидат
действительно использует **свой** апстрим-вывод, а не вывод победителя раунда.

`README.md:9` заявляет «per-stage winner promotion» прямо в первом предложении описания проекта,
`README.md:20` перечисляет «Validator-pairing for cross-family scoring» как отдельную фичу — ни одна
строчка не помечена как «not yet wired» ни в README, ни в CHANGELOG.md (проверено grep'ом — нулевые
совпадения на «substrate», «wired», «TODO», «not yet», «unimplemented», «stub»).

**Why it matters / concrete failure scenario**: Любой обычный прогон `Benchmark.run_phase()` с
многораундовым `StageGraph` (`parent_stage` не `None` хотя бы у одного этапа) даёт другую (по
собственному определению фреймворка — менее честную) сравнительную оценку моделей, чем
задокументировано. Модель, слабая на раннем этапе (enrich), несправедливо «тащит» этот провал во все
последующие этапы своей собственной цепочки, тогда как по дизайну она должна была получить чистый
апстрим от победителя раунда. Это меняет итоговый ranking и halving-отсев без единой ошибки, лога-
предупреждения или падения теста — классический «silent wrong result in normal, non-adversarial
operation».

**Recommendation**: Либо (а) подключить `load_winner_substrate()` в `_run_pipeline` перед вызовом
`_build_prompt` для этапов с `parent_stage is not None` (начиная со второго раунда, когда winner уже
персистирован), либо (б) если решено сознательно отказаться от этой фичи в пользу «каждый кандидат
строит свою цепочку сам», — убрать/переписать docstring в `per_stage_winners.py`, `core/types.py` и
README, чтобы не вводить в заблуждение. Не оставлять «наполовину реализованным».

---

### [Critical] Layer-1 независимость валидатора («модель не судит сама себя») не применяется

**File:Line**: `src/llm_bench/runner/round_runner.py:168-313` (`_run_pipeline`),
`src/llm_bench/halving/pairing.py:1-24,97-185` (`allowed_validator_pairs`, `select_validator_for_producer`),
`src/llm_bench/core/types.py:79-82` (docstring `Stage.is_validator`).

**Description**: `core/types.py:79-82` документирует: «`is_validator` flags stages that audit a prior
stage's output. When a candidate pool is supplied, **the framework swaps in a different candidate as
the validator** (Layer-1 hard cross-family filter) so a model never validates its own output.»
`halving/pairing.py` — целый модуль (185 строк), реализующий именно это: HARD-правило «validator ≠
producer, без исключений», SOFT cross-family предпочтение, и вырожденные случаи N=1/2/3/≥4 с логами
разной степени серьёзности.

Ни `allowed_validator_pairs`, ни `select_validator_for_producer`, ни само поле `stage.is_validator`
**ни разу не читаются** в `round_runner.py` (grep по `is_validator` в `src/` даёт совпадения только в
`core/types.py` (объявление поля) и `halving/pairing.py` (docstring модуля) — ни одного в
`runner/round_runner.py` или `runner/benchmark.py`). `_run_pipeline(model=model, ...)` идёт по
`cfg.stages.topo_order()` и для **каждого** этапа, включая помеченные `is_validator=True`, использует
**одну и ту же** `model` из внешнего цикла — никакой подмены валидатора не происходит в принципе.

Это тот же корень, что и предыдущая находка (единая точка входа `_run_pipeline` никогда не смотрит на
`stage.is_validator`), но с отдельным, более резким докстринг-обещанием: «No exceptions» (pairing.py:9).

**Why it matters / concrete failure scenario**: Любой `StageGraph` с `is_validator=True`-этапом (пример
из репозитория — `validate_draft` в `examples/job_app_cover_letter/stages.py:203-209`, `validate_enrich`
в `tests/integration/test_smoke_in_memory.py:114-119`) на практике даёт **самооценку**: каждая модель
проверяет собственный черновик. Это ровно тот сценарий, который `pairing.py` называет «self-judgment
is forbidden… No exceptions» — предвзятая, завышенная оценка моделей, которые «любят» свой стиль,
систематически искажает validator-pair ranking (единственный доступный сигнал качества в no-gold
режиме — см. `ranking/ranker.py:8-14`, `stage/base.py:83-84`). Опять же — без крэша, без варнинга.

**Recommendation**: В `_run_pipeline`/`run_round` для этапов с `stage.is_validator=True` вызывать
`select_validator_for_producer(producer_model=model, candidate_pool=cfg.candidates, ...)` и выполнять
вызов **этой** моделью вместо `model`, либо пропускать этап согласно правилам N=1/2 из `pairing.py`.
Либо явно задокументировать как известное ограничение текущей версии (0.1.0, alpha) — но не заявлять
как реализованную фичу в README/docstring'ах.

---

### [High] `mad_bootstrap_prune`'s cost/variance-penalty и paired-bootstrap никогда не активируются

**File:Line**: `src/llm_bench/halving/driver.py:130`, `src/llm_bench/halving/pruner.py:41-193`,
`tests/unit/test_halving.py:78-116` (`TestMADBootstrapPrune`).

**Description**: Docstring `pruner.py:1-30` описывает две «penalty terms» как основную идею модуля:
variance-penalty (штраф за «скачущий» скор через `per_task_unit_scores`) и cost-penalty (штраф за
дороговизну через `eff_cost`), плюс paired-bootstrap-режим, который использует `per_task_unit_scores`
для честного «apples-to-apples» доверительного интервала. Всё это полностью реализовано и
параметризовано (`pruner.py:47-50`).

Единственный вызывающий код — `halving/driver.py:130`:
```python
o1_survivors, _lb = mad_bootstrap_prune(scores)
```
— вызывает функцию **только** с `scores`, не передавая ни `per_task_unit_scores`, ни `eff_cost`, хотя
`eff_cost` **доступен прямо тут же** как параметр `promote()` (driver.py:77) и используется двумя
строками ниже для `cost_tiebreak_key` (driver.py:133-139). Grep по всему репозиторию (`src/` и `tests/`)
подтверждает: `per_task_unit_scores=` как именованный аргумент не встречается нигде, кроме собственного
определения в `pruner.py`. Юнит-тесты `TestMADBootstrapPrune` (test_halving.py:78-116) тоже ни разу не
передают `per_task_unit_scores`/`eff_cost` — то есть эта половина функции не покрыта тестами вовсе.

**Why it matters / concrete failure scenario**: На каждом реальном раунде халвинга O1-отсев по факту
работает по «упрощённой» ветке (голый MAD по средним скорам), а не по заявленной риск/стоимость-
скорректированной версии. Модель со «скачущими» результатами (0.95, 0.10, 0.95, 0.10 — собственный
пример из docstring, строки 7-8) НЕ получает штраф за дисперсию и может пройти отсев наравне со
стабильной моделью с тем же средним — именно тот сценарий, который модуль заявляет решать.

**Recommendation**: Либо прокинуть `eff_cost` (уже есть в `promote()`) и агрегированные
per-task-unit-скоры (их можно получить из `report.stages[stage].rows` в `round_runner.py::run_round`,
переагрегировав по `(model, task_unit_id)`) в вызов `mad_bootstrap_prune(...)`, либо явно
задокументировать, что O1-прунинг в v0.1.0 работает только в «базовом» режиме, и добавить юнит-тесты
на penalty-ветки, раз они существуют в публичном API.

---

### [High] Latency-aware tiebreak учитывается при выборе winner'а этапа, но не при halving cut

**File:Line**: `src/llm_bench/runner/round_runner.py:129-152` (агрегация в `run_round`),
`src/llm_bench/ranking/ranker.py:245,251-256` (`compute_ranking`), `src/llm_bench/core/scoring.py:10-44`
(`cost_tiebreak_key`), `src/llm_bench/halving/driver.py:83,133-139` (`promote`).

**Description**: `core/scoring.py:30-36` документирует «Pareto-style throughput-aware ranking»: при
одинаковой $/call модель в 2 раза быстрее должна выигрывать тай-брейк. `ranking/ranker.py:245` реально
вычисляет `sr.model_avg_latency_sec` из сырых строк и передаёт его в `cost_tiebreak_key(...,
mean_latency_sec=sr.model_avg_latency_sec)` при выборе winner'а **одного этапа** (ranker.py:251-256) —
здесь всё работает как задумано.

Но `round_runner.py::run_round` (строки 129-152), агрегируя `aggregate_scores`/`aggregate_eff_cost` по
всем этапам для передачи в `cfg.halving.promote(...)`, **не** агрегирует аналогичный
`mean_latency_sec`-словарь и не передаёт его в `promote()`, хотя `Halving.promote()` принимает
одноимённый параметр (`driver.py:83`) именно для этой цели и использует его в halving-cut-сортировке
(`driver.py:133-139`). В итоге при *выборе одного лучшего* исполнителя этапа латентность учитывается, а
при решении «кто вообще проходит в следующий раунд» (более важное решение, отсеивающее половину пула)
— нет.

**Why it matters / concrete failure scenario**: Два кандидата с одинаковым eff_cost и одинаковым
score на границе halving-cut: без учёта latency порядок между ними определяется третьим
тай-брейком — именем модели (`core/scoring.py:44`, `(sgn*score, cost, model)`) — то есть фактически
алфавитным порядком, а не скоростью. Задокументированная «throughput-aware» гарантия соблюдается только
частично и непоследовательно между двумя близкими механизмами одного и того же фреймворка.

**Recommendation**: В `run_round` агрегировать `mean_latency_sec` тем же способом, что и
`aggregate_eff_cost` (строки 136-142), и передать его в `cfg.halving.promote(...)`.

---

### [High] Предикат «строка успешна» продублирован в 8 местах с «магическим» порогом 20

**File:Line**: `src/llm_bench/storage/memory.py:90-92` (`prefetch_resume_cache`), `:114-117`
(`get_cached`); `src/llm_bench/storage/file.py:159` (`record_call`), `:194-196`
(`prefetch_resume_cache`), `:225` (`get_cached`); `src/llm_bench/storage/postgres.py:266-274`
(`prefetch_resume_cache`, SQL-текст), `:304-307` (`get_cached`); `src/llm_bench/ranking/ranker.py:66-70`
(`default_row_scorer`).

**Description**: Бизнес-правило «строка считается успешной/пригодной для кеша, если `error_class`
пуст И `len(response) >= 20`» независимо переписано вручную 8 раз в 4 разных файлах:
- `memory.py` — дважды в одном файле (строки 90-92 и 114-117);
- `file.py` — трижды (159, 194-196, 225), причём один раз как Python-условие, другой — идентичное по
  смыслу условие на уже распакованных из SQLite-курсора переменных;
- `postgres.py` — как SQL-предложение `WHERE response IS NOT NULL AND length(response) >= $1 AND
  (error_class IS NULL OR error_class = '')` (266-274) и отдельно как Python-условие в `get_cached`
  (304-307);
- `ranking/ranker.py:66-70` — `default_row_scorer`, который управляет **скорингом**, а не кешем, но
  использует буквально тот же порог `20` и ту же логику.

Литерал `20` фигурирует явно в 5 местах (`memory.py:116`, `file.py:159,225`, `postgres.py:306`,
`ranker.py:68`) плюс ещё 3 раза как значение по умолчанию параметра `min_response_len: int = 20` —
отдельно в каждой из трёх сигнатур `prefetch_resume_cache` (`memory.py:85`, `file.py:181`,
`postgres.py:261`). Нигде в кодовой базе нет единой именованной константы
(`MIN_CACHEABLE_RESPONSE_LEN` или аналогичной) или общей функции-предиката.

Комментарий в `memory.py:206` («kept here as the canonical impl») показывает, что авторы **уже**
осознанно применили этот паттерн (вынести общую логику в один файл и импортировать) для
`_stage_to_op` (`file.py:290`, `postgres.py:368` оба честно делают `from llm_bench.storage.memory
import _stage_to_op`) — то есть примерно тот же самый DRY-приём для «is row usable» просто не был
применён по аналогии.

**Why it matters / concrete failure scenario**: Это классический «will drift when one copy is fixed and
the other isn't» кейс. Например, если завтра `ranker.py::default_row_scorer` порог поднимут до 30
символов (более строгий критерий «непустого» ответа), а `storage/memory.py`/`storage/postgres.py`
забудут обновить — resume cache будет считать успешной и переиспользовать строку с ответом длиной
20-29 символов, хотя свежий вызов с таким же ответом уже оценивался бы `default_row_scorer` как 0.0.
Результат: тихое расхождение между «что попадает в кеш» и «что скорер считает успехом» — данные,
пришедшие из кеша, статистически отличаются от свежих данных без единого сообщения об ошибке.

**Recommendation**: Вынести `MIN_USABLE_RESPONSE_LEN = 20` и функцию вроде `is_row_usable(response:
str | None, error_class: str | None, min_len: int = MIN_USABLE_RESPONSE_LEN) -> bool` в
`core/types.py` или отдельный `core/predicates.py` (core — низший слой, всем доступен по layering-правилу
из `tests/test_meta/test_no_import_cycles.py`), и переиспользовать её везде, включая
SQL-версию в postgres.py (там достаточно оставить `$1`-параметризацию, но константу для дефолта брать
оттуда же).

---

### [High] Диагностические поля `RunRow` полностью прошиты в storage, но никогда не заполняются; docstring лжёт

**File:Line**: `src/llm_bench/stage/base.py:64-66` (docstring `ResponseParser`),
`src/llm_bench/core/types.py:223-228` (объявления полей), `src/llm_bench/runner/round_runner.py:252-283`
(конструирование единственного `RunRow` в кодовой базе), `src/llm_bench/storage/postgres.py:98-103,
123-134,250-252,501-505` (DDL/INSERT/SELECT), `src/llm_bench/storage/file.py:378-381,391-393`
(JSONL round-trip).

**Description**: `stage/base.py:64-66` документирует контракт `ResponseParser`: «Returning `None`
signals an unrecoverable parse failure — **the pipeline records the row with `parse_failure_prefix`
set** and proceeds to the next stage…». Поле `RunRow.parse_failure_prefix` (`core/types.py:228`)
существует, имеет колонку в Postgres DDL (`postgres.py:102`), участвует в списке колонок INSERT
(`postgres.py:133,252`) и в чтении обратно (`postgres.py:505`).

Однако единственное место в продакшен-коде, где создаётся `RunRow` — `round_runner.py::_run_pipeline`,
строки 252-283 — **не устанавливает `parse_failure_prefix` никогда** (поле остаётся на дефолте `None`
из `core/types.py:228`), даже когда `_parse_safely` (round_runner.py:329-343) ловит исключение
парсера и возвращает `None` (строка 341-343 просто логирует и возвращает `None`, не сообщая об этом
наверх через какое-либо возвращаемое значение, которое затем попало бы в `row.parse_failure_prefix`).

Та же судьба у ещё четырёх полей: `logs`, `logs_compressed`, `http_status_sequence`,
`per_attempt_durations_sec` (`core/types.py:223-227`) — все они имеют полную DDL/INSERT/SELECT- и
JSONL-обвязку (`file.py:378-381,391-393`; `postgres.py:98-101,131-132,250-251,501-504`), но не
встречаются ни разу в конструкторе `RunRow(...)` в `round_runner.py:252-283`.

**Why it matters / concrete failure scenario**: (1) Прямая docstring-неточность: разработчик,
читающий `stage/base.py`, разумно ожидает, что может отличить «модель не ответила» от «модель ответила
мусором, парсер упал» через `row.parse_failure_prefix` — на практике поле всегда `None`, и эта
диагностика тихо теряется. (2) Это прямо усиливает риск находки Critical #1/#2 и High
(swallowed-exceptions): именно `parse_failure_prefix` мог бы стать сигналом для кастомного `RowScorer`,
отличающим «получен ответ, но распарсить не удалось» (сейчас `default_row_scorer` засчитывает такую
строку как **успех**, см. `ranker.py:66-70` — есть непустой `response` и нет `error_class`) от
реального провала. Консьюмер, доверяющий документации, построит скорер, который никогда не сработает
как задумано.

**Recommendation**: Либо прокинуть эти поля из фактических данных пайплайна (parser exception message
→ `parse_failure_prefix`; retry-историю провайдера → `http_status_sequence`/`per_attempt_durations_sec`,
если pyutilz их предоставляет), либо удалить неиспользуемые колонки/поля и docstring-обещание, чтобы
API не подразумевал несуществующую функциональность.

---

### [High] `PostgresStorage.schema_name`: динамический SQL без валидации, обоснование безопасности не подкреплено кодом

**File:Line**: `src/llm_bench/storage/postgres.py:39-120` (`_ddl`), `:137-172` (docstring класса,
`__init__`), `:203-441` (все 15 SQL-запросов с `# nosec B608`).

**Description**: Класс-докстринг (`postgres.py:146-153`) утверждает: «`self._schema` is set once at
construction time from an operator-supplied config value, not per-request user input, so this isn't
the SQL-injection pattern bandit's B608 heuristically flags». Формально `__init__` (строки 155-167)
принимает `schema_name: str = "llm_bench"` **без какой-либо проверки** — ни regex-allowlist
(`^[a-zA-Z_][a-zA-Z0-9_]*$`), ни сверки со списком разрешённых схем, ни экранирования через
`asyncpg`/`identifier quoting`. Далее это значение f-string-подставляется буквально во все DDL
(`_ddl()`, строки 39-120) и во все 15 CRUD/аналитических запросов (`upsert_prompts`, `record_call`,
`prefetch_resume_cache`, `get_cached`, `query_rows`, `query_spend_total`, `query_spend_by_stage`,
`persist_winners`, `load_winners`, `delete_experiment` — строки 203-441), каждый раз с
локальным `# nosec B608`.

Аргумент docstring'а — это утверждение о *способе использования снаружи*, а не инвариант, который
поддерживает сам код. Ничто не мешает интегратору написать
`PostgresStorage(url, schema_name=os.environ["LLM_BENCH_SCHEMA"])` (переменная окружения — вполне
«operator-supplied config value» по букве docstring'а, но полностью недоверенная в смысле безопасности,
если её кто-то может подставить) или, в мульти-тенантном сценарии, `schema_name=f"tenant_{tenant_id}"`,
где `tenant_id` пришёл из запроса пользователя (docstring прямо не запрещает такое использование, лишь
описывает «типичный» кейс VocabApp).

**Why it matters / concrete failure scenario**: Пока единственный известный потребитель
(VocabApp, согласно docstring'у) действительно задаёт `schema_name` константой в коде — риска нет. Но
как только `schema_name` попадает в конфиг/env/мульти-тенантный путь без явной проверки в самом
`PostgresStorage.__init__`, каждый из 15 сайтов с `# nosec B608` становится реальной SQL-инъекцией
(например, `schema_name = "public; DROP SCHEMA llm_bench CASCADE; --"` в CREATE SCHEMA-запросе на
`initialize()`). Библиотека не может контролировать, как её будут использовать в будущем — а
docstring-обоснование прямо приглашает читателя доверять входным данным без проверки.

Это преимущественно security-тема (полноценный разбор эксплуатируемости — в отдельном security-отчёте
аудита), но включено сюда, поскольку это ровно случай **несоответствия docstring/комментария реальному
коду** — заявленная гарантия безопасности не реализована как инвариант, а является недокументированным
допущением о вызывающей стороне.

**Recommendation**: Добавить в `__init__` простую regex-валидацию идентификатора Postgres
(`^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`) с явным `ValueError` при нарушении, либо использовать
`asyncpg`/`asyncpg.utils`-безопасное квотирование идентификатора вместо f-string. После этого
`# nosec B608`-подавления станут действительно обоснованными, а не только «по документации».

---

### [High] Проглоченные исключения в hot path — уже найдены собственным сканером, но не исправлены

**File:Line**: `src/llm_bench/runner/round_runner.py:109-118` (`_one_pair`), `:180-186`
(`_build_prompt`-вызов), `:329-343` (`_parse_safely`); baseline:
`tests/test_meta/_code_audit_baseline.json:16-18`.

**Description**: `tests/test_meta/test_code_audit_baseline.py` запускает pyutilz-овый generic
code-audit сканер, который **уже обнаружил** и занёс в `_code_audit_baseline.json` три
`log_only_except`-находки именно в этих местах (строки 16-18 baseline-файла:
`runner/round_runner.py:116`, `:184`, `:341`). Baseline-механизм («только НОВАЯ находка ломает CI»)
корректно предотвращает регресс, но не требует и не поощряет исправление уже найденного — три реальных
паттерна «поймали Exception, залогировали, тихо продолжили» остаются в hot path без дальнейшего
разбора.

Конкретно:
- `round_runner.py:109-118` (`_one_pair`) — оборачивает **весь** вызов `_run_pipeline` в
  `except Exception: logger.exception(...)`. Внутри `_run_pipeline` сетевые/провайдерские ошибки уже
  корректно обработаны на своём уровне (строки 226-231, `classify_provider_error`) — то есть до этого
  внешнего except добираются только **баги самого фреймворка/пользовательского кода** (например,
  необработанное исключение в `cfg.storage.record_call`/`resume_cache.put`). Такой сбой не создаёт
  `RunRow`, не попадает в `n_stages_attempted`, не виден нигде, кроме лога.
- `round_runner.py:180-186` — падение пользовательского `PromptBuilder` тихо пропускает этап
  (`continue`) для этой пары (model, task_unit) — без записи строки.
- `round_runner.py:329-343` (`_parse_safely`) — падение пользовательского `ResponseParser`
  возвращает `None` и лог; связано с находкой High про `parse_failure_prefix` — этот сигнал
  никуда не передаётся дальше по цепочке.

**Why it matters / concrete failure scenario**: Баг в пользовательском `PromptBuilder`, который
воспроизводимо падает **только** для конкретной модели (например, из-за неожиданной формы
`ctx.outputs` этой модели), тихо снижает `n_stages_attempted[model]` для этой модели — а это поле
напрямую участвует в coverage-gate `Halving.promote()` (`driver.py:104-118`, порог `coverage_min=0.7`
по умолчанию). Результат — модель может быть исключена из раунда по «недостаточному покрытию»,
хотя реальная причина — баг в общем коде, а не в самой модели; ни один лог не укажет на это как на
причину отсева при обычном чтении отчёта `RoundResult`.

**Recommendation**: Технически задача не в «убрать broad except» (в асинхронном `gather()`-паттерне
это оправдано, чтобы одна упавшая пара не завалила весь раунд) — а в том, чтобы **различать** ожидаемые
провайдерские сбои (уже размечены `error_class`) от неожиданных внутренних ошибок: последние должны
как минимум создавать `RunRow` с отдельным `error_class` (например, `"InternalPipelineError"`) вместо
полного отсутствия записи, чтобы `n_stages_attempted`/coverage-gate не искажались молча.

---

### [Medium] `# type: ignore[arg-type]` маскирует реальное расхождение типов `str`/`ExperimentTag`

**File:Line**: `src/llm_bench/runner/budget.py:49-63` (`BudgetGate.check`).

**Description**: `check(self, storage, experiment_tag: str, op: str)` типизирует `experiment_tag` как
голый `str`, но передаёт его в `storage.query_spend_by_op(experiment_tag=experiment_tag)`
(`storage/base.py:127-129`), где параметр типизирован как `ExperimentTag` (`NewType` над `str`,
`core/types.py:19`). mypy закономерно ругается на номинальное несовпадение `NewType`, и вместо
исправления сигнатуры добавлен `# type: ignore[arg-type]` (budget.py:63). Единственный вызывающий код
(`round_runner.py:202-205`, метод `_run_pipeline`) и так передаёт `cfg.experiment_tag: ExperimentTag` —
то есть реальный рантайм-тип всегда правильный, просто аннотация параметра `check()` занижена.

**Why it matters / concrete failure scenario**: Само по себе не баг сегодня (рантайм всегда получает
`ExperimentTag`), но `# type: ignore` выключает единственную защиту от будущей регрессии — если кто-то
когда-нибудь вызовет `check()` с обычной строкой из другого источника (не `ExperimentTag`), никакой
инструмент об этом не предупредит.

**Recommendation**: Изменить сигнатуру `check(self, storage, experiment_tag: ExperimentTag, op: str)`
и убрать `# type: ignore` — однострочное исправление без изменения поведения.

---

### [Medium] `# type: ignore[operator]` — mypy не сужает `Optional` через промежуточный bool

**File:Line**: `src/llm_bench/cost/estimator.py:126-138` (`estimate_call_cost`).

**Description**: `has_cache_pricing = cache_read_price_per_m is not None and cache_read_price_per_m > 0`
(строка 134) корректно проверяет `None`, но mypy не связывает истинность `has_cache_pricing` (отдельная
переменная) с последующим прямым обращением `cache_read_price_per_m / 1_000_000.0` в ветке `if
has_cache_pricing:` (строка 136) — отсюда `# type: ignore[operator]`.

**Why it matters / concrete failure scenario**: Как и выше — не баг сейчас (логика верна), но
подавление снимает защиту: если условие на строке 134 когда-нибудь отвяжут от переменной
`has_cache_pricing` (например, добавят ещё один флаг), деление на `None` больше не будет замечено
статически.

**Recommendation**: Переписать без промежуточного bool, например:
```python
p_cache_read = (cache_read_price_per_m / 1_000_000.0) if cache_read_price_per_m and cache_read_price_per_m > 0 else p_prompt
has_cache_pricing = p_cache_read is not p_prompt  # или отдельный явный флаг
```
или использовать `if cache_read_price_per_m is not None and cache_read_price_per_m > 0: p_cache_read =
cache_read_price_per_m / 1_000_000.0; has_cache_pricing = True` — mypy сузит тип внутри самого `if`.

---

### [Medium] `PostgresStorage` не покрыт вообще никаким тестом

**File:Line**: `tests/unit/test_storage_protocol.py:1-53` (особенно комментарии на строках 5, 37-38),
`tests/conftest.py:36-43` (`postgres_test_url`), `src/llm_bench/storage/postgres.py` (509 строк).

**Description**: Контрактный тест `test_storage_protocol.py` параметризован только по `memory` и
`file` бэкендам (фикстура `storage`, строки 33-53); комментарий на строке 37-38 явно отсылает к
несуществующему файлу: «PostgresStorage parametrization gated on LLM_BENCH_TEST_DB_URL — see
test_storage_protocol_postgres.py (Phase D follow-up)» — такого файла нет нигде в дереве репозитория
(проверено `Glob`). Фикстура `postgres_test_url` в `conftest.py:36-43` объявлена специально «для
тестов, помеченных `@pytest.mark.postgres`», но такого маркера ни на одном тесте нет (grep по всему
`tests/` — ноль совпадений `mark.postgres`). `pytest.mark.postgres` при этом объявлен в
`pyproject.toml` markers (см. `addopts`/`markers`) как будто активно используется.

**Why it matters / concrete failure scenario**: `postgres.py` — самый рискованный по объёму
кастомного SQL бэкенд (509 строк, 15 хэндрайтовых запросов, динамическая интерполяция схемы — см.
находку High выше), и при этом единственный из трёх backend'ов, чья идемпотентность
(`record_call`/`upsert_prompts`/`persist_winners`), tag-agnostic-кеш и `delete_experiment`-семантика
никогда не проверяются автоматически. Регрессия в SQL-запросе (опечатка в имени колонки, неверный
`$n`-placeholder, сломанная `ON CONFLICT`) была бы обнаружена только на реальной проде PostgreSQL.

**Recommendation**: Либо создать `tests/unit/test_storage_protocol_postgres.py` (файл, на который уже
ссылается комментарий), параметризующий тот же контрактный набор через `pytest.mark.postgres` +
`postgres_test_url`-фикстуру и пропускающий тест при отсутствии `LLM_BENCH_TEST_DB_URL`, либо, как
временная мера, добавить хотя бы SQL-syntax-smoke-тест (`asyncpg`-mock/`sqlparse`) на `_ddl()` и
собранные строки запросов.

---

### [Medium] `tests/property/` пуста — заявленная hypothesis-часть тестового набора не существует

**File:Line**: `tests/property/__init__.py` (единственный файл в директории), `pyproject.toml`
(`hypothesis>=6.0` в `[project.optional-dependencies].dev`).

**Description**: Директория `tests/property/` содержит только пустой `__init__.py`. `hypothesis`
объявлен зависимостью для разработки, но нигде в репозитории (`grep -r "hypothesis\|given(\|strategies"
tests/`) не встречается ни одного реального использования.

**Why it matters / concrete failure scenario**: Модули с богатой инвариантной логикой — отличные
кандидаты для property-тестов, которых явно не хватает: `core/hashing.py` (детерминированность/отличие
хэшей при разных system/user-разбиениях), `halving/schedule.py` (`n_calls_for_stage`/`next_round_size`
инварианты при произвольных `round_sizes`), `halving/pruner.py::mad_bootstrap_prune` (сохранение
свойства «эффективный скор монотонен по threshold»), `cost/estimator.py::estimate_call_cost`
(неотрицательность итоговой стоимости). Отсутствие реальных property-тестов — не баг сам по себе, но
заявленная (в структуре проекта и зависимостях) категория тестового покрытия фактически отсутствует,
что вводит в заблуждение относительно реального уровня QA.

**Recommendation**: Либо написать хотя бы базовые property-тесты для перечисленных выше модулей, либо
убрать `hypothesis` из dev-зависимостей и пустую директорию `tests/property/`, если она не планируется
в ближайшее время.

---

### [Medium] Fast-abort «3+ DEAD errors» в одном пайплайне игнорирует `RateLimited` — стоит подтвердить у автора

**File:Line**: `src/llm_bench/runner/round_runner.py:305-312`, `src/llm_bench/halving/alive_filter.py:44-90`,
`src/llm_bench/runner/classify.py:35-36`.

**Description**: `round_runner.py:308` использует `if error_class in DEAD_ERROR_CLASSES:` для
подсчёта «сколько DEAD-ошибок подряд получила эта (model, task_unit)-пара внутри одного пайплайна»,
и при достижении 3 — прерывает оставшиеся этапы этой пары (строки 310-312). `classify_provider_error`
(`classify.py:35-36`) возвращает строку `"RateLimited"` на HTTP 429/rate-limit-сообщения, но
`"RateLimited"` **намеренно отсутствует** в `DEAD_ERROR_CLASSES` (подтверждено:
`tests/unit/test_halving.py:163-177`, `test_transient_classes_meaningful`, явно документирует и
тестирует этот дизайн для **другой** функции — `is_alive_candidate`, которая отдельно считает
`TRANSIENT_ERROR_CLASSES`-класс ошибок через собственный порог).

Это значит: 3+ подряд `RateLimited`-ошибки в рамках **одного** (model, task_unit)-пайплайна НЕ
прервут оставшиеся вызовы этой пары — раннер продолжит пытаться выполнить оставшиеся этапы графа для
той же модели, скорее всего получая тот же 429 снова и снова.

**Alternative reading**: Это может быть намеренным дизайн-решением: rate-limit — не признак «сломанной»
модели (в отличие от `ContextOverflow`/`ModelNotFound`), и попытка следующего этапа теоретически может
успеть, пока троттлинг снят (особенно если между вызовами есть другая задержка от `asyncio.gather` по
другим парам). Также возможно, что `pyutilz`'s `tenacity`-слой уже делает retry/backoff **до** того, как
исключение вообще долетает до `classify_provider_error` — в этом случае повторные попытки на уровне
`round_runner.py` были бы избыточны с точки зрения самого источника (429 из ретрая), но это не проверяемо
без доступа к исходникам `pyutilz`.

**Why it matters / concrete failure scenario**: Если предположение о «tenacity уже сделал retry» неверно,
пайплайн, попавший под троттлинг у одного провайдера, продолжает жечь HTTP-запросы (и потенциально
бюджет через `BudgetGate`, если `cost_usd` списывается даже за 429-ответы — не проверялось) по всем
оставшимся этапам вместо быстрого отказа, вместо того чтобы остановиться пораньше и дать шанс другим
парам в том же `asyncio.gather()`.

**Recommendation**: Подтвердить с автором: если решение осознанное — оставить как есть, но добавить
комментарий рядом с `round_runner.py:308`, поясняющий, почему `RateLimited` намеренно исключён именно
из ЭТОЙ (per-pipeline abort), а не только из `DEAD_ERROR_CLASSES` в целом (сейчас обоснование этого
конкретного использования нигде не написано — оно есть только для `is_alive_candidate`).

---

### [Low] Непроаннотированные параметры в приватных хелперах `cost/openrouter.py`

**File:Line**: `src/llm_bench/cost/openrouter.py:22` (`_per_token_to_per_m(value)`), `:41`
(`_normalise_uptime(value)`).

**Description**: Обе функции полностью типизируют возвращаемое значение (`-> float | None`), но
параметр `value` не имеет аннотации типа вовсе (не `Any`, а просто отсутствует). `disallow_untyped_defs
= false` в mypy-конфиге (pyproject.toml) это пропускает молча. Остальной модуль (и кодовая база в целом)
последовательно аннотирует все параметры.

**Recommendation**: `def _per_token_to_per_m(value: float | int | str | None) -> float | None:` (судя по
телу функции — `float(value)` внутри `try`) и аналогично для `_normalise_uptime`.

---

### [Low] «Голый» `dict` вместо `dict[str, Any]`

**File:Line**: `src/llm_bench/cost/openrouter.py:188` (`OpenRouterCatalogue.list_models`).

**Description**: `kwargs: dict = {...}` — непараметризованный `dict`. Везде в остальной кодовой базе
(`stage/base.py:27`, `storage/*.py` и т.д.) используется полная форма `dict[str, Any]` или
конкретные типы. Не ловится ruff/mypy при текущей конфигурации (`disallow_untyped_defs=false`), но
нарушает собственную конвенцию проекта.

**Recommendation**: `kwargs: dict[str, Any] = {...}` (импорт `Any` уже нужен файлу или тривиально
добавляется).

---

### [Low] Мёртвая секция `[[tool.mypy.overrides]] module = "tests.*"`

**File:Line**: `pyproject.toml` (`[[tool.mypy.overrides]]`), `.pre-commit-config.yaml:112-114`
(`mypy-blocking`, `entry: python -m mypy src/llm_bench`), `.github/workflows/mypy-full.yml:29`
(`package-name: llm_bench`).

**Description**: И pre-commit-хук `mypy-blocking`, и CI workflow `mypy-full.yml` запускают mypy
только против `src/llm_bench` (пакет), никогда против `tests/`. Секция `[[tool.mypy.overrides]] module
= "tests.*" \n ignore_errors = true` в `pyproject.toml` таким образом никогда не находит ни одного
файла для применения — подтверждено собственным выводом mypy при локальном запуске: `python -m mypy
src` печатает `note: unused section(s): module = ['tests.*']`.

**Why it matters**: Не баг (mypy честно предупреждает), но вводящая в заблуждение конфигурация:
читатель `pyproject.toml` разумно решит, что тесты **тоже** прогоняются через mypy (просто с
подавлением ошибок), тогда как на самом деле они не проверяются вовсе ни в одном из двух гейтов.

**Recommendation**: Либо реально запускать `mypy tests` (с `ignore_errors=true`, чтобы не блокировать
CI, но хотя бы фиксировать краши/синтаксические проблемы), либо убрать секцию как аспirational/неверную.

---

### [Low] Unicode-console-checker не видит f-строки/kwargs/конкатенацию

**File:Line**: `tests/test_meta/test_no_unicode_in_console_output.py:41-63` (`_audit_file`).

**Description**: Проверка (`node.args` → `isinstance(arg, ast.Constant)`, строка 50) ловит только
простые строковые литералы, переданные **позиционным** аргументом в `print()`/`logger.<level>()`.
`ast.JoinedStr` (f-строки), keyword-аргументы (`logger.info(msg="—")`, если бы такое было) и
`"a" + "—"`-конкатенации (`ast.BinOp`) не проверяются вовсе — запрещённый символ внутри статической
части f-строки (`f"результат: — {x}"`) проскочит незамеченным.

**Why it matters**: На сегодня в кодовой базе нет `logger.*(f"...")`-вызовов вообще (это отдельно
гарантируется `test_logger_lazy_formatting.py`, который специально запрещает f-строки в логгер-вызовах)
— то есть практическое пересечение риска сейчас нулевое для логов, но для голого `print()` (не
запрещённого отдельным правилом) f-строка с запрещённым символом прошла бы незамеченной.

**Recommendation**: Расширить `_audit_file`, чтобы также сканировать статические сегменты
`ast.JoinedStr.values` (аналогично тому, как `test_logger_lazy_formatting.py` уже разбирает
`ast.JoinedStr`, просто для другой цели) — паттерн для копирования уже есть в соседнем файле.

---

### [Low] Неточная формулировка комментария про «подмножество» в `alive_filter.py`

**File:Line**: `src/llm_bench/halving/alive_filter.py:75`.

**Description**: Комментарий «Subset of DEAD_ERROR_CLASSES that are TRANSIENT» неточен:
`TRANSIENT_ERROR_CLASSES` **не** является подмножеством `DEAD_ERROR_CLASSES` — `"RateLimited"` входит
только в `TRANSIENT_ERROR_CLASSES`. Это подтверждено как осознанное дизайн-решение отдельным тестом
(`tests/unit/test_halving.py:163-177`), поэтому это не логическая ошибка, а просто неточное слово
«subset» в комментарии.

**Recommendation**: Переформулировать, например: «Overlaps heavily with DEAD_ERROR_CLASSES but is
NOT a strict subset — `RateLimited` lives only here, see test_transient_classes_meaningful for why.»

---

### [Low] Статичная hand-maintained карта `_VENDOR_TO_FAMILY`

**File:Line**: `src/llm_bench/halving/pairing.py:38-82`.

**Description**: ~45 захардкоженных vendor-префиксов OpenRouter → «семья». Fallback на строке 94
(`_VENDOR_TO_FAMILY.get(vendor, vendor)`) корректно трактует незнакомый вендор как отдельную семью —
то есть отсутствующая запись не ломает независимость валидатора, только снижает точность SOFT
cross-family-предпочтения (Layer 2). Не баг, но список неизбежно будет тихо устаревать по мере
появления новых вендоров на OpenRouter, и ничего в репозитории не сигнализирует о необходимости его
обновлять (нет теста, сверяющего список с живым каталогом).

**Recommendation**: Не критично для v0.1.0; на будущее — можно логировать `INFO`, когда встречается
вендор вне карты, чтобы было видно в продакшен-логах, что список пора пополнить.

---

### [Info] README заявляет функциональность, для которой отсутствует ссылочная документация

**File:Line**: `README.md:9,20`, отсутствие `docs/`.

**Description**: `README.md:9` описывает «per-stage winner promotion» в первом предложении проекта,
`README.md:20` — «Validator-pairing for cross-family scoring when no gold-truth is available» как
отдельный пункт списка возможностей. Директория `docs/` в репозитории отсутствует полностью (проверено
`ls`), хотя (по вводным данным задания) ожидалась `docs/architecture.md`. Это, вероятно, в основном
предмет отдельного документационного аудита, но напрямую подтверждает масштаб находок Critical
#1/#2 выше: заявленные в README как реализованные функции не только не подключены к раннеру, но и не
имеют даже отдельного архитектурного документа, который мог бы явно пометить их как «в разработке».

---

### [Info] `console_scripts`-точка входа указывает на несуществующий модуль

**File:Line**: `pyproject.toml` (`[project.scripts]` → `llm-bench = "llm_bench.cli.main:main"`),
`src/llm_bench/cli/__init__.py` (единственный файл в `cli/`, однострочный docstring).

**Description**: `src/llm_bench/cli/main.py` не существует (подтверждено `Glob`). `setuptools` не
проверяет существование entry-point-цели на этапе `pip install`, поэтому установка пакета пройдёт
успешно, но запуск `llm-bench` из шелла упадёт с `ModuleNotFoundError: No module named
'llm_bench.cli.main'`. Ожидаемо для alpha-версии (0.1.0) с явно помеченными stub-пакетами
(`cli/`, `provider/`, `discovery/`, `confirmation/` — все четыре содержат только однострочный
docstring), но стоит зафиксировать явно, поскольку это единственная точка, где заявленный
пользовательский интерфейс проекта (CLI) физически не функционирует.

---

## Итог по категориям (для сверки с текстовым резюме в конце ответа)

- Critical: 2
- High: 6
- Medium: 5
- Low: 6
- Info: 2

**Общий тезис аудита**: у репозитория явно прослеживается повторяющийся паттерн — сложная,
хорошо задокументированная и часто изолированно юнит-тестированная логика (per-stage winner substrate,
validator independence pairing, MAD-bootstrap penalty-термы, latency-aware tiebreak) реализована
качественно на уровне отдельных модулей, но **не домонтирована** до единственной точки оркестрации
(`runner/round_runner.py` → `runner/benchmark.py`). Юнит-тесты проверяют эти модули в изоляции и
проходят, интеграционные/smoke-тесты проверяют лишь «happy path» без валидатор-стейджей/многораундового
substrate-сценария — поэтому вся эта категория багов невидима для существующего CI и обнаруживается
только сквозным чтением от docstring к единственному вызывающему коду.
