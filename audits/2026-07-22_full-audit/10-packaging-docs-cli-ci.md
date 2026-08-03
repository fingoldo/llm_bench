# Аудит: Пакетирование, документация, CLI и CI (llm_bench)

## Scope & method

**Прочитано целиком** (не выдержками): `README.md`, `pyproject.toml`, `CHANGELOG.md`,
`src/llm_bench/__init__.py`, `src/llm_bench/storage/__init__.py`, `src/llm_bench/core/types.py`,
`src/llm_bench/cost/filter.py`, `src/llm_bench/runner/benchmark.py`, `src/llm_bench/halving/schedule.py`,
`src/llm_bench/storage/file.py`, `src/llm_bench/storage/postgres.py`,
`src/llm_bench/{cli,provider,discovery,confirmation}/__init__.py`,
`.github/workflows/{ci,mypy-full,black-filtered,release}.yml`, `.pre-commit-config.yaml`,
`.env.example`, `.yamllint`, `tests/test_meta/test_version_consistency.py`,
`tests/test_meta/test_api_stability.py`, `tests/test_meta/test_no_top_level_side_effects.py`,
`tests/unit/test_storage_protocol.py`, `tests/conftest.py`,
`examples/job_app_cover_letter/{run.py,stages.py,job_pool.py}`,
`tests/integration/test_job_app_example.py`, `src/llm_bench.egg-info/{SOURCES.txt,entry_points.txt,requires.txt}`,
`LICENSE`.

**Команды, реально выполненные (read-only)**:
- `python -c "import inspect; ... Stage(...); ... run_phase signature"` — эмпирическая проверка
  сигнатур из Quick Start README.
- `which llm-bench` + `llm-bench` (установленный editable-пакет) — проверка консольного скрипта.
- `python -c "from llm_bench.cli.main import main"` — проверка существования модуля.
- `python -m pytest -m "not live" --no-cov -q -p no:randomly` — офлайн-сьют (122 теста).
- `python -m ruff check src`, `python -m bandit -r src -q`, `python -m mypy src/llm_bench`,
  `python -m interrogate src/llm_bench`, `python -m deptry .` — сверка заявленных «0 findings» с
  реальным текущим состоянием.
- `git log --oneline`, `git tag -l`, `git status`, `git log origin/main..HEAD` /
  `HEAD..origin/main` — сверка CHANGELOG.md с реальной опубликованной историей коммитов
  (репозиторий синхронизирован с `origin/main`, working tree чист).
- `grep -riE "flask|fastapi|streamlit|gradio|django|jinja2"` по `src/tests/examples` — проверка
  отсутствия web-UI слоя.

Ничего не изменялось, не форматировалось, не коммитилось. Файл отчёта — единственный созданный файл.

**UI surface**: подтверждено грепом — ни одного реального импорта flask/fastapi/streamlit/gradio/
django/jinja2 в `src/`, `tests/`, `examples/`. Все совпадения — ложные срабатывания
(`.gitignore`-шаблоны "# Django stuff:"/"# Flask stuff:", строка "fastapi" внутри тестовых
данных/фейковых LLM-ответов примера JobApp). **У проекта нет web-UI, что ожидаемо и не является
находкой.** Единственный заявленный пользовательский интерфейс — консольный скрипт `llm-bench`
(см. находку №1) и Python API.

## Summary table

| Severity | File:Line | Summary |
|---|---|---|
| High | pyproject.toml:70, src/llm_bench/cli/__init__.py:1 | Консольный скрипт `llm-bench` объявлен, но `llm_bench.cli.main` физически не существует — падает `ModuleNotFoundError` при каждом запуске (проверено эмпирически) |
| High | README.md:38-57 | Пример Quick Start в README не запускается «как есть» — два независимых `TypeError` (отсутствует `op=` у второго `Stage`, отсутствует обязательный `candidates=` у `run_phase`), подтверждено эмпирически |
| High | .github/workflows/ci.yml:29-47, tests/unit/test_storage_protocol.py:33-53 | `PostgresStorage` — production-backend — не имеет вообще никакого теста ни unit, ни integration; Postgres-контейнер в CI поднимается впустую |
| Medium | src/llm_bench/storage/postgres.py:155-167 | `schema_name` конструктора `PostgresStorage` не валидируется (нет allowlist/regex) перед f-string-интерполяцией в SQL/DDL; безопасность держится только на докстринге, а не на коде |
| Medium | .github/workflows/ci.yml:94-134 vs .pre-commit-config.yaml | CI-джоба `lint` полностью `continue-on-error: true` (ruff/black/bandit); 6 «blocking» pre-commit хуков (vulture/interrogate/deptry/codespell/yamllint/zizmor) вообще не имеют аналога в GitHub Actions |
| Medium | CHANGELOG.md:6-22 vs git log | CHANGELOG описывает только самый первый коммит («repo skeleton»); 20+ реально запушенных коммитов (halving/ranking/runner/storage-backends/JobApp-пример/весь CI-hardening) не отражены |
| Medium | .github/workflows/release.yml:43-49 | Проверка «git tag == package version» пропускается для `workflow_dispatch` — ручной запуск публикует в PyPI без этой защиты |
| Low | README.md:26 | Ссылка на `docs/architecture.md` — директории `docs/` в репозитории не существует вовсе |
| Low | tests/test_meta/test_code_audit_baseline.py | Офлайн-сьют (та самая команда `pytest -m "not live"` из README) на текущем HEAD **падает** с 1 новой находкой `docstring_args_incomplete` в cost/estimator.py:161 |
| Low | CHANGELOG.md:20 vs pyproject.toml:37 | CHANGELOG утверждает «Hard dep on pyutilz>=1.1», pyproject.toml фактически пинит `>=1.0` (с явным комментарием почему) |
| Info | pyproject.toml:72-74, src/llm_bench.egg-info/SOURCES.txt | 4 пустые подпакета (`cli/`, `provider/`, `discovery/`, `confirmation/`) реально попадают в собранный wheel |
| Info | git tag -l (пусто) | Тегов/релизов ещё не было — то, что CHANGELOG держит всё под `[Unreleased]`, само по себе корректно |

## Findings

### 1. [High] Консольный скрипт `llm-bench` объявлен в pyproject.toml, но не существует — падает при каждом запуске

**File:Line**: `pyproject.toml:69-70` (`[project.scripts]\nllm-bench = "llm_bench.cli.main:main"`),
`src/llm_bench/cli/__init__.py:1` (весь файл — однострочный докстринг `"""cli subpackage."""`,
больше ничего).

**Description**: `pyproject.toml` регистрирует консольный entry point `llm-bench`, указывающий на
`llm_bench.cli.main:main`. Файла `src/llm_bench/cli/main.py` в репозитории нет — подкаталог `cli/`
целиком состоит из одного `__init__.py` с однострочным докстрингом, без единой функции. То же
самое верно для `provider/`, `discovery/`, `confirmation/` — это чистые заглушки-скелеты.

Проверено эмпирически на реально установленном (editable) пакете в этом окружении:

```
$ llm-bench
Traceback (most recent call last):
  ...
  File "...\Scripts\llm-bench.exe\__main__.py", line 2, in <module>
    from llm_bench.cli.main import main
ModuleNotFoundError: No module named 'llm_bench.cli.main'

$ python -c "from llm_bench.cli.main import main"
ModuleNotFoundError: No module named 'llm_bench.cli.main'
```

Сгенерированный `src/llm_bench.egg-info/entry_points.txt` подтверждает, что именно эта (нерабочая)
точка входа реально прописывается в метаданных пакета при установке:
```
[console_scripts]
llm-bench = llm_bench.cli.main:main
```

**Why it matters / сценарий отказа**: любой пользователь, который сделает `pip install llm-bench`
(или `pip install -e .`) и попробует запустить `llm-bench` из командной строки (естественное
ожидание от пакета с зарегистрированным console-script — README нигде явно не отговаривает от
этого, наоборот, само наличие entry point'а сигнализирует, что CLI существует) — получит
немедленный краш с непонятным для конечного пользователя traceback. Смягчающее обстоятельство: сам
README Quick Start ведёт пользователя исключительно через Python API (`from llm_bench import
Benchmark, ...`), а не через `llm-bench` CLI, так что человек, ограничившийся READMEом, эту
проблему не встретит. Но `pip check` / `twine check` / автоматизированный packaging-lint эту
поломку не ловят (entry point синтаксически валиден, ссылается на существующий модуль-путь без
проверки его импортируемости) — только реальный запуск.

**Recommendation**: либо реализовать минимальный `llm_bench/cli/main.py:main()` (даже просто
`print("llm-bench CLI is not implemented yet — use the Python API", file=sys.stderr); sys.exit(1)`
на первое время, чтобы вместо `ModuleNotFoundError` пользователь видел осмысленное сообщение),
либо временно убрать `[project.scripts]` из `pyproject.toml` до появления реальной реализации.
Второй вариант чище для alpha-статуса: не обещать то, чего нет.

---

### 2. [High] Пример Quick Start в README.md не запускается «как есть» — два независимых TypeError

**File:Line**: `README.md:38-57`.

**Description**: Флагманский пример из README:

```python
from llm_bench import Benchmark, Stage, StageGraph, HalvingSchedule, CostFilter
from llm_bench.storage import FileStorage

bench = Benchmark(
    task_pool=my_pool,
    stages=StageGraph([
        Stage(id="draft", op="draft",
              prompt_builder=build_draft, parser=parse_draft,
              budget_per_call=0.50),
        Stage(id="validate_draft", parent_stage="draft", is_validator=True,
              prompt_builder=build_validate, parser=parse_validate,
              budget_per_call=0.10),
    ]),
    storage=FileStorage(root="./benchmark_runs"),
    cost_filter=CostFilter(max_output_price_per_m=2.0, min_context_length=64000),
    halving_schedule=HalvingSchedule(round_sizes=(20, 10, 5), units_per_arm=(3, 6, 10)),
)
report = await bench.run_phase(tag="exp_v1", rounds=[1, 2, 3])
```

Импорты (`Benchmark, Stage, StageGraph, HalvingSchedule, CostFilter` из `llm_bench`, `FileStorage`
из `llm_bench.storage`) — рабочие, они действительно реэкспортируются
(`src/llm_bench/__init__.py:11-19,28,31`, `src/llm_bench/storage/__init__.py:3-13`). Но сам код
падает в двух местах, оба подтверждены эмпирически интроспекцией реальных сигнатур:

1. Второй `Stage(...)` (строки 48-50) не передаёт `op=` — а это обязательный (без значения по
   умолчанию) второй позиционный параметр датакласса `Stage`
   (`src/llm_bench/core/types.py:63-91`, поле `op: str` идёт сразу после `id: str`, до всех полей
   со значением по умолчанию):
   ```
   >>> Stage(id="validate_draft", parent_stage="draft", is_validator=True,
   ...       prompt_builder=..., parser=..., budget_per_call=0.10)
   TypeError: Stage.__init__() missing 1 required positional argument: 'op'
   ```
2. `bench.run_phase(tag="exp_v1", rounds=[1, 2, 3])` (строка 56) не передаёт `candidates=` — а это
   обязательный keyword-only параметр без значения по умолчанию
   (`src/llm_bench/runner/benchmark.py:206-213`):
   ```python
   async def run_phase(
       self, *,
       tag: str | ExperimentTag,
       candidates: list[str],     # <- нет default, обязателен
       rounds: list[int] | None = None,
       ...
   ```
   Реальная сигнатура, полученная через `inspect.signature`:
   `(self, *, tag: 'str | ExperimentTag', candidates: 'list[str]', rounds: 'list[int] | None' = None, ...)`.

Показательно, что реальный рабочий пример в этом же репозитории —
`examples/job_app_cover_letter/{stages.py:183-210, run.py:172}` — оба раза делает всё правильно:
каждый `Stage(...)` всегда передаёт `op=`, а `run_phase(tag=tag, candidates=candidates)` всегда
передаёт `candidates=`. Это доказывает, что README-сниппет просто не тестируется /
не синхронизируется с эволюцией API, в отличие от `examples/`, которые покрыты
`tests/integration/test_job_app_example.py` и реально гоняются в CI.

**Why it matters**: это первый код, который увидит и попробует скопипастить новый пользователь —
и он падает на первой же строчке конструирования пайплайна, ещё до вызова `run_phase`. Для
alpha-библиотеки, которая продаёт себя через «cost-rank discovery + Sequential Halving», нерабочий
Quick Start — это прямой удар по доверию и первому опыту использования.

**Recommendation**: добавить сам README Quick Start (или его точную копию) как doctest/smoke-тест
в CI (например, extract-and-exec блока кода из README в отдельном meta-тесте, как уже делается для
API-stability snapshot), чтобы регресс в примере ловился автоматически при следующем изменении
сигнатур `Stage`/`run_phase`. Немедленно — добавить `op="validate_draft"` во второй `Stage` и
`candidates=[...]` в вызов `run_phase`.

---

### 3. [High] `PostgresStorage` — не имеет вообще никакого тестового покрытия (ни unit, ни integration)

**File:Line**: `.github/workflows/ci.yml:29-47` (Postgres service), `pyproject.toml:88` (маркер
`postgres`), `tests/conftest.py:36-43` (фикстура `postgres_test_url`),
`tests/unit/test_storage_protocol.py:1-53` (параметризация бэкендов), `.env.example:23-25`.

**Description**: `ci.yml` на каждый прогон CI (все PR/push) поднимает реальный сервис
`postgres:16` с health-check'ом, пробрасывает порт 5432 и передаёт
`LLM_BENCH_TEST_DB_URL=postgresql://postgres:postgres@localhost:5432/llm_bench_test` в окружение
джобы `test` (строки 29-47). `pyproject.toml:88` объявляет маркер `postgres: marks tests requiring
a Postgres test DB at LLM_BENCH_TEST_DB_URL`. `tests/conftest.py:36-43` даёт fixture
`postgres_test_url()`, читающую именно эту переменную, с явным комментарием «Tests marked
`@pytest.mark.postgres` should request this fixture».

Но реально ни один тест в репозитории не использует ни этот маркер, ни `PostgresStorage`:
- `tests/unit/test_storage_protocol.py:33-53` — фикстура `storage`, параметризующая контрактные
  тесты по бэкендам, содержит только `"memory"` и `"file"`; третья ветка для Postgres отсутствует,
  вместо неё — комментарий-заглушка (строки 5-6, 37-38): *«PostgresStorage (Phase D — gated on
  LLM_BENCH_TEST_DB_URL env)»* и *«see test_storage_protocol_postgres.py (Phase D follow-up)»*.
- Файла `test_storage_protocol_postgres.py` в репозитории нет нигде (проверено полнотекстовым
  поиском).
- `.env.example:23` и `ci.yml:31` оба, независимо друг от друга, комментируют Postgres-переменную
  ссылкой на `tests/integration/test_smoke_postgres.py` — этого файла тоже нет нигде в дереве
  `tests/` (проверено `find tests -iname "*postgres*"` — пусто).
- Полнотекстовый grep `PostgresStorage` по `tests/` не находит ни одного использования класса.

**Why it matters / сценарий отказа**: `PostgresStorage` — это именно тот бэкенд, который README
позиционирует как «production backend» (первая строчка README: «three storage backends (Postgres /
file / in-memory)», и «Postgres for production» в разделе Why). Ни DDL-операторы
(`CREATE SCHEMA`/`CREATE TABLE ... IF NOT EXISTS`), ни `ON CONFLICT DO NOTHING`-логика идемпотентных
вставок, ни JSONB-роундтрип (`_to_json`/`_maybe_json` в `storage/postgres.py:448-468`), ни — что
особенно значимо для этого аудита — сама f-string-интерполяция `self._schema` (см. находку №4),
никогда не выполнялись против реального сервера Postgres ни в одном CI-прогоне. Регрессия в SQL
(опечатка в имени колонки, неверный порядок `$n`-плейсхолдеров относительно 37 позиционных
аргументов в `record_call`, ошибка в JOIN/constraint) была бы обнаружена только на реальном
проде/у первого консьюмера, не в CI. Одновременно поднятый в CI Postgres-контейнер — чистые
потраченные впустую CI-минуты и заряд контейнера на каждый прогон, поскольку к нему никто не
подключается.

**Recommendation**: либо дописать `tests/integration/test_smoke_postgres.py` (файл уже дважды
анонсирован в комментариях, но не создан) и параметризовать `test_storage_protocol.py`'s `storage`
fixture третьим вариантом `"postgres"`, гейтя его через `postgres_test_url()`/`@pytest.mark.postgres`
(инфраструктура для этого уже полностью готова — просто не подключена), либо, если реализация
осознанно отложена, убрать Postgres-сервис из `ci.yml` до появления тестов, чтоббы не создавать
ложное ощущение «эта часть тоже покрыта CI».

---

### 4. [Medium] `PostgresStorage.schema_name` не валидируется перед f-string-интерполяцией в SQL

**File:Line**: `src/llm_bench/storage/postgres.py:137-167` (класс + `__init__`), затем 13 сайтов
`# nosec B608` по всему файлу (строки 43-119 в `_ddl()`, 203-437 во всех CRUD-методах).

**Description**: Как и просил бриф аудита — прочитал файл целиком и оценил заявление докстринга
независимо. Класс принимает `schema_name: str = "llm_bench"` (строка 159) без какой-либо проверки
формата, сохраняет его как `self._schema` (строка 164) и затем f-string-интерполирует буквально в
каждый SQL/DDL statement по всему файлу — начиная с `CREATE SCHEMA IF NOT EXISTS {s};` в `_ddl()`
(строка 43) и заканчивая `DELETE FROM {s}.benchmark_winners WHERE experiment_tag=$1` (строка 437).
Докстринг класса (строки 146-153) аргументирует безопасность так: *«self._schema is set once at
construction time from an operator-supplied config value, not per-request user input, so this
isn't the SQL-injection pattern bandit's B608 heuristically flags»*.

Это заявление верно ТОЛЬКО как утверждение о текущем способе использования класса — оно нигде не
закреплено кодом. В `__init__` (строки 155-167) нет:
- проверки regex/allowlist на допустимые символы (`^[A-Za-z_][A-Za-z0-9_]*$`),
- ограничения длины,
- использования `asyncpg`/`psycopg`-style `quote_ident()`/санитайзера идентификаторов.

Ничто в самом классе не мешает вызывающему коду передать `schema_name` из переменной окружения
(вполне реалистичный паттерн — сам README ничего не говорит о том, что `schema_name` нельзя
конфигурировать через `.env`/`os.environ`), из конфиг-файла, который сам частично собирается из
менее доверенного источника, или (в гипотетическом будущем multi-tenant-развёртывании) из
per-tenant-идентификатора. В любом из этих случаев docstring-предположение «operator-supplied, not
per-request user input» перестаёт быть верным, а код — нет, потому что сам класс никак не проверяет
это на границе.

Отдельно проверил бандитом (`bandit -r src -q`): на -ll (medium+) находок 0 — совпадает с
заявлением о «closed to 0 findings» в `.pre-commit-config.yaml:120-124`; на любом уровне severity
находится 19 Low-находок, все — не про этот паттерн (`B101 assert_used`, не относится к SQL).
Т.е. текущая линтинг-конфигурация (`-ll`) действительно молчит про эту конструкцию именно потому,
что `# nosec B608` подавляет её точечно — что абсолютно ожидаемо и не является отдельной находкой:
сам факт подавления bandit'а тут не бага, а её обоснованность — вопрос дизайна, разобранный выше.

**Why it matters / сценарий отказа**: если когда-либо `PostgresStorage(url=..., schema_name=X)`
будет вызван с `X`, полученным из значения, которое не полностью контролируется оператором на
этапе деплоя (например: `schema_name=os.environ["TENANT_SCHEMA"]` в будущей multi-tenant версии
VocabApp/другого консьюмера, где `TENANT_SCHEMA` формируется из tenant-slug'а, полученного от
пользователя) — получаем классическую SQL-инъекцию в DDL/DML произвольного масштаба (от
`CREATE SCHEMA IF NOT EXISTS`, буквально исполняемого при каждом `initialize()`, до всех
CRUD-запросов). Ничего в текущем коде не предотвратит это на уровне библиотеки — вся защита
держится на дисциплине вызывающего кода, которая нигде не проверяется во время выполнения.

Также см. находку №3: поскольку `PostgresStorage` не покрыт ни одним тестом, даже базовый
happy-path (не говоря об adversarial input) для этого класса никогда не проверялся автоматически.

**Recommendation**: добавить в `__init__` простую, дешёвую защиту в глубину — например:
```python
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema_name):
    raise ValueError(f"schema_name must be a valid SQL identifier, got {schema_name!r}")
```
Это не отменяет валидность текущего docstring-обоснования как модели угроз «на сегодня», но
переводит её из «предположение, которое легко нарушить одной опечаткой конфигурации» в
«инвариант, гарантированный кодом» — трёх строк достаточно, реальной стоимости в производительности
или гибкости не несёт (легитимные имена схем Postgres и так ограничены этим форматом без
дополнительного quoting).

---

### 5. [Medium] Реальный CI не блокирует то, что pre-commit документирует как «blocking» — заметный gap между локальными хуками и GitHub Actions

**File:Line**: `.github/workflows/ci.yml:94-134` (джоба `lint`), `.pre-commit-config.yaml` (хуки
`bandit-blocking:125-133`, `vulture-blocking:138-146`, `interrogate-blocking:152-160`,
`deptry-blocking:168-175`, `codespell-blocking:180-187`, `yamllint-blocking:191-199`,
`zizmor-blocking:208-216`).

**Description**: `.pre-commit-config.yaml` явно и многократно называет семь хуков «Blocking» —
`bandit-blocking`, `vulture-blocking`, `interrogate-blocking`, `deptry-blocking`,
`codespell-blocking`, `yamllint-blocking`, `zizmor-blocking` (плюс `ruff-real-bugs` и
`mypy-blocking`, но `mypy` отдельно реально гейтится в CI через `mypy-full.yml`, так что этот один
не в списке проблемы). Комментарии в этом файле неоднократно утверждают «Closed to 0 findings
2026-07-11» для bandit (строка 121), vulture (строка 136), interrogate (строка 149), deptry
(строка 165), codespell (строка 178), yamllint (строка 190), zizmor (строка 205) — то есть
разработчик явно рассчитывает на них как на реальный, работающий gate.

Но в `.github/workflows/ci.yml` джоба `lint` (строки 94-134) запускает только `ruff`, `black`
(через `py_ci_shared.black_filtered_apply`) и `bandit` — и все три шага имеют
`continue-on-error: true` (строки 114, 123, 127). Ни один из них физически не может провалить
джобу или заблокировать merge через этот workflow; отчёт bandit просто загружается как artifact
(строки 129-134) для последующего ручного просмотра.

Что важнее: **vulture, interrogate, deptry, codespell, yamllint и zizmor вообще не запускаются ни
в одном `.github/workflows/*.yml`** — ни в `ci.yml`, ни в `mypy-full.yml`, ни в `black-filtered.yml`,
ни в `release.yml` (все четыре файла прочитаны целиком, других воркфлоу в `.github/workflows/` нет).
Единственное место, где эти шесть проверок вообще исполняются — локальный `pre-commit`, а сам
`.pre-commit-config.yaml` документирует официальный обход: *«To skip in a hurry: `git commit
--no-verify`»* (строки 6-7).

**Why it matters / сценарий отказа**: PR, открытый через GitHub UI (без локального клона и
`pre-commit install`), или запушенный контрибьютором, который не устанавливал pre-commit локально,
или запушенный с `--no-verify` — пройдёт весь GitHub-side CI зелёным (при условии, что required
status checks настроены на существующие джобы) полностью без единой проверки dead-code
(`vulture`), docstring-coverage (`interrogate`), dependency-hygiene (`deptry`), опечаток
(`codespell`), YAML-стиля (`yamllint`) или security workflow-файлов (`zizmor`) — несмотря на то,
что все семь описаны в репозитории как «blocking». Проверил сам это на актуальном коде: прогнал
`deptry .` и `interrogate src/llm_bench` напрямую — оба сейчас чисты (0 findings / 68.5% ≥ 65%
порог), т.е. риск сегодня латентный, а не проявленный — но защитный механизм, которым разработчик,
судя по комментариям, явно доверяет как «gate», реально не является таковым за пределами локальной
машины конкретного контрибьютора.

**Recommendation**: либо добавить в `ci.yml` (или отдельный `quality.yml`) реальные, блокирующие
(без `continue-on-error`) шаги для этих семи проверок — зеркалируя то, что уже настроено локально в
pre-commit (это, кажется, наиболее естественный fix, раз вся инфраструктура/конфигурация уже
существует и по утверждениям в комментариях «закрыта к 0 находкам»), либо честно понизить формулировки
в `.pre-commit-config.yaml` с «blocking» до «locally-enforced, not yet mirrored in CI», чтобы не
создавать ложного впечатления защищённости у ревьюеров, которые полагаются на зелёный CI-бейдж.

---

### 6. [Medium] CHANGELOG.md существенно устарел относительно реальной истории коммитов

**File:Line**: `CHANGELOG.md:6-22` (единственная секция `[Unreleased]`).

**Description**: `git log --oneline` на `origin/main` (репозиторий синхронизирован, working tree
чист — `git status` подтверждает «up to date with origin/main», `git log origin/main..HEAD` и
обратный diff оба пусты) показывает 22 коммита от `97026ee` («Phase A: repo skeleton + storage
Protocol + 24 passing tests») до `965dc9b` («fix(ci): bump black-filtered.yml pin to v1.2.1»),
включая:
- `c1ebb66` Phase B: halving + cost_ranker + per_stage_winners
- `2ce6f83` Phase C: ranker + GoldChecker Protocol blend
- `61f7849` Phase D: FileStorage (JSONL + sqlite) + PostgresStorage (asyncpg)
- `255609e` Phase E: runner subpackage (Benchmark facade + run_round + ResumeCache + BudgetGate + classify_provider_error)
- `bc56889` Phase G: JobApp POC example
- `452e5ce` Benchmark.preflight()
- `4f710f8`, `293c03e`, `19ae719`, `de99b78`, `3066e2f`, `05ad2f5`, `6f3342d`, `05af8e3`, `965dc9b` —
  весь CI/quality-gate buildout (uv+pyutilz install, ruff-base.toml adoption + real fixes, zizmor
  hardening, pre-commit parity, mypy/black gates, code_audit baseline wiring, filtered-black debt
  cleanup).

`CHANGELOG.md`'s единственная секция `[Unreleased]` (строки 6-22) описывает только содержимое
самого первого, «skeleton»-коммита: пакетную структуру (`core/`, `pool/`, ... `cli/`),
`BenchmarkStorage` Protocol с тремя реализациями, и bootstrap-файлы конфигурации. Ничего из
реальной имплементации halving/ranking/runner, из фактического появления `FileStorage`/
`PostgresStorage`, из JobApp-примера, ни из последующего девятикоммитного CI-hardening'а в
CHANGELOG не попало.

**Why it matters**: сам файл заявляет формат «Keep a Changelog» (строка 4), подразумевающий, что
он — источник правды о том, что реально изменилось. Читатель, доверяющий CHANGELOG, узнает из него
только о структуре каталогов и абстрактном Protocol'е — но не о том, что `FileStorage` и
`PostgresStorage` реально реализованы и работают, что есть готовый `Benchmark`-runner, или что есть
рабочий сквозной пример (JobApp). Это прямо противоречит заявленной пользовательской привычке вести
CHANGELOG синхронно с изменениями кода.

**Recommendation**: развернуть `[Unreleased]` в честный список того, что реально появилось за 21
коммит после skeleton'а (halving/ranking engine, `runner` пакет, оба конкретных storage-бэкенда,
JobApp-пример, весь CI/quality-tooling), сгруппировав по подсистемам — это не требует
git-tag/релиза, просто актуализации текста.

---

### 7. [Medium] `release.yml`: проверка «тег == версия пакета» пропускается при ручном `workflow_dispatch`

**File:Line**: `.github/workflows/release.yml:13-16` (триггеры), `:43-49` (guard-степ), `:63-81`
(джоба `publish`).

**Description**: `release.yml` триггерится и на `release: types: [published]`, и на
`workflow_dispatch` (строки 14-16). Единственная защита от несогласованного релиза — степ «Assert
git tag matches package version» (строки 43-49), сверяющий `GITHUB_REF_NAME` (тег вида `vX.Y.Z`) с
`[project].version` из `pyproject.toml` — но этот степ явно ограничен условием `if:
github.event_name == 'release'` (строка 44), с комментарием «Skipped for manual dispatch runs.»
(строка 42). После него джоба `build` безусловно билдит и валидирует дистрибутивы, а джоба
`publish` (строки 63-81) безусловно `needs: build` и публикует в PyPI через Trusted Publishing —
никакого дополнительного условия на event_name на публикующей джобе нет.

**Why it matters / сценарий отказа**: любой, у кого есть право запускать `workflow_dispatch` в
этом репозитории (обычно — участники с write-доступом), может вручную запустить `release.yml` с
любого ref/ветки — и опубликовать в PyPI версию из `pyproject.toml`, которая не соответствует
никакому git-тегу вообще (например, версию, которая уже была опубликована ранее под другим
содержимым коммита, или версию, которую ещё рано публиковать). Единственная страховка,
специально спроектированная именно для этого сценария несогласованности (guard-степ), намеренно
выключена для этого же самого пути запуска. Насколько это реально эксплуатируемо — зависит от
настроек GitHub Environment `pypi` (protection rules/required reviewers), которые не видны из
файлов репозитория и не проверялись в рамках этого аудита — это не файловая, а
организационная/UI-настройка GitHub, вне зоны того, что можно подтвердить чтением кода.

**Recommendation**: либо распространить guard и на `workflow_dispatch` (сравнивая текущий
`pyproject.toml`-version с последним существующим git-тегом на ref, откуда запущен workflow, и
требуя явного `workflow_dispatch`-инпута с ожидаемой версией для подтверждения), либо явно
задокументировать в самом workflow (не только в комментарии), что ручной dispatch — заведомо
«break-glass»-путь, зарезервированный за GitHub Environment approval gate, и подтвердить, что этот
approval gate реально настроен в репозитории.

---

### 8. [Low] README.md ссылается на `docs/architecture.md`, которого не существует

**File:Line**: `README.md:26`.

**Description**: *«`0.1.0` — alpha. API is stabilizing. See
[docs/architecture.md](docs/architecture.md).»* Полнотекстовый поиск по всему репозиторию
подтверждает, что каталога `docs/` не существует нигде: единственное совпадение по строке `docs/`
во всём дереве (помимо самого README) — `.gitignore:72: docs/_build/`, стандартный boilerplate-паттерн
для игнорирования Sphinx build output, который не доказывает, что исходники `docs/` вообще когда-либо
существовали или планировались через Sphinx конкретно.

**Why it matters**: битая ссылка в самом коротком, самом читаемом разделе README — единственном
месте, где заявлено, что подробности архитектуры вынесены отдельно. Читатель, который хочет понять
устройство фреймворка глубже, чем позволяет README (для проекта с такой архитектурной сложностью —
halving/ranking/resume-cache/three storage backends — это довольно вероятный сценарий), получает
404 на GitHub.

**Recommendation**: либо написать `docs/architecture.md` (даже конспективный документ — обзор
core/halving/ranking/runner/storage layering — был бы полезен, учитывая явно выраженную в коде
слоистую архитектуру, см. комментарий `core/types.py:3-6` про «core sits at the bottom, all roads
point downward»), либо убрать ссылку из README до тех пор, пока документ не появится.

---

### 9. [Low] Офлайн pytest-сьют (команда из самого README) сейчас красный — 1 упавший meta-тест

**File:Line**: `tests/test_meta/test_code_audit_baseline.py` (тест `test_no_new_code_audit_findings`),
находка указывает на `src/llm_bench/cost/estimator.py:161-203` (сигнатура и докстринг
`estimate_top_n_by_cost`).

**Description**: Выполнил ровно то, что рекомендует README (`pytest -m "not live"`, а также
отдельно эквивалент `pytest tests/test_meta/`, упомянутый в README тремя строчками ниже) на
текущем `origin/main` HEAD (`965dc9b`, working tree чист). Результат: **121 passed, 1 failed** —
`tests/test_meta/test_code_audit_baseline.py::test_no_new_code_audit_findings`:

```
Failed: 1 new static-analysis finding(s) from pyutilz.dev.code_audit:
  docstring_args_incomplete [Low] cost/estimator.py:161 --
    `estimate_top_n_by_cost`'s docstring has an `Args:` section but
    omits parameter(s) ['refresh'] -- a caller reading the docstring
    has no idea these exist.
```

Проверил находку по существу: `estimate_top_n_by_cost` (`cost/estimator.py:161-171`) действительно
имеет параметр `refresh: bool = False` (строка 169), но секция `Args:` докстринга
(`cost/estimator.py:174-199`) документирует `catalogue`, `n`, `profile`, `filter_`,
`include_reasoning_models`, `include_topn_best_by_provider`, `blacklist` — и не упоминает `refresh`
вообще. Проверил также, что это не устаревший false-positive: baseline-файл
`tests/test_meta/_code_audit_baseline.json` для `cost/estimator.py` содержит только два
несвязанных заранее принятых finding'а (`default_via_or::cost/estimator.py:204` и `:207`) — записи
про `docstring_args_incomplete` там нет вовсе, т.е. это подлинная, не учтённая в baseline находка.

`ci.yml`'s джоба `meta` (строки 136-161) запускает именно эту команду (`pytest tests/test_meta/
--no-cov -p no:randomly -q`) без `continue-on-error` — то есть эта находка сейчас реально красит CI
на `main`.

**Caveat / альтернативное прочтение**: я запускал это против локального sibling-чекаута `pyutilz`
(обновлён 2026-07-21, независимо от `llm_bench`), а не против точного `git clone --depth 1`,
который CI делает при каждом запуске. Существует небольшая вероятность, что именно эта конкретная
проверка (`docstring_args_incomplete`) появилась в `pyutilz.dev.code_audit` недавно и ещё не была
«принята»/добавлена в baseline llm_bench специально по этой причине версийного дрейфа между
чекаутами — то есть находка может быть свежей не по вине llm_bench, а по вине более свежего
pyutilz. Но сам докстринг-пробел в `cost/estimator.py` — реальный и не зависит от этой
интерпретации: параметр `refresh` действительно не задокументирован.

**Why it matters**: сама находка тривиальна (одна строка докстринга), но факт, что стандартная,
рекомендованная в README команда прямо сейчас возвращает ненулевой exit code — это по существу CI
hygiene issue: либо `main` реально сейчас красный на джобе `meta`, либо (если проблема
воспроизводится только локально из-за версии sibling-пакета) — репозиторий хрупок к дрейфу версий
`pyutilz`, что тоже стоит знать.

**Recommendation**: добавить `refresh: bool = False -- when True, forces a fresh catalogue reload
bypassing any cache` (или аналогичное) в `Args:`-секцию докстринга `estimate_top_n_by_cost`
(`cost/estimator.py:174`) — тривиальный однострочный фикс, закрывающий находку по существу
независимо от причины её появления.

---

### 10. [Low] CHANGELOG.md утверждает «hard dep on pyutilz>=1.1», реальный пин в pyproject.toml — `>=1.0`

**File:Line**: `CHANGELOG.md:20-22`, `pyproject.toml:28-37`.

**Description**: `CHANGELOG.md`'s секция «Notes» (строки 19-22) буквально гласит: *«Hard dep on
`pyutilz>=1.1` for LLMProvider, OpenRouterProvider, `list_openrouter_models()` (...), unified
`reasoning` field, `supports_json_mode()`, Phase-4 OR extras.»* Но фактический объявленный минимум
в `[project.dependencies]` — `"pyutilz>=1.0"` (`pyproject.toml:37`), с развёрнутым inline-комментарием
прямо над ним (строки 28-36), явно объясняющим, что это осознанное временное ослабление: *«Pinned
>= 1.0; in practice we rely on the post-1.0.0 commits adding Phase-4 OR fields (...). Tighten to
>=1.1 once those land in a tagged pyutilz release.»*

**Why it matters**: это не обязательно баг (`pyproject.toml`'s комментарий сам объясняет
расхождение как временное и осознанное) — но как факт, зафиксированный в CHANGELOG, формулировка
«Hard dep on pyutilz>=1.1» на сегодняшний день неточна: реально enforced нижняя граница — `>=1.0`.
Похоже, CHANGELOG-строка была написана в расчёте на целевое состояние и не была пересмотрена, когда
было принято решение временно ослабить пин. Возможная альтернативная трактовка: CHANGELOG описывает
*функциональную* потребность («нужны фичи уровня 1.1»), а pyproject.toml — текущий безопасный
*временный* флор; в этом смысле два файла не противоречат друг другу по существу, а просто говорят
о разных вещах (need vs. pin). Тем не менее дословно они утверждают разные версии как «hard dep».

**Recommendation**: либо привести формулировку CHANGELOG к `pyutilz>=1.0 (interim; functionally
needs >=1.1-era commits, see pyproject.toml comment)`, либо, когда `pyutilz` действительно
затегает нужный релиз, синхронно поднять оба места одним PR.

---

### 11. [Info] Четыре пустых подпакета реально попадают в собранный wheel

**File:Line**: `pyproject.toml:72-74` (`[tool.setuptools.packages.find]`, `include = ["llm_bench*"]`),
`src/llm_bench/{cli,provider,discovery,confirmation}/__init__.py` (каждый — однострочный докстринг),
`src/llm_bench.egg-info/SOURCES.txt` (строки 11, 22, 32 подтверждают их присутствие в реально
собранных source-файлах пакета).

**Description**: `cli/`, `provider/`, `discovery/`, `confirmation/` — чистые заглушки без единой
функции/класса, но `include = ["llm_bench*"]` не делает для них исключения, и `SOURCES.txt`
подтверждает, что все четыре `__init__.py` реально включаются в собранный дистрибутив.

**Why it matters**: с точки зрения packaging-гигиены это не баг сам по себе — вполне разумная и
частая практика для alpha-пакета резервировать namespace под будущие подсистемы (roadmap
placeholder), задокументированная самим README как «alpha, API is stabilizing». Три из четырёх
(`provider/`, `discovery/`, `confirmation/`) полностью инертны — занимают несколько байт в wheel'е,
без функционального риска. Единственное исключение — `cli/`, которое не инертно: оно активно
подключено как `[project.scripts]` entry point и реально ломается при вызове (см. находку №1) —
то есть этот конкретный стаб не «просто занимает место», а активно вводит пользователя в
заблуждение о готовности CLI.

**Recommendation**: отдельного действия не требуется для `provider/discovery/confirmation` (разумный
roadmap placeholder). Для `cli/` — см. рекомендацию находки №1.

---

### 12. [Info] Тегов/GitHub Releases ещё не было — структура CHANGELOG (`[Unreleased]`-only) сама по себе корректна

**File:Line**: `git tag -l` (пустой вывод), `pyproject.toml:7` (`version = "0.1.0"`).

**Description**: В репозитории нет ни одного git-тега. Значит, официального релиза `0.1.0` на PyPI
ещё не было (что согласуется с примечанием в `release.yml:9-11`: *«a successful upload requires the
runtime dependency `pyutilz` to be available on PyPI; until then this workflow builds and validates
but the upload step is the gating action to run only once that is true»*). Это делает то, что
`CHANGELOG.md` держит весь свой контент под единственной секцией `[Unreleased]` без секции
`[0.1.0]`, структурно корректным — здесь нет расхождения с версией/тегами как таковыми.
Единственная реальная проблема с содержимым CHANGELOG — его неполнота относительно фактически
реализованного функционала (см. находку №6), а не отсутствие версионной секции.

**Recommendation**: нет действия, кроме учёта при чтении находки №6 — не путать «нет секции
[0.1.0]» (это ожидаемо/корректно) с «секция [Unreleased] неполна» (реальная проблема).

## Итог по проверенным claim'ам из задания

- **`pip install llm-bench[postgres]` / core-only / dev-install команды** — синтаксически
  корректны относительно `[project.optional-dependencies]` (`postgres`, `dev` секции существуют,
  имена совпадают).
- **Версионная согласованность** `__version__` (`src/llm_bench/__init__.py:8`) ↔ `pyproject.toml`
  `version` (строка 7) — обе `"0.1.0"`, совпадают; `tests/test_meta/test_version_consistency.py`
  реально ловит дрейф (проверил логику regex-экстракции — она корректно ограничивается секцией
  `[project]`, не путает с версиями инструментов в `[tool.*]`). Никакого суффикса `-alpha` в самой
  версионной строке нигде нет ни в одном из файлов — «alpha» упоминается только как прозаический
  Development Status / текст в README, что само по себе не расхождение.
- **Matrix CI Python-версий** (`ci.yml:27`: `["3.11", "3.12", "3.13", "3.14"]`) действительно
  совпадает 1:1 с classifiers в `pyproject.toml:20-23`.
- **mypy/interrogate/deptry/bandit(-ll)** — перепроверил напрямую: все проходят чисто, совпадает с
  заявлениями в комментариях репозитория (0 mypy issues; interrogate 68.5% ≥ 65% порог; deptry 0
  issues; bandit 0 findings на medium+ уровне, только не относящиеся к SQL Low-находки). Не
  дублирую как отдельные находки, т.к. это именно то, что CI/pre-commit конфигурация уже
  корректно покрывает.

## Number of findings by severity

High: 3 · Medium: 4 · Low: 3 · Info: 2
