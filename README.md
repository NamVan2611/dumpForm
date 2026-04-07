# Google Form Automation Toolkit

A Python toolkit to parse Google Forms, generate answer distributions, create fake respondent data, and submit responses in batch with Playwright.

## Features

- Parse Google Form editor links (`/edit`) to detect:
  - question title
  - question type (`text_input`, `radio`, `checkbox`, `dropdown`)
  - options (for choice questions)
- Generate fake respondent data using Faker:
  - full name
  - email
  - phone
  - address
- Build ratio-based distributions:
  - radio: total ratio must equal 100%
  - checkbox: total ratio can exceed 100%
  - convert percentages to exact counts for a fixed submission total
- Auto-suggest realistic ratios for many questions
- Fill and submit Google Forms with Playwright:
  - text, radio, checkbox, dropdown
  - random delays between actions/submissions
  - headless or headed mode
  - retry support
- Interactive CLI orchestration with progress and final summary

## Project Files

- `main.py` - interactive CLI entrypoint
- `form_parser.py` - parse form questions from edit link
- `form_filler.py` - fill and submit form responses
- `ratio_manager.py` - validate ratios and generate exact distributions
- `auto_suggest.py` - auto-generate ratio configs
- `fake_data.py` - generate fake profile data
- `core.py` - shared config, errors, and logging helpers
- `config.json` - runtime config

## Requirements

- Python 3.10+
- Playwright + Chromium browser
- Faker

Install dependencies:

```bash
pip install playwright faker
playwright install chromium
```

## Configuration

Edit `config.json`:

```json
{
  "headless": true,
  "delay_min": 2,
  "delay_max": 5,
  "retry": 3
}
```

## How to Launch

Run the main interactive program:

```bash
py main.py
```

Then provide:

1. Form link (`/edit` or `/viewform`)
2. Number of submissions
3. Mode:
   - `1` manual ratio
   - `2` auto suggest

The program will:

1. Parse questions from the form
2. Build ratio plan
3. Submit responses with progress logs
4. Print summary (success/fail/elapsed + distribution snapshot)

