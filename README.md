# ExamTopics Downloader

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![uv](https://img.shields.io/badge/uv-managed-purple.svg)](https://docs.astral.sh/uv/)

Scrape exam questions from [examtopics.com](https://www.examtopics.com) and generate an **interactive single-file HTML study page** with answer checking, per-page grading, cross-page search, dark/light theme, and integrated discussion comments.

Supports every provider hosted on ExamTopics (Amazon AWS, Microsoft, Google, Cisco, CompTIA, ISC2, and others).

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage](#usage)
  - [By Provider and Exam Code](#by-provider-and-exam-code)
  - [By URL](#by-url)
  - [Output Formats](#output-formats)
  - [Pagination](#pagination)
- [Command-line Options](#command-line-options)
- [Output Files](#output-files)
- [FAQ and Troubleshooting](#faq-and-troubleshooting)
- [Notes](#notes)
- [Acknowledgments](#acknowledgments)
- [License](#license)

## Features

- **Multiple input modes** -- paste a single question URL, a discussion listing, an exam view, an exam list, or search by `-p provider -s exam-code`
- **Interactive HTML output** -- radio-button answers, Peek (reveal answer), Submit (grade the current page), per-question progress, dark/light theme toggle persisted in `localStorage`
- **Pagination in groups of 100** -- sticky prev/next nav, page label, automatic hide on single-page exams
- **Cross-page search** -- typing in the search box jumps to the page of the first match
- **Per-page grading** -- Submit scores only the visible page, shows `X / PAGE_SIZE` (or remainder on the last page)
- **Discussion comments** -- optionally include user comments per question (`-c`)
- **Image lightbox** -- click an exhibit or answer image for the full-size view
- **Concurrent scraping** -- 15 workers, rate-limited at 2 requests/second, with retries and exponential backoff
- **Multiple output formats** -- HTML (default) plus optional `.md` or `.txt` sidecar

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) (Astral's Python package and project manager)

## Installation

Clone the repository and install the package in editable mode with all runtime dependencies:

```bash
git clone https://github.com/kacppy/examtopics-scraper.git
cd examtopics-downloader
uv sync
```

This creates a `.venv/` and exposes the `examtopics` console script inside it. Run the CLI with `uv run`:

```bash
uv run examtopics --help
```

### Global install

To make the `examtopics` command available anywhere on your `PATH` (without `uv run` or activating a virtual environment), install the tool globally:

```bash
uv tool install .
examtopics --help
```

`uv tool install` builds an isolated environment and exposes `examtopics` in `~/.local/bin/` (added to `PATH` automatically on most setups). To upgrade after pulling new changes, re-run `uv tool install --reinstall .`. To uninstall, run `uv tool uninstall examtopics-downloader`.

## Quick Start

```bash
uv run examtopics -p amazon -s SAA-C03
```

This downloads every SAA-C03 discussion question ExamTopics lists, then writes an interactive `SAA-C03.html` in the current working directory. Open it in any browser -- no server, no build step.

## Usage

### By Provider and Exam Code

The most common mode. Provider slug plus a search string (typically the exam code) is enough:

```bash
examtopics -p amazon -s SAA-C03
examtopics -p microsoft -s AZ-104 -n 3 -o my-az104
examtopics -p google -s "Professional Cloud Architect"
```

`-n` caps how many discussion listing pages are scanned. Use it when you only need a sample or want to avoid long downloads.

### By URL

The tool auto-detects four URL types:

| URL shape | Detected type | Behaviour |
|-----------|---------------|-----------|
| `/discussions/<provider>/view/<id>-...` | `discussion_single` | Scrapes one question |
| `/discussions/<provider>/` | `discussion_list` | All discussions for the provider |
| `/exams/<provider>/<exam>/view/<n>/` | `exam_view` | One exam view, expands to all questions via discussion links |
| `/exams/<provider>/<exam>/` | `exam_list` | One exam, expands to all questions via discussion links |

Examples:

```bash
examtopics https://www.examtopics.com/discussions/amazon/view/12345-example/
examtopics https://www.examtopics.com/discussions/amazon/
examtopics https://www.examtopics.com/exams/amazon/aws-certified-solutions-architect-associate-saa-c03/view/1/
examtopics https://www.examtopics.com/exams/amazon/aws-certified-solutions-architect-associate-saa-c03/
```

### Output Formats

The primary output is always an interactive HTML file. Use `-t` to keep a sidecar file in another format:

```bash
examtopics -p amazon -s SAA-C03 -t md      # also keep .md
examtopics -p amazon -s SAA-C03 -t txt     # also produce .txt
```

`.md` is an intermediate file used to build both HTML and TXT, so it is removed by default. See [Output Files](#output-files) for the exact file layout per format.

### Pagination

Long exams are split into pages of **100 questions**. This is a deliberate design choice and is not currently a CLI flag -- the constant `PAGE_SIZE` lives in `src/examtopics/__init__.py` if you need to change it.

What the page UI provides:

- **Sticky nav bar** with a previous-page button, a `Page X of Y` label, and a next-page button. Hidden automatically when the exam has only one page.
- **Submit grades only the current page.** The result modal reports the page score (`X / 100`, or `X / remainder` on the last page).
- **Search jumps across pages.** Typing a query that matches a question on another page takes you to the page of the first match. Clearing the search restores the current page's view.
- **Reset All** clears answer state and returns to page 1.

All question cards stay in the DOM; visibility is toggled via a `data-page` attribute. The page nav IDs (`#pageNav`, `#pagePrev`, `#pageNext`, `#pageLabel`) and JS functions (`showPage`, `getPageRange`, `hidePageNav`, `gradePage`) are wired in `src/examtopics/exam.html`.

## Command-line Options

| Flag | Description |
|------|-------------|
| `url` | (positional) ExamTopics page URL -- discussion, exam view, or exam list. Omit to use `-p` / `-s`. |
| `-p`, `--provider` | Provider slug, e.g. `amazon`, `microsoft`, `google`. Use with `-s`. |
| `-s`, `--search` | Filter discussions by substring, e.g. exam code `SAA-C03`. |
| `-n`, `--pages` | Max discussion listing pages to scan (default: all available). |
| `-o`, `--output` | Output file base name (default: exam slug from URL, or `examtopics_output`). |
| `-c`, `--comments` | Include user comments from each discussion thread (default: off). |
| `-t`, `--type` | Extra output format alongside HTML: `md` keeps markdown, `txt` produces plain text (default: `html`). |
| `-h`, `--help` | Show the help message and exit. |

## Output Files

Output lands in the **current working directory** (not the directory you ran the command from if you used `uv tool install`). The base name comes from `-o`, or from the URL slug, or `examtopics_output` as a final fallback.

| `-t` value | Files written |
|------------|---------------|
| `html` (default) | `<base>.html` |
| `md` | `<base>.html` and `<base>.md` |
| `txt` | `<base>.html` and `<base>.txt` |

The `.md` file is an intermediate used to build both `.html` and `.txt`; it is deleted unless `-t md` is passed.

## FAQ and Troubleshooting

**A page is skipped with no error.**
ExamTopics serves a CAPTCHA to suspicious clients. When the scraper detects the `Enter Captcha` text or a `.g-recaptcha` button, that question is silently dropped. There is no built-in bypass.

**I get HTTP 503 errors.**
You are being rate-limited. Defaults are 2 requests/second and 15 concurrent workers. Adjust `MAX_CONCURRENT` and `REQUESTS_PER_SEC` in `src/examtopics/__init__.py` if you need a different profile, but going faster will trip the limiter faster.

**Some questions have no "correct answer" letter.**
This is expected for non-subscribers. The tool falls back through four strategies (see [How It Works](#how-it-works)) and may still fail when none of them finds a consensus. The question is still rendered -- the Peek button will not reveal an answer in that case.

**`uv: command not found`.**
Install `uv` from [docs.astral.sh/uv](https://docs.astral.sh/uv/) and ensure `~/.local/bin` (or the install location) is on your `PATH`.

**`python -m examtopics` does not work.**
The package has no `__main__.py`. Use `uv run examtopics` or the globally installed `examtopics` console script.

**How do I change the page size from 100?**
Edit `PAGE_SIZE` in `src/examtopics/__init__.py`. The value is deliberately hardcoded; there is no CLI flag for it.

**The output file is not where I expected.**
Output lands in the current working directory, not the directory you invoked the command from. When using `uv tool install`, `cd` to your target folder first.

**Pagination nav is missing.**
The nav is hidden when the exam fits on a single page. If you expected multiple pages, check that scraping collected more than 100 questions.

**Captured an interactive page but Submit does nothing visible.**
Submit grades only the questions on the current page. Make sure at least one answer on that page is selected before pressing Submit.

## Notes

- This is an unofficial tool. Use it responsibly and respect ExamTopics' terms of service.
- CAPTCHA-protected pages are detected and skipped automatically; there is no bypass.
- Rate limiting and concurrency live as module-level constants in `src/examtopics/__init__.py`; do not duplicate them in your own scripts.
- Developer-facing details (template substitution contract, the offline iteration loop, project conventions) live in `AGENTS.md`.

## Acknowledgments

This project is a rewritten and enhanced version of two excellent tools:

- [**examtopics-downloader**](https://github.com/thatonecodes/examtopics-downloader) by [thatonecodes](https://github.com/thatonecodes) -- the original scraper that pioneered the idea.
- [**pretty-examtopics-downloader**](https://github.com/npapatheodorou/pretty-examtopics-downloader) by [npapatheodorou](https://github.com/npapatheodorou) -- the interactive HTML template used as the foundation for the study page.

This version combines the scraping logic with the interactive UI, adds pip-installability, concurrent downloads, improved answer detection, a polished CLI, and per-page pagination.

## License

MIT. See [LICENSE](LICENSE) for the full text. Copyright (c) 2026 kacppy.
