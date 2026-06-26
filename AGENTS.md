# AGENTS.md

Guidance for OpenCode sessions working in this repository.

## Project at a glance

- **What it is:** Python CLI that scrapes exam questions from `examtopics.com` and renders an interactive single-file HTML study page (radio answers, Peek, grading, dark/light theme, comments).
- **Package:** single package `examtopics/`. Every line of logic lives in `src/examtopics/__init__.py` (922 lines) plus a static `src/examtopics/exam.html` template (894 lines) loaded as package data.
- **Console entry point:** `examtopics` -> `examtopics:main` (declared in `pyproject.toml`).
- **Dist vs. import name:** the distribution is `examtopics-downloader` but the importable package and console script are both `examtopics`. They are decoupled via `[tool.uv.build-backend] module-name = "examtopics"` in `pyproject.toml`.
- **Python:** 3.11+ (pinned via `.python-version`). Runtime deps: `requests>=2.32`, `beautifulsoup4>=4.12`.
- **Build backend:** `uv_build` with the `src/` layout.

## Build, run, install

```bash
uv sync                          # create .venv/, install deps (editable)
uv run examtopics --help         # run the CLI (console script form)
uv tool install .                # global install -> `examtopics` on PATH (~/.local/bin)
```

There is **no test suite, linter, formatter, or type-checker configured** in this repo. No `pytest`, `ruff`, `ty`, `tox`, `pre-commit`, no `.github/workflows/`. Do not invent one without asking. A reasonable manual smoke test:

```bash
uv run examtopics -p amazon -s SAA-C03 -n 1 -o smoke
```

(uses `-n 1` to keep it fast; opens `smoke.html` to inspect output).

Note: `python -m examtopics` does not work (no `__main__.py`). Use `uv run examtopics` instead. The `__init__.py` does have a `if __name__ == "__main__": main()` guard at the bottom, so it can be executed directly as a script too.

## Key files

- `src/examtopics/__init__.py` -- scraping, CLI, HTML generation. The only Python file.
- `src/examtopics/exam.html` -- interactive template, shipped as package data (auto-included by `uv_build`).
- `pyproject.toml` -- `uv_build` backend, `examtopics` console script, dist/import-name decoupling. No tool config.
- `uv.lock` -- committed; pins runtime deps for reproducible installs.
- `.python-version` -- pins Python 3.11 for `uv`.
- `README.md` -- user-facing docs and CLI examples; keep in sync if CLI flags change.
- `.gitignore` -- standard Python ignores plus `.venv/`, build artifacts, pytest/mypy caches (none are used today, but listed preemptively).

## Code conventions worth knowing

- All hardcoded scraping/CLI tunables are module-level constants in `examtopics/__init__.py:15-21` -- `BASE_URL`, `HTTP_TIMEOUT`, `MAX_CONCURRENT`, `REQUESTS_PER_SEC`, `MAX_RETRIES`, `INITIAL_BACKOFF`, `BACKOFF_FACTOR`. Adjust scraping behavior there; don't duplicate magic numbers.
- The HTML template (`exam.html`) is mutated via **exact string find/replace**. The substitutions in `generate_html` (`__init__.py:459`) and `_replace_js_data` (`__init__.py:603`) rely on these literal substrings still being in the template:
  - `<title>AWS EXAM</title>` and `>AWS EXAM<` (display name; replaced at `__init__.py:466-467`)
  - `<!-- Question 1 -->` and `<!-- SUBMIT -->` (card insertion window; `__init__.py:471-472`)
  - `var totalQuestions = 3;` (count; `__init__.py:628`)
  - `var correctAnswers = { '1': 'b', '2': 'c', '3': 'b' };` plus the matching `gradedQuestions` and `answeredQuestions` lines (`__init__.py:630-639`)
  - `var commentsData = {` ... `  };` (comments payload; `__init__.py:642-647`)
  If you rewrite the template, keep these placeholders or update the call sites in `__init__.py` to match.
- `exam.html` is loaded via `importlib.resources.files("examtopics")` with a filesystem fallback (`_get_template_path`, `__init__.py:24-30`). Don't break that fallback -- running from a source checkout depends on it.
- `write_output` (`__init__.py:661`) always writes `<base>.html`. `<base>.md` is written as an intermediate and deleted unless `-t md` is passed. `-t txt` adds `<base>.txt` and removes the `.md`. Output lands in the **current working directory** unless `-o` sets a different base name.
- The scraper uses real HTTPS calls; there is no mocking layer. CAPTCHA'd pages (`Enter Captcha` text or `.g-recaptcha` button) are silently skipped via `detect_captcha` (`__init__.py:78`).
- URL type auto-detection is in `parse_url` (`__init__.py:86`). Four URL types: `discussion_single`, `discussion_list`, `exam_view`, `exam_list`, plus the no-URL `-p/-s` mode in `main()`. If you add a mode, keep all branches in `main()` (`__init__.py:765-918`) consistent.
- Answer extraction tries four strategies in order (`get_answer_from_soup`, `__init__.py:240`): explicit `.correct-answer` box, `correct-hidden` choice, `voted-answers-tally` JSON, then comment vote consensus. Don't reorder without a reason -- the fallbacks exist because ExamTopics hides the answer for free users.

## Things agents commonly get wrong here

- Searching for a test framework. There isn't one. Don't run `pytest` or `ruff`; they aren't installed and aren't configured.
- Looking for separate modules. The whole CLI is one file. Resist splitting it up unless you're also adding tests to justify the split.
- Editing `exam.html` without realizing the Python code does literal string substitution on it. Visual tweaks inside the placeholder regions are fine; renaming `AWS EXAM` or removing `<!-- SUBMIT -->` is not.
- Adding new CLI flags without updating `README.md`. The README's options table is the user-facing contract.
- Assuming the project uses a virtual environment. It doesn't commit one. Use `uv sync` to create `.venv/`, or `uv tool install .` for a global install. `.venv/` is gitignored.
- Forgetting that scraping hits the live site. Network failures, 503s, and CAPTCHAs are expected -- `fetch_url` (`__init__.py:36`) already retries 3x with exponential backoff.
- Running `python -m examtopics`. There is no `__main__.py`; only `uv run examtopics` (or the `examtopics` console script) works.

## Repo-specific workflow notes

- The default output file is `examtopics_output.html` in CWD (computed at `__init__.py:820`). When iterating locally, prefer `-o smoke` to keep artifacts tidy.
- `-n <N>` caps how many discussion listing pages are scanned. Use it whenever you don't need the full exam corpus.
- Rate limiting is a `Semaphore` + sliding window (`get_discussion_links` `__init__.py:186`, `scrape_questions_concurrently` `__init__.py:399`). Don't bypass it for "just one test" -- ExamTopics will 503 you.
- Image URLs from ExamTopics are sometimes protocol-relative (`//cdn...`); `scrape_question` normalizes them to `https://`. Preserve that logic if you refactor.
