"""
main.py
-------
CLI orchestrator:
1) Parse form questions
2) Build ratio plan (manual or auto)
3) Submit many responses with progress and summary
"""

from __future__ import annotations

import random
import time
from collections import Counter
from typing import Any

from auto_suggest import AutoSuggest
from core import ConfigError, get_logger, load_config
from fake_data import generate_fake_data
from form_filler import GoogleFormFiller
from form_parser import parse_google_form
from ratio_manager import RatioManager, RatioValidationError

def _normalize_url(link: str) -> tuple[str, str]:
    """
    Return (edit_url, viewform_url) from a user-provided form link.
    Supports either /edit or /viewform input.
    """
    cleaned = link.strip()
    if not cleaned:
        raise ValueError("Form link is empty.")

    if "/edit" in cleaned:
        edit_url = cleaned
        view_url = cleaned.replace("/edit", "/viewform")
    elif "/viewform" in cleaned:
        view_url = cleaned
        edit_url = cleaned.replace("/viewform", "/edit")
    else:
        raise ValueError("Link must contain '/edit' or '/viewform'.")
    return edit_url, view_url


def _ask_int(prompt: str, minimum: int = 1) -> int:
    """Prompt user for integer >= minimum."""
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
            if value < minimum:
                print(f"Please enter a number >= {minimum}.")
                continue
            return value
        except ValueError:
            print("Invalid number. Try again.")


def _ask_mode() -> int:
    """Prompt user for generation mode."""
    print("\nChoose mode:")
    print("1) Manual ratio")
    print("2) Auto suggest")
    while True:
        raw = input("Mode (1/2): ").strip()
        if raw in {"1", "2"}:
            return int(raw)
        print("Please choose 1 or 2.")


def _parse_manual_ratios(questions: list[dict[str, Any]], manager: RatioManager) -> dict[str, dict[str, Any]]:
    """
    Ask user to input ratio percentages for each choice question.

    Return format:
    {
      "<question>": {"type": "radio|checkbox", "ratio": {"opt": pct, ...}}
    }
    """
    config: dict[str, dict[str, Any]] = {}

    for q in questions:
        q_text = q.get("question", "")
        q_type = q.get("type", "")
        options = q.get("options") or []
        if q_type not in {"radio", "checkbox"} or not options:
            continue

        print("\n" + "-" * 72)
        print(f"Question: {q_text}")
        print(f"Type: {q_type}")
        for i, opt in enumerate(options, start=1):
            print(f"  {i}. {opt}")
        print(
            "Enter percentages in the same option order, comma-separated.\n"
            "Example: 40,30,30"
        )

        while True:
            raw = input("Ratios (%): ").strip()
            parts = [x.strip() for x in raw.split(",") if x.strip()]
            if len(parts) != len(options):
                print(f"You must provide exactly {len(options)} percentages.")
                continue
            try:
                values = [float(x) for x in parts]
            except ValueError:
                print("All percentages must be numeric.")
                continue

            ratio = {opt: pct for opt, pct in zip(options, values)}
            try:
                manager.validate_ratio_config(q_type, ratio)
            except RatioValidationError as exc:
                print(f"Invalid ratio: {exc}")
                continue
            config[q_text] = {"type": q_type, "ratio": ratio}
            break

    return config


def _auto_suggest_ratios(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Generate ratio config automatically for all choice questions."""
    specs: list[dict[str, Any]] = []
    for q in questions:
        q_text = q.get("question", "")
        q_type = q.get("type", "")
        options = q.get("options") or []
        if q_type in {"radio", "checkbox"} and options:
            specs.append({"question": q_text, "type": q_type, "options": options})

    suggester = AutoSuggest()
    return suggester.suggest_many(specs)


def _build_submission_plan(
    questions: list[dict[str, Any]],
    ratio_config: dict[str, dict[str, Any]],
    submission_count: int,
    manager: RatioManager,
) -> list[dict[str, Any]]:
    """
    Convert ratio configs to exact per-submission answers.

    Output:
      [
        {"Q1": "A", "Q2": ["X","Y"], "Email": None, ...},
        ...
      ]
    """
    plan: list[dict[str, Any]] = [{} for _ in range(submission_count)]

    # Pre-calculate exact answer arrays for ratio questions.
    distributed: dict[str, list[Any]] = {}
    for q_text, meta in ratio_config.items():
        distributed[q_text] = manager.build_distribution(
            meta["type"],
            submission_count,
            meta["ratio"],
            shuffle=True,
        )

    for q in questions:
        q_text = q.get("question", "")
        q_type = q.get("type", "")
        if not q_text:
            continue

        # Ratio-based question: use exact distributed answer per submission index.
        if q_text in distributed:
            answers = distributed[q_text]
            for i in range(submission_count):
                plan[i][q_text] = answers[i]
            continue

        # Text questions: leave as None so form_filler maps to fake data each submission.
        if q_type == "text_input":
            for i in range(submission_count):
                plan[i][q_text] = None

    return plan


def main() -> None:
    """Interactive CLI entrypoint."""
    logger = get_logger("main")
    print("=== Google Form Batch Filler ===")

    try:
        try:
            config = load_config("config.json")
        except ConfigError as exc:
            logger.warning("Config error: %s. Falling back to defaults.", exc)
            config = load_config("__missing_config__.json")
        print(
            "Loaded config: "
            f"headless={config.headless}, "
            f"delay_min={config.delay_min}, "
            f"delay_max={config.delay_max}, "
            f"retry={config.retry}"
        )

        form_link = input("Enter form link (/edit or /viewform): ").strip()
        submission_count = _ask_int("Number of submissions: ", minimum=1)
        mode = _ask_mode()

        edit_url, view_url = _normalize_url(form_link)

        # Parse questions from editor page.
        # NOTE: If your form requires login, run with headed browser for parser.
        print("\nParsing form questions...")
        parse_started = time.time()
        questions = parse_google_form(edit_url, headless=False)
        if not questions:
            print("No questions detected. Stop.")
            return

        print(f"Detected {len(questions)} questions.")
        logger.info("Parsing completed in %.2fs", time.time() - parse_started)

        # Call fake_data module once here (requirement) and show sample preview.
        preview = generate_fake_data()
        print(
            f"Sample fake identity preview: {preview.get('name')} | "
            f"{preview.get('email')} | {preview.get('phone')}"
        )

        manager = RatioManager()
        if mode == 1:
            ratio_config = _parse_manual_ratios(questions, manager)
        else:
            ratio_config = _auto_suggest_ratios(questions)
            print("\nAuto-suggest ratio config:")
            for q_text, meta in ratio_config.items():
                print(f"- {q_text}")
                print(f"  type={meta['type']}, ratio={meta['ratio']}")

        # Build exact plan for all submissions.
        plan_started = time.time()
        plan = _build_submission_plan(questions, ratio_config, submission_count, manager)
        logger.info("Plan build completed in %.2fs", time.time() - plan_started)

        filler = GoogleFormFiller(headless=config.headless)
        print("\nSubmitting responses...")
        submit_started = time.time()
        batch_result = filler.submit_plan_with_reuse(
            view_url,
            plan,
            min_submission_delay=config.delay_min,
            max_submission_delay=config.delay_max,
            retry=config.retry,
        )
        total_elapsed = time.time() - submit_started
        success = int(batch_result.get("success_count", 0))
        failed = int(batch_result.get("fail_count", 0))
        errors = list(batch_result.get("errors", []))
        logger.info("Submit stage completed in %.2fs", total_elapsed)

        # Optional quick ratio audit for radio questions.
        print("\n=== Summary ===")
        print(f"Total submissions requested: {submission_count}")
        print(f"Success: {success}")
        print(f"Failed: {failed}")
        print(f"Elapsed: {total_elapsed:.1f}s")

        if ratio_config:
            print("\nPlanned distribution snapshot:")
            for q_text, meta in ratio_config.items():
                q_type = meta["type"]
                if q_type == "radio":
                    values = [p.get(q_text) for p in plan]
                    counts = Counter(values)
                    print(f"- {q_text}")
                    print(f"  actual_count={dict(counts)}")
                else:
                    # Checkbox summary: count each option appearance.
                    option_counts: Counter[str] = Counter()
                    for p in plan:
                        picks = p.get(q_text) or []
                        if isinstance(picks, list):
                            option_counts.update(str(x) for x in picks)
                    print(f"- {q_text}")
                    print(f"  actual_option_appearances={dict(option_counts)}")

        if errors:
            print("\nErrors (first 10):")
            for line in errors[:10]:
                print(f"  {line}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as exc:
        logger.exception("Fatal runtime error")
        print(f"\nFatal error: {exc}")


if __name__ == "__main__":
    main()

