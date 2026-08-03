# Аудит безопасности — llm_bench

## Scope & method (что прочитано, что запущено)

Прочитаны полностью (не выдержками) все файлы `src/llm_bench/**/*.py` (core, cost, halving,
pool, ranking, runner, stage, storage — все 4 backend-а), `examples/job_app_cover_letter/*.py`,
`pyproject.toml`, `.pre-commit-config.yaml`, `.env.example`, `.gitignore`, `.secrets.baseline`
(только заголовок/список детекторов), `README.md`, `CHANGELOG.md`, все четыре
`.github/workflows/*.yml` (ci.yml, release.yml, mypy-full.yml, black-filtered.yml),
`tests/conftest.py`, `tests/unit/test_storage_protocol.py`. Stub-пакеты `cli/`, `provider/`,
`discovery/`, `confirmation/` проверены — подтверждено, что это только однострочные docstring
`__init__.py` без реализации; `src/llm_bench/cli/main.py` действительно отсутствует (не мой
раздел — не репортится как security-finding, но использовано как контекст).

Запущенные read-only команды:
- `grep`/`find` по `src/`, `examples/`, `tests/`, `.github/` на предмет `os.environ`/`os.getenv`,
  `open(`/`Path(`, `pickle`/`eval`/`exec`/`yaml.load`/`subprocess`/`os.system`/`shell=True`,
  `logger.`/`print(`, `verify=False`/`ssl.`/`CERT_NONE`, `getattr`/`setattr`/`globals`/`compile`.
- `python -c "import llm_bench; ..."` — подтверждение, что пакет доступен в окружении (editable
  install), без изменения репозитория.
- Два **исполняемых PoC-скрипта** (только во временном scratchpad-каталоге, вне репозитория,
  никаких файлов внутри `llm_bench` не создавалось/не удалялось), доказывающих path traversal в
  `FileStorage` — см. Finding 1. Оба скрипта и их вывод приведены в разделе Findings.
- Не запускались `pytest`, `ruff`, `mypy`, `bandit` заново — эти гейты уже описаны в задаче как
  wired-in CI/pre-commit; вместо повторного прогона я вручную перепроверил конфигурацию гейтов
  (`.pre-commit-config.yaml`, `ci.yml`) на предмет того, действительно ли они блокирующие.

Не запускалось: ничего с `pytest -m live` (paid API calls), никаких git-команд, никаких
изменений/удалений файлов внутри репозитория.

## Summary table

| Severity | File:Line | Summary |
|---|---|---|
| Critical | `src/llm_bench/storage/file.py:134-136,239,303,326,350-353` | `experiment_tag` без какой-либо валидации подставляется в `Path(root) / experiment_tag` → path traversal (`../`) и полный обход `root` абсолютным путём; PoC подтверждает запись файла и **рекурсивное удаление каталога** (`shutil.rmtree`) вне `root` |
| High | `src/llm_bench/storage/postgres.py:39-52,155-186` и все `# nosec B608` сайты | `schema_name` конструктора f-string-подставляется без экранирования/allowlist ни в одном месте кода; DDL в `initialize()` выполняется через `conn.execute(stmt)` без параметров (simple query protocol asyncpg — поддерживает multi-statement stacking через `;`), т.е. класс структурно не может гарантировать заявленное в docstring "operator-supplied, not per-request" |
| Medium | `.github/workflows/ci.yml:112-127` | Ruff/black/bandit-шаги job `lint` помечены `continue-on-error: true` — реальный блокирующий security-гейт существует только в локальном pre-commit (опционален, обходится `--no-verify`), а не в CI, которая гейтит PR |
| Medium | `tests/unit/test_storage_protocol.py:33-40`, `.env.example:23-25` | SQL-код `PostgresStorage` (именно тот, что описан в задаче как требующий independent scrutiny) не покрыт вообще никаким тестом в репозитории — referenced файлы `test_storage_protocol_postgres.py` и `tests/integration/test_smoke_postgres.py` не существуют |
| Medium | `pyproject.toml:62`; `.github/workflows/ci.yml:72,157,194`; `.github/workflows/mypy-full.yml:33` | `py-ci-shared` и `pyutilz` подтягиваются через `git+https://...` / `git clone` без пина на commit/tag — каждый `pip install -e ".[dev]"` и каждый CI-прогон получает произвольный HEAD default-ветки без hash-проверки |
| Low | `src/llm_bench/runner/round_runner.py:229-230` | `error_message = str(e)[:500]` от произвольного исключения (включая исключения из внешних, не аудированных здесь библиотек httpx/asyncpg/pyutilz) персистится без редактирования в JSONL/Postgres и уходит в `logger.exception` — потенциальный канал утечки секрета, если внешняя библиотека когда-либо кладёт credential в текст исключения |
| Low | `src/llm_bench/storage/postgres.py:179-183` | `asyncpg.create_pool(dsn=self._url, ...)` вызывается без try/except внутри `initialize()` — при ошибке парсинга DSN сообщение исключения (потенциально содержащее пароль) не перехватывается и не редактируется этим кодом, будущее логирование на call-site целиком на совести внешнего кода |
| Low | (архитектурное) `storage/base.py`, все три backend-а | Ни один backend не предлагает опцию at-rest шифрования; результаты (полный текст промптов/ответов LLM) пишутся в plaintext JSONL/Postgres — нормально для benchmarking-фреймворка, но стоит зафиксировать, т.к. VocabApp/JobApp consumers прогоняют потенциально чувствительные тексты (job descriptions, PII) |
| Info | — | Секреты (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LLM_BENCH_TEST_DB_URL`) читаются только через `os.environ.get(...)` для presence-check, никогда не логируются и не попадают в пути к файлам; `.env` в `.gitignore` (строки 105-108), `.env.example` — единственный закоммиченный шаблон |
| Info | — | `eval`/`exec`/`pickle`/`yaml.load`(unsafe)/`subprocess`/`os.system`/`shell=True` — ни одного вхождения во всём `src/` и `examples/` (grep-подтверждено) |
| Info | `src/llm_bench/runner/round_runner.py:329-343`, `examples/job_app_cover_letter/stages.py:109-129` | LLM-output обрабатывается как untrusted: framework-уровневый `_parse_safely` оборачивает вызов parser-callback в try/except, consumer-парсер (`_parse_json_loose`) делает только `json.loads` (никакого `eval`) с fallback-экстракцией `{...}` и обрезает каждое поле фиксированной длиной |
| Info | `.github/workflows/*.yml` | zizmor-hardening заявления подтверждены на актуальном YAML: все actions SHA-pinned с версионным комментарием, `persist-credentials: false` на каждом checkout, `permissions: contents: read` на уровне workflow, `id-token: write` ограничен только job `publish` в release.yml; `pull_request_target` нигде не используется |

## Findings

### Finding 1 — Critical: path traversal / absolute-path escape через `experiment_tag` в FileStorage (arbitrary write + arbitrary recursive delete)

**Severity:** Critical
**File:Line:** `src/llm_bench/storage/file.py:134-136` (`record_call`), `:239` (`query_rows`),
`:303` (`persist_winners`), `:326` (`load_winners`), `:350-353` (`delete_experiment`);
корневая причина — `src/llm_bench/core/types.py:19` (`ExperimentTag = NewType("ExperimentTag", str)`,
runtime-нулевая проверка) и публичный API `src/llm_bench/runner/benchmark.py:209,237`
(`run_phase(*, tag: str | ExperimentTag, ...)` → `exp_tag = ExperimentTag(str(tag))`, без валидации).

**Описание.** Каждый метод `FileStorage`, которому нужен каталог эксперимента, строит путь как
`self.root / experiment_tag` (или `self.root / row.experiment_tag`) — без единой проверки, что
`experiment_tag` не содержит `..`, разделители пути или сам не является абсолютным путём. Это
касается ЛЮБОГО потребителя пакета, вызывающего публичный API `Benchmark.run_phase(tag=...)` /
`BenchmarkStorage.record_call(row)` / `delete_experiment(experiment_tag=...)` — `tag`/`experiment_tag`
всюду типизирован как обычный `str` (через `NewType`, который на рантайме не даёт вообще никакой
проверки — это просто алиас для документации типов).

Я подтвердил это исполняемым PoC (см. Scope) в изолированном scratchpad, вне репозитория:

1) **Arbitrary write.** `FileStorage(root=<tmp>/poc_root).record_call(RunRow(..., experiment_tag="../poc_ESCAPED_dir", ...))` создал
   `results.jsonl` **вне** `poc_root` — по пути `<tmp>/poc_ESCAPED_dir/results.jsonl`. Вывод:
   ```
   root exists: True
   escape dir exists (OUTSIDE root): True
     wrote: ...\poc_ESCAPED_dir\results.jsonl
   ```

2) **Arbitrary recursive delete.** `FileStorage(root=<tmp>/poc_root2).delete_experiment(experiment_tag="../poc_VICTIM_dir_outside_root")`
   вызвал `shutil.rmtree` (storage/file.py:352-353) над каталогом, заранее созданным СНАРУЖИ `root`
   и содержащим файл `important_unrelated_file.txt`. Вывод:
   ```
   victim exists BEFORE delete_experiment: True
   delete_experiment returned: 0
   victim exists AFTER delete_experiment: False
   ```
   Каталог и его содержимое удалены безвозвратно, без единого предупреждения — `storage/base.py:161-163`
   документирует `delete_experiment` как "DESTRUCTIVE — callers MUST gate this behind explicit user
   authorization", но это относится только к решению "удалять ли этот tag вообще"; сам факт, что тег
   может указать НА СОВЕРШЕННО ДРУГОЙ каталог, нигде не рассматривается и ничем не блокируется.

3) **Абсолютный путь полностью отбрасывает `root`** (стандартное поведение `pathlib`, проверено
   read-only, без записи файлов): `Path(root) / "C:/Users/.../absolute_escape_demo"` возвращает
   ИСКЛЮЧИТЕЛЬНО `C:/Users/.../absolute_escape_demo`, полностью игнорируя `root`. Т.е. `experiment_tag`
   не обязателен даже вид `../..` — достаточно, чтобы он оказался абсолютным путём (Windows-диск
   `C:\...` или POSIX `/etc/...`), и ВСЕ файловые операции `FileStorage` (запись результатов,
   `winners/round_N.json`, а при вызове `delete_experiment` — рекурсивное удаление) уйдут по этому
   абсолютному пути целиком, а не внутрь `root`.

**Почему это важно / конкретный сценарий отказа.** `tag` — публичный, по контракту фреймворка
свободно формируемый потребителем параметр (docstring `core/types.py:20-22` лишь рекомендует
конвенцию `<consumer>_<purpose>_<unix_ts>`, ничего не проверяя). README (`README.md:56`) показывает
использование как `bench.run_phase(tag="exp_v1", ...)` без единой оговорки о доверии к строке.
Ни один из трёх заявленных потребителей репозитория (VocabApp, JobApp POC) сегодня не строит tag
из недоверенного пользовательского ввода — но ничто в самом фреймворке этого не гарантирует и не
проверяет: пример из `examples/job_app_cover_letter/run.py:166` (`tag = f"job_app_cl_{int(time.time())}"`)
безопасен только потому, что автор конкретного примера не подставил туда, например, job title или
company name из CSV. Любое будущее/стороннее приложение (в том числе описанный в задаче VocabApp,
который явно документирован как переопределяющий поведение `PostgresStorage` под себя — т.е. это
не гипотетический третий потребитель, а реальный) может легко собрать tag из данных, частично
управляемых пользователем/внешним источником (например, включить job-ID, email, company slug), и
тем самым получить произвольную запись файлов или — что гораздо хуже — произвольное рекурсивное
удаление каталога где угодно в файловой системе, на которую хватает прав у процесса.

**Рекомендация.** В `FileStorage.__init__` (или единой internal-helper, используемой ВСЕМИ методами,
работающими с `experiment_tag`) добавить: (1) allowlist-регэксп на `experiment_tag`, например
`^[A-Za-z0-9_.-]{1,200}$` (запрещает `/`, `\`, `..`, абсолютные префиксы, null-байты); (2) явную
проверку через `resolved = (self.root / experiment_tag).resolve(); assert resolved.is_relative_to(self.root.resolve())`
как defense-in-depth даже после regex; (3) то же самое для `round_idx`-based имён файлов (сейчас
безопасно, т.к. `round_idx: int`, но стоит явно закрепить тип, чтобы будущий рефакторинг не поменял
его на произвольную строку). То же самое имеет смысл сделать в `PostgresStorage` для консистентности
(`experiment_tag` там уже безопасно параметризован через `$n`, но стоит один раз провалидировать
формат на входе во всём фреймворке, а не полагаться на то, что один backend случайно безопасен).

---

### Finding 2 — High: `schema_name` в PostgresStorage — неэкранированная интерполяция идентификатора без какой-либо валидации в коде

**Severity:** High
**File:Line:** `src/llm_bench/storage/postgres.py:39-52` (`_ddl`), `:155-186` (`__init__`/`initialize`),
и далее каждый f-string SQL-сайт: `:203,209,215,230,266,295,324,342,356,392,405,431,437` (все с
`# nosec B608`).

**Описание.** Я прочитал файл `postgres.py` целиком (509 строк) и независимо проверил оба тезиса
из задания:

1. **Значения параметризованы везде без исключений.** Я не нашёл ни одного места, где реальное
   ЗНАЧЕНИЕ (experiment_tag, hash, response text, cost, JSON payload и т.д.) подставлялось бы в
   f-string вместо `$n`-плейсхолдера asyncpg — все 13 `# nosec B608`-сайтов используют `$1..$37`
   корректно. Это подтверждает half этого тезиса задания.

2. **`self._schema` — нет.** Он подставляется как raw f-string во ВСЕ SQL: не только в `SELECT/
   INSERT/UPDATE/DELETE` (что и обсуждает docstring класса на строках 146-152), но и в
   `_ddl()` (строки 39-120) — включая `f"CREATE SCHEMA IF NOT EXISTS {s};"` (строка 43) и все
   `CREATE TABLE`/`CREATE INDEX`. Класс-докстринг (строки 146-152) заявляет, что это безопасно,
   т.к. `self._schema` "set once at construction time from an operator-supplied config value, not
   per-request user input" — но это **утверждение никак не гарантировано кодом**: `__init__`
   (строка 155-166) принимает `schema_name: str = "llm_bench"` без единой проверки формата
   (нет regex/allowlist, нет `asyncpg`-style quote_ident, нет даже банальной проверки
   `str.isidentifier()`). Docstring сам же признаёт на строках 3-4 и 143-144, что "consumers can
   override via `schema_name`" и что "VocabApp overrides to `llm`" — т.е. переопределение
   schema_name внешним потребителем это не гипотетика, а документированный, уже происходящий в
   реальности сценарий.

   Я проверил `grep -rn "PostgresStorage(" .` по всему репозиторию — **ноль вхождений**: класс
   нигде не инстанциируется ни в тестах, ни в примерах этого репозитория. Это означает, что
   единственная "гарантия", что `schema_name` не станет менее доверенным значением
   (например, `os.environ["TENANT_SCHEMA"]` в мультитенантном деплое, что прямо напрашивается,
   учитывая, что framework уже документирует per-consumer override) — это дисциплина внешнего,
   неаудированного здесь кода (VocabApp), а не что-либо в самом llm_bench.

   Дополнительно, конкретно `initialize()` (строка 175-186) выполняет DDL-операторы через
   `await conn.execute(stmt)` **без параметров** (цикл `for stmt in _ddl(self._schema)`). По
   задокументированному поведению asyncpg, `Connection.execute()` без переданных `*args`
   использует simple query protocol, который (в отличие от extended/prepared-statement протокола,
   применяемого ко всем ОСТАЛЬНЫМ параметризованным вызовам в этом файле) поддерживает несколько
   `;`-разделённых SQL-команд в одной строке. Т.е. именно путь `initialize()` — не value-based
   инъекция, а полноценная классическая multi-statement SQL-инъекция, если когда-либо
   `schema_name` окажется менее доверенным значением: строка вида
   `"llm_bench; DROP SCHEMA public CASCADE; --"`, попав в `f"CREATE SCHEMA IF NOT EXISTS {s};"`,
   выполнится как две отдельные команды в рамках simple query protocol.

**Почему это важно.** Это ровно тот паттерн, который bandit's B608-эвристика существует, чтобы
ловить, и который здесь глушится через `# nosec` на основании довода, которого сам класс не
проверяет и не может проверить, поскольку принимает произвольный `str`. Сегодня, в этом
конкретном репозитории, эксплуатации нет (нет вызовов), но это latent design-level уязвимость:
любой будущий or внешний вызывающий код, который свяжет `schema_name` с чем-то менее статичным,
чем литеральная константа в исходниках (переменная окружения, конфиг из БД, per-tenant роутинг),
немедленно получает SQL-инъекцию без единого предупреждения со стороны llm_bench.

**Альтернативное прочтение (в порядке объективности).** Если у автора репозитория есть жёсткая
организационная гарантия, что `schema_name` ВСЕГДА и ТОЛЬКО задаётся как Python source-level
литерал в конфиге деплоя (как это, по всей видимости, сегодня и есть — VocabApp якобы жёстко
прописывает `"llm"`), риск практически нулевой при текущей дисциплине. Но это дисциплина
эксплуатации, а не гарантия кода — ни тестов, ни runtime-проверки, которые заставили бы нарушение
этой дисциплины упасть с понятной ошибкой вместо тихой SQL-инъекции, не существует (см. Finding 4).

**Рекомендация.** Добавить в `PostgresStorage.__init__` жёсткую валидацию: `if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", schema_name): raise ValueError(...)` (Postgres-совместимый identifier, ограничение длины 63 байта). Это на порядок дешевле, чем текущая
чисто документационная защита, и превращает "теоретически опасный `str`" в честно провалидированный
идентификатор — тогда `# nosec B608` перестаёт быть голословным и становится действительно
верифицируемым.

---

### Finding 3 — Medium: security-гейты в GitHub Actions CI — advisory-only (`continue-on-error: true`), реально блокирует только необязательный local pre-commit

**Severity:** Medium
**File:Line:** `.github/workflows/ci.yml:112-114` (ruff), `:119-123` (black-filtered), `:125-127`
(bandit).

**Описание.** В job `lint` (`ci.yml:94-134`) все три шага явно помечены `continue-on-error: true`:
```
112:      - name: Run ruff
113:        run: uvx ruff check src/
114:        continue-on-error: true
...
125:      - name: Run bandit security scan
126:        run: uvx bandit -r src/ -ll -f json -o bandit-report.json
127:        continue-on-error: true
```
Это значит, что провал любого из них НЕ делает CI-run красным. Реально блокирующие версии этих
же проверок существуют только в `.pre-commit-config.yaml` (`ruff-real-bugs` строки 41-54,
`bandit-blocking` строки 125-133) — но pre-commit хуки запускаются локально, требуют
`pre-commit install` (не выполняется автоматически при клонировании) и явно документированы как
обходимые: заголовок файла (`.pre-commit-config.yaml:6-7`) прямо говорит "To skip in a hurry: git
commit --no-verify". Ни в одном workflow нет шага, который перезапускал бы pre-commit хуки в CI
(`pre-commit run --all-files` или аналог) как реально блокирующую проверку для внешних PR.

**Почему это важно.** Для контрибьютора, который либо не установил pre-commit локально, либо
использовал `--no-verify`, либо просто открыл PR из форка (где GitHub Actions по умолчанию
работает с чужого чекаута, а не с локальным git hook-состоянием автора), единственный реально
исполняющийся security-гейт в этом сценарии — bandit — не заблокирует merge при новом реальном
findings (hardcoded credential, `eval`, новая небезопасная f-string SQL-конструкция и т.п.).
Комментарий в `.pre-commit-config.yaml:120-124` заявляет "Blocking — closed to 0 findings" — это
верно только для локального commit-time гейта, а не для того, что фактически защищает ветку
`main`/`master` на GitHub.

**Альтернативное прочтение.** Возможно, ветка защищена required-status-checks на уровне GitHub
branch protection settings, которые я не могу проверить из локального чекаута (репозиторий не
инициализирован как git-репозиторий в этом окружении — `.git` отсутствует, свериться с реальными
branch protection rules через `gh` невозможно без сетевого доступа/токена). Если branch protection
требует прохождения `meta` job (который реально блокирующий, т.к. это обычный `pytest`-запуск без
`continue-on-error`) — это частично закрывает hygiene-гейты, но НЕ закрывает bandit/ruff/black,
у которых нет отдельного non-continue-on-error эквивалента нигде в `.github/workflows/`.

**Рекомендация.** Либо снять `continue-on-error: true` с шага bandit (минимум — security-скан
обязан блокировать merge), либо, если сохранение advisory-режима в CI осознанно (например, чтобы
не дублировать pre-commit), явно задокументировать в `README`/`CONTRIBUTING`, что ветка защищена
required pre-commit.ci-подобным гейтом, и подтвердить это в самих branch protection settings, а не
только в комментарии внутри `.pre-commit-config.yaml`.

---

### Finding 4 — Medium: SQL-код `PostgresStorage` не покрыт вообще никаким тестом в репозитории

**Severity:** Medium
**File:Line:** `tests/unit/test_storage_protocol.py:33-40` (fixture `storage`, параметризован
только `"memory"` и `"file"`, с комментарием "see test_storage_protocol_postgres.py (Phase D
follow-up)"); `.env.example:23-25` (ссылается на
`tests/integration/test_smoke_postgres.py`); `.github/workflows/ci.yml:29-47` (Postgres service
контейнер поднимается в job `test` специально под этот файл).

**Описание.** Я проверил: ни `tests/test_storage_protocol_postgres.py`, ни
`tests/integration/test_smoke_postgres.py` не существуют нигде в дереве репозитория
(`find tests -iname "*postgres*"` — пусто; `grep -rln "PostgresStorage" tests/` находит только
упоминание в докстринге/комментарии `test_storage_protocol.py`, не реальный импорт/вызов класса).
CI поднимает `postgres:16` service-контейнер (`ci.yml:29-47`) и экспортирует
`LLM_BENCH_TEST_DB_URL` (`ci.yml:47`) — то есть инфраструктура для теста существует и оплачивается
(время CI), но ни один тест её не использует.

**Почему это важно.** Именно SQL-конструкция `postgres.py` (Finding 2) — та часть кода, которую
задание прямо просит проверить "independently" — не проверяется вообще НИЧЕМ автоматическим:
ни unit-тестом с mock-asyncpg, ни integration-тестом с реальным Postgres. Заявление в
`.pre-commit-config.yaml:120-124` "closed to 0 findings ... every value is passed as an asyncpg
$n placeholder" опирается исключительно на ручной code review в момент написания — нет
регрессионного теста, который поймал бы, если будущий контрибьютор случайно скопипастит один из
13 SQL-сайтов и забудет параметризовать новое поле (человеческая ошибка, для которой обычно и
нужен contract-тест).

**Рекомендация.** Либо написать `tests/integration/test_smoke_postgres.py` (gated по
`LLM_BENCH_TEST_DB_URL`, как и планировалось) и параметризовать `test_storage_protocol.py`'s
`storage` fixture третьим вариантом `"postgres"`, либо, как минимум, добавить дешёвый offline unit-тест
с `unittest.mock.AsyncMock` на месте `asyncpg.Pool`, который бы утверждал, что ни один
`conn.execute(...)`/`conn.fetch(...)` вызов не получает значение через f-string (например, снэпшот-тест
самого SQL-текста на предмет того, что единственная переменная часть — `{self._schema}`).

---

### Finding 5 — Medium: `pyutilz` и `py-ci-shared` подтягиваются из git без пина на commit/tag

**Severity:** Medium
**File:Line:** `pyproject.toml:62` (`"py-ci-shared @ git+https://github.com/fingoldo/py-ci-shared.git"`);
`.github/workflows/ci.yml:72` (job `test`), `:157` (job `meta`), `:194` (job `build`, smoke-install:
`pip install "pyutilz @ git+https://github.com/fingoldo/pyutilz.git"`);
`.github/workflows/mypy-full.yml:33` (`git clone --depth 1 https://github.com/fingoldo/pyutilz.git`).

**Описание.** `pyutilz` не опубликован на PyPI (подтверждено комментарием в `release.yml:9-11`:
"a successful upload requires the runtime dependency pyutilz to be available on PyPI; until then
..."), поэтому ЕДИНСТВЕННЫЙ способ получить его — `git clone`/`pip install git+...` без указания
`--branch`/`@<tag>`/`@<commit-sha>` — каждый вызов подтягивает HEAD текущей default-ветки на момент
выполнения. То же самое верно для dev-extra `py-ci-shared`. Это происходит в четырёх независимых
местах (`ci.yml` три job-а + `mypy-full.yml`), плюс любой сторонний разработчик, который делает
`pip install -e ".[dev,postgres]"` локально, получает тот же непинованный клон.

**Почему это важно.** Оба репозитория (`fingoldo/pyutilz`, `fingoldo/py-ci-shared`) принадлежат
тому же владельцу, что и `llm_bench` — сегодня это доверенная, low-likelihood поверхность атаки.
Но структурно это ровно тот gap, который задание просит явно проверить: компрометация
GitHub-аккаунта владельца (фишинг credentials, украденный PAT, скомпрометированный CI runner
одного из этих репозиториев, который делает `git push --force` на default-ветку) немедленно и
без какой-либо hash-проверки исполняется в: (а) любом свежем dev-инсталле `llm_bench`, (б) каждом
CI-прогоне `llm_bench` (test/meta/build/mypy-full jobs), причём установка идёт с правами всего
процесса (`uv pip install --system`, т.е. системный Python в CI-контейнере). `deptry` (уже wired
в CI) эту категорию рисков не проверяет вообще — он про unused/missing/transitive-deps hygiene,
не про supply-chain integrity непинованных git-источников.

**Рекомендация.** Зафиксировать оба git-source-зависимости на конкретный commit SHA или tag:
`pyutilz @ git+https://github.com/fingoldo/pyutilz.git@<sha-or-tag>` и аналогично для
`py-ci-shared`; в `ci.yml`/`mypy-full.yml` заменить `git clone --depth 1 <url>` на
`git clone --depth 1 --branch <tag> <url>` (или checkout конкретного SHA после clone). Обновлять
пин осознанно при каждом релизе `pyutilz`, а не автоматически подхватывать HEAD.

---

### Finding 6 — Low: непроверенное/неотредактированное исключение (`str(e)[:500]`) персистится в storage и в логи

**Severity:** Low
**File:Line:** `src/llm_bench/runner/round_runner.py:226-231` (`error_message = str(e)[:500]`
внутри `except Exception as e:` вокруг `_call_llm`), затем строка 265 (`error_message=error_message,`
в конструкторе `RunRow`, персистируется через `storage.record_call(row)` на строке 284 — то есть в
JSONL `storage/file.py:151-154` или Postgres `storage/postgres.py:225-256` в поле `error_message
TEXT`, plaintext, без редактирования).

**Описание.** Любое исключение из вызова провайдера (`provider.generate(...)`, реализация которого
находится в `pyutilz`, не в этом репозитории и потому не аудирована здесь) перехватывается broad
`except Exception as e`, и его `str(e)` (до 500 символов) сохраняется дословно в постоянное
хранилище и, отдельно, попадает в `logger.exception(...)` в других местах (`round_runner.py:117,
185, 342`). Ни здесь, ни в `classify.py` (`runner/classify.py`, прочитан целиком — только
классификация по ключевым словам в lowercase-сообщении, никакого редактирования) нет
скрабинга/маскирования потенциальных секретов внутри текста исключения.

**Почему это важно.** Если библиотека `pyutilz`/`httpx`/`asyncpg` когда-либо формирует текст
исключения, включающий чувствительные данные (классический пример — `ValueError` на
невалидный/усечённый DSN, который иногда echo-ит всю строку подключения с паролем; либо
HTTP-клиент, чей `repr` запроса включает заголовки), эта строка окажется навсегда записана в
JSONL-файл на диске и/или строку Postgres, доступную любому с read-доступом к storage — и в
лог-вывод процесса. Это НЕ подтверждённая живая утечка (я не могу проверить поведение
`pyutilz`, т.к. её нет в этом репозитории), а defense-in-depth gap: сам `llm_bench` ничего не
делает, чтобы такую утечку исключить, даже если она произойдёт ниже по стеку.

**Рекомендация.** Добавить простую regex-based редактирующую обёртку (маскировать
`Bearer\s+\S+`, `postgresql://[^:]+:[^@]+@`, `sk-[A-Za-z0-9_-]{10,}`-подобные паттерны) перед
записью `error_message` в `RunRow` и перед передачей в `logger.exception`. Дёшево сделать сейчас,
дорого добавлять постфактум, когда через полгода realистично найдётся утечка в исторических JSONL.

---

### Finding 7 — Low: `PostgresStorage.initialize()` не перехватывает исключение `asyncpg.create_pool`, потенциально несущее DSN

**Severity:** Low
**File:Line:** `src/llm_bench/storage/postgres.py:175-186`.

**Описание.** `self._pool = await asyncpg.create_pool(dsn=self._url, ...)` (строки 179-183)
выполняется без обёртки try/except. `self._url` (строка 163, из конструктора) — это полноценный
Postgres DSN, потенциально с embedded паролем (`postgresql://user:pass@host/db`, ровно формат из
`.env.example:25`). Если `create_pool` бросит исключение (неверный формат DSN, DNS-резолв не
удался, auth failure), это исключение всплывает НЕОБРАБОТАННЫМ из `initialize()` к вызывающему
коду. Сам `llm_bench` при этом нигде `self._url` не логирует (grep подтвердил — единственное
использование `self._url` во всём файле это строка 180 внутри `create_pool`), так что прямой
утечки из ЭТОГО репозитория нет.

**Почему это важно.** Ответственность за то, чтобы вызывающий код (VocabApp и другие потребители)
не залогировал пойманное исключение с `logger.error(f"init failed: {e}")` без редактирования,
целиком лежит вне этого репозитория. Это низкий по вероятности, но конкретный, легко воспроизводимый
на практике паттерн (`try: await bench.initialize() except Exception as e: logger.error(str(e))`
— типичный catch-all в прикладном коде).

**Рекомендация.** Обернуть `create_pool` в `initialize()` в try/except, который переформатирует
сообщение об ошибке без DSN (например, оставить только hostname/port/dbname, вырезав userinfo),
прежде чем перевыбрасывать — снимает нагрузку с каждого downstream-потребителя по отдельности.

---

### Finding 8 — Low: отсутствие опции шифрования результатов at-rest ни в одном backend

**Severity:** Low
**File:Line:** `src/llm_bench/storage/base.py` (весь Protocol), `storage/file.py`, `storage/postgres.py`,
`storage/memory.py`.

**Описание.** Все три backend-а пишут полный текст промптов и ответов LLM в открытом виде
(`benchmark_system_prompts.prompt_text`/`benchmark_user_prompts.prompt_text` в Postgres —
`postgres.py:45-57`; `_prompts.jsonl`/`results.jsonl` в FileStorage — `file.py:104,136`). Ни
Protocol (`storage/base.py`), ни один backend не предоставляет hook для шифрования на уровне
приложения (например, envelope encryption поля `response`/`prompt_text` перед записью).

**Почему это важно.** Не баг и не эксплуатируемая уязвимость сама по себе — но данный фреймворк по
дизайну прогоняет реальные прикладные данные потребителей через LLM (VocabApp: словарные
определения; JobApp: реальные job postings с company/client-идентифицирующей информацией,
видно в `examples/job_app_cover_letter/job_pool.py:31,54-59`). Для деплоймента, где такие данные
считаются чувствительными, "включить на уровне приложения" сегодня невозможно — надо городить это
поверх фреймворка вручную для каждого backend-а отдельно.

**Рекомендация.** Не блокер для alpha (v0.1.0), но стоит зафиксировать как осознанный design-note
в README (сейчас такой оговорки нет) — "результаты хранятся в открытом виде; шифрование at-rest
— ответственность деплоймента (Postgres TDE / encrypted volume для FileStorage)".

## Итог по категориям серьёзности

- Critical: 1
- High: 1
- Medium: 3
- Low: 3
- Info: 4
