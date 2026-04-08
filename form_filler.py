"""
form_filler.py
--------------
Fill a Google Form (response URL) with Playwright.

Features:
- Class-based API
- Supports text input, radio, checkbox, dropdown
- Maps fake data to text questions
- Submits one or many responses
- Ratio distribution for answers
- Random delay between actions and between submissions
- Console progress output
- Headless/headed mode
- Returns success/failure summary
"""

from __future__ import annotations

import argparse
import json
import random
import time
from typing import Any
from urllib.parse import urlparse

from core import FillerError, OperationResult, get_logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from fake_data import generate_fake_data


class GoogleFormFiller:
    """Automates filling Google Forms response pages."""

    def __init__(
        self,
        *,
        headless: bool = True,
        min_delay: float = 0.2,
        max_delay: float = 0.8,
        timeout_ms: int = 60_000,
    ) -> None:
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout_ms = timeout_ms
        self._logger = get_logger("form_filler")

    def _sleep_random(self, *, low: float | None = None, high: float | None = None) -> None:
        """Sleep random time in seconds."""
        lo = self.min_delay if low is None else low
        hi = self.max_delay if high is None else high
        if hi < lo:
            lo, hi = hi, lo
        time.sleep(random.uniform(lo, hi))

    @staticmethod
    def _validate_form_url(url: str) -> str:
        """Ensure the URL looks like a Google Form response link."""
        cleaned = url.strip()
        if not cleaned:
            raise ValueError("form_url is empty.")
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("form_url must use http or https.")
        if "docs.google.com" not in parsed.netloc:
            raise ValueError("form_url must be a docs.google.com link.")
        if "/forms/" not in parsed.path:
            raise ValueError("form_url does not look like a Google Form URL.")
        return cleaned

    @staticmethod
    def _normalize(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    @staticmethod
    def _strip_choice_aria(aria: str) -> str:
        """Column/option label from aria-label (strip trailing ', row N of M', etc.)."""
        a = (aria or "").strip()
        if "," in a:
            a = a.split(",")[0].strip()
        return a

    @staticmethod
    def _is_radio_grid_answer_map(spec: Any) -> bool:
        if not isinstance(spec, dict) or not spec:
            return False
        if "distribution" in spec or "choices" in spec or "answer" in spec:
            return False
        return all(isinstance(k, str) and isinstance(v, str) for k, v in spec.items())

    @staticmethod
    def _is_checkbox_grid_answer_map(spec: Any) -> bool:
        if not isinstance(spec, dict) or not spec:
            return False
        if "distribution" in spec or "choices" in spec:
            return False
        return all(isinstance(k, str) and isinstance(v, list) for k, v in spec.items())

    def _fill_radio_grid(self, q: Any, answer_map: dict[str, str]) -> None:
        """One radiogroup per row; pick radio whose column label matches answer_map[row] by row order."""
        groups = q.locator('[role="radiogroup"]')
        row_keys = list(answer_map.keys())
        for gi in range(groups.count()):
            g = groups.nth(gi)
            col_pick: str | None = None
            if gi < len(row_keys):
                col_pick = answer_map.get(row_keys[gi])
            elif row_keys:
                col_pick = answer_map.get(row_keys[-1])
            radios = g.locator("[role='radio']")
            clicked = False
            if col_pick:
                for ri in range(radios.count()):
                    rad = radios.nth(ri)
                    aria = self._strip_choice_aria((rad.get_attribute("aria-label") or "").strip())
                    if self._normalize(aria) == self._normalize(col_pick):
                        rad.click()
                        clicked = True
                        self._sleep_random()
                        break
            if not clicked and radios.count() > 0:
                radios.first.click()
                self._sleep_random()

    def _fill_checkbox_grid(self, q: Any, answer_map: dict[str, list[str]]) -> None:
        """Each row: [role=group] with one checkbox per column; targets matched by aria label."""
        row_keys = list(answer_map.keys())
        groups = q.locator('[role="group"]')
        row_idx = 0
        for gi in range(groups.count()):
            g = groups.nth(gi)
            nchk = g.locator("[role='checkbox']").count()
            if nchk < 2:
                continue
            if row_idx >= len(row_keys):
                break
            targets = [str(t) for t in (answer_map.get(row_keys[row_idx]) or [])]
            row_idx += 1
            for ci in range(nchk):
                chk = g.locator("[role='checkbox']").nth(ci)
                aria = self._strip_choice_aria((chk.get_attribute("aria-label") or "").strip())
                if not targets:
                    if ci == 0:
                        chk.click()
                        self._sleep_random()
                    continue
                if any(self._normalize(aria) == self._normalize(t) for t in targets):
                    chk.click()
                    self._sleep_random()

    @staticmethod
    def _answers_to_map(answers_data: Any) -> dict[str, Any]:
        """
        Convert supported answer formats to {question_text: answer_spec}.

        Supported:
        - dict[str, Any]
        - list[{"question": "...", "answer": ...}]
        """
        if isinstance(answers_data, dict):
            return answers_data
        if isinstance(answers_data, list):
            out: dict[str, Any] = {}
            for item in answers_data:
                if isinstance(item, dict) and "question" in item:
                    out[str(item["question"])] = item.get("answer")
            return out
        raise ValueError("answers_data must be dict or list of {'question', 'answer'} objects.")

    def _resolve_answer_by_ratio(self, answer_spec: Any) -> Any:
        """
        Resolve one answer value using ratio distribution.

        Accepted ratio formats:
        1) {"distribution": {"Có": 0.7, "Không": 0.3}}
        2) {"choices": ["A", "B", "C"], "ratios": [0.2, 0.3, 0.5]}
        3) fixed value (str/list/None) => returned as-is
        """
        if not isinstance(answer_spec, dict):
            return answer_spec

        if "distribution" in answer_spec and isinstance(answer_spec["distribution"], dict):
            dist = answer_spec["distribution"]
            choices = list(dist.keys())
            weights = [float(dist[c]) for c in choices]
            if not choices:
                return None
            return random.choices(choices, weights=weights, k=1)[0]

        if (
            "choices" in answer_spec
            and "ratios" in answer_spec
            and isinstance(answer_spec["choices"], list)
            and isinstance(answer_spec["ratios"], list)
        ):
            choices = answer_spec["choices"]
            ratios = answer_spec["ratios"]
            if len(choices) == 0 or len(choices) != len(ratios):
                return None
            weights = [float(x) for x in ratios]
            return random.choices(choices, weights=weights, k=1)[0]

        # Support nested spec: {"answer": {...distribution...}}
        if "answer" in answer_spec:
            return self._resolve_answer_by_ratio(answer_spec["answer"])

        return answer_spec

    def _pick_text_value(self, question_text: str, answer_spec: Any, fake_data: dict[str, str]) -> str:
        """
        Pick value for text questions.

        Priority:
        1) Resolved explicit answer (including ratio result)
        2) fake_data mapped by keyword
        3) fallback sentence
        """
        explicit_answer = self._resolve_answer_by_ratio(answer_spec)
        if explicit_answer is not None:
            return str(explicit_answer)

        q = self._normalize(question_text)
        if any(k in q for k in ("name", "họ tên", "ho ten", "full name", "tên")):
            return fake_data["name"]
        if any(k in q for k in ("email", "e-mail", "mail")):
            return fake_data["email"]
        if any(k in q for k in ("phone", "số điện thoại", "so dien thoai", "mobile", "điện thoại")):
            return fake_data["phone"]
        if any(k in q for k in ("address", "địa chỉ", "dia chi")):
            return fake_data["address"]
        return "This is an automated test response."

    def _choose_radio_target(self, answer_spec: Any) -> str | None:
        val = self._resolve_answer_by_ratio(answer_spec)
        if val is None:
            return None
        return str(val)

    def _choose_checkbox_targets(self, answer_spec: Any) -> list[str]:
        """
        Resolve checkbox targets.

        Supports:
        - ["A", "B"]                        (fixed)
        - "A"                               (single fixed)
        - {"distribution": {...}}           (select one by ratio)
        - {"answer": [...]}
        """
        val = self._resolve_answer_by_ratio(answer_spec)
        if val is None:
            return []
        if isinstance(val, list):
            return [str(x) for x in val]
        return [str(val)]

    @staticmethod
    def _extract_question_title(q_locator: Any) -> str:
        """Find question title text from common heading areas."""
        title = ""
        title_node = q_locator.locator("[role='heading']").first
        if title_node.count() > 0:
            title = title_node.inner_text().strip()
        if not title:
            fallback_title = q_locator.locator("div[dir='auto']").first
            if fallback_title.count() > 0:
                title = fallback_title.inner_text().strip()
        return title

    def _fill_page(self, page: Any, answers: dict[str, Any], fake_data: dict[str, str]) -> OperationResult:
        """Fill questions on current page and click submit."""
        questions = page.locator("div[role='listitem']")
        total = questions.count()
        if total == 0:
            return OperationResult(False, "No question blocks found on form page.")

        for i in range(total):
            q = questions.nth(i)
            if not q.is_visible():
                continue

            title = self._extract_question_title(q)
            if not title:
                continue
            answer_spec = answers.get(title)

            text_input = q.locator("input[type='text'], textarea").first
            if text_input.count() > 0:
                text_input.fill(self._pick_text_value(title, answer_spec, fake_data))
                self._sleep_random()
                continue

            radio_groups = q.locator("[role='radiogroup']")
            if radio_groups.count() >= 2 and self._is_radio_grid_answer_map(answer_spec):
                self._fill_radio_grid(q, answer_map=answer_spec)
                continue

            if isinstance(answer_spec, dict) and self._is_checkbox_grid_answer_map(answer_spec):
                cb_row_groups = 0
                grp = q.locator('[role="group"]')
                for j in range(grp.count()):
                    if grp.nth(j).locator("[role='checkbox']").count() >= 2:
                        cb_row_groups += 1
                if cb_row_groups >= 2:
                    self._fill_checkbox_grid(q, answer_map=answer_spec)
                    continue

            radios = q.locator("[role='radio']")
            if radios.count() > 0:
                target = self._choose_radio_target(answer_spec)
                clicked = False
                for r_idx in range(radios.count()):
                    radio = radios.nth(r_idx)
                    aria = (radio.get_attribute("aria-label") or "").strip()
                    if target is None or self._normalize(aria) == self._normalize(target):
                        radio.click()
                        clicked = True
                        self._sleep_random()
                        break
                if not clicked and radios.count() > 0:
                    radios.first.click()
                    self._sleep_random()
                continue

            checks = q.locator("[role='checkbox']")
            if checks.count() > 0:
                targets = self._choose_checkbox_targets(answer_spec)
                clicked_count = 0
                for c_idx in range(checks.count()):
                    chk = checks.nth(c_idx)
                    aria = (chk.get_attribute("aria-label") or "").strip()
                    if not targets:
                        if clicked_count == 0:
                            chk.click()
                            clicked_count += 1
                            self._sleep_random()
                        continue
                    if any(self._normalize(aria) == self._normalize(t) for t in targets):
                        chk.click()
                        clicked_count += 1
                        self._sleep_random()
                continue

            dropdown = q.locator("[role='listbox'], [role='combobox']").first
            if dropdown.count() > 0:
                dropdown.click()
                self._sleep_random()
                wanted = self._choose_radio_target(answer_spec)
                options = page.locator("[role='option']")
                selected = False
                for o_idx in range(options.count()):
                    opt = options.nth(o_idx)
                    label = opt.inner_text().strip()
                    if wanted is None or self._normalize(label) == self._normalize(wanted):
                        opt.click()
                        selected = True
                        self._sleep_random()
                        break
                if not selected and options.count() > 0:
                    options.first.click()
                    self._sleep_random()

        submit_btn = page.get_by_role("button", name="Submit")
        if submit_btn.count() == 0:
            submit_btn = page.get_by_role("button", name="Gửi")
        if submit_btn.count() == 0:
            submit_btn = page.get_by_role("button", name="Nộp")
        if submit_btn.count() == 0:
            return OperationResult(False, "Submit button not found.")

        submit_btn.first.click()
        self._sleep_random()
        page.wait_for_timeout(1200)
        html = page.content().lower()
        success_indicators = (
            "your response has been recorded",
            "câu trả lời của bạn đã được ghi lại",
            "đã ghi lại",
        )
        if any(x in html for x in success_indicators):
            return OperationResult(True, "Form submitted successfully.")
        return OperationResult(True, "Submit clicked (confirmation text not detected).")

    def fill_form_once(self, form_url: str, answers_data: Any) -> dict[str, Any]:
        """Fill and submit one response."""
        try:
            url = self._validate_form_url(form_url)
            answers = self._answers_to_map(answers_data)
            fake_data = generate_fake_data()  # New fake identity for each submission
        except Exception as exc:
            return {"success": False, "message": f"Invalid input: {exc}"}

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(locale="vi-VN")
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                self._sleep_random()
                result = self._fill_page(page, answers, fake_data)
                browser.close()
                return {"success": result.success, "message": result.message}

        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return {"success": False, "message": f"Browser error: {exc}"}
        except Exception as exc:
            return {"success": False, "message": f"Unexpected error: {exc}"}

    def fill_form(self, form_url: str, answers_data: Any) -> dict[str, Any]:
        """Backward-compatible alias for one submission."""
        return self.fill_form_once(form_url, answers_data)

    def submit_multiple(
        self,
        form_url: str,
        answers_data: Any,
        submission_count: int,
        *,
        min_submission_delay: float = 0.8,
        max_submission_delay: float = 2.0,
        stop_on_error: bool = False,
    ) -> dict[str, Any]:
        """
        Submit the same form multiple times.

        Parameters
        ----------
        submission_count:
            Number of responses to submit (e.g. 301).
        min_submission_delay, max_submission_delay:
            Random delay range between submissions.
        stop_on_error:
            If True, stop immediately when one submission fails.
        """
        if submission_count <= 0:
            return {"success": False, "message": "submission_count must be > 0."}

        started = time.time()
        ok_count = 0
        fail_count = 0
        errors: list[str] = []

        print(f"Start submitting {submission_count} responses...")
        for idx in range(1, submission_count + 1):
            result = self.fill_form_once(form_url, answers_data)
            if result.get("success"):
                ok_count += 1
                status = "OK"
            else:
                fail_count += 1
                status = "FAIL"
                errors.append(f"#{idx}: {result.get('message')}")

            # Console progress
            elapsed = time.time() - started
            print(
                f"[{idx}/{submission_count}] {status} | "
                f"success={ok_count} fail={fail_count} | elapsed={elapsed:.1f}s"
            )

            if fail_count and stop_on_error:
                print("Stopped early because stop_on_error=True")
                break

            if idx < submission_count:
                self._sleep_random(low=min_submission_delay, high=max_submission_delay)

        total_elapsed = time.time() - started
        all_success = fail_count == 0
        message = (
            f"Completed {ok_count + fail_count} submissions. "
            f"Success={ok_count}, Fail={fail_count}, Elapsed={total_elapsed:.1f}s"
        )
        if errors:
            message += f". First error: {errors[0]}"

        return {
            "success": all_success,
            "message": message,
            "total": ok_count + fail_count,
            "success_count": ok_count,
            "fail_count": fail_count,
            "errors": errors,
        }

    def submit_plan_with_reuse(
        self,
        form_url: str,
        submissions_plan: list[dict[str, Any]],
        *,
        min_submission_delay: float = 0.8,
        max_submission_delay: float = 2.0,
        retry: int = 0,
    ) -> dict[str, Any]:
        """
        Faster batch mode: reuse one browser/context/page for all submissions.
        """
        try:
            url = self._validate_form_url(form_url)
        except Exception as exc:  # noqa: BLE001
            raise FillerError(f"Invalid URL: {exc}") from exc

        total = len(submissions_plan)
        if total == 0:
            return {"success": False, "message": "Empty submission plan.", "total": 0}

        ok_count = 0
        fail_count = 0
        errors: list[str] = []
        started = time.time()
        self._logger.info("Start batch submit with reused browser: total=%s", total)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(locale="vi-VN")
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                for idx, answers in enumerate(submissions_plan, start=1):
                    result = OperationResult(False, "Unknown error")
                    for attempt in range(retry + 1):
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                        self._sleep_random()
                        fake_data = generate_fake_data()
                        result = self._fill_page(page, self._answers_to_map(answers), fake_data)
                        if result.success:
                            break
                        if attempt < retry:
                            self._sleep_random(low=min_submission_delay, high=max_submission_delay)

                    if result.success:
                        ok_count += 1
                    else:
                        fail_count += 1
                        errors.append(f"#{idx}: {result.message}")

                    elapsed = time.time() - started
                    status = "OK" if result.success else "FAIL"
                    print(
                        f"[{idx}/{total}] {status} | success={ok_count} fail={fail_count} | "
                        f"elapsed={elapsed:.1f}s"
                    )
                    if idx < total:
                        self._sleep_random(low=min_submission_delay, high=max_submission_delay)
            finally:
                browser.close()

        all_success = fail_count == 0
        return {
            "success": all_success,
            "message": (
                f"Completed {total} submissions. Success={ok_count}, Fail={fail_count}, "
                f"Elapsed={time.time() - started:.1f}s"
            ),
            "total": total,
            "success_count": ok_count,
            "fail_count": fail_count,
            "errors": errors,
        }


def _load_answers_from_json(path: str) -> Any:
    """Load answers config from JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    """CLI entry."""
    parser = argparse.ArgumentParser(description="Fill and submit Google Form responses with Playwright.")
    parser.add_argument("form_url", help="Google Form response URL")
    parser.add_argument(
        "--answers-file",
        required=True,
        help="Path to JSON file containing answers map or list format.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of responses to submit (example: 301).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    parser.add_argument("--min-action-delay", type=float, default=0.2)
    parser.add_argument("--max-action-delay", type=float, default=0.8)
    parser.add_argument("--min-submission-delay", type=float, default=0.8)
    parser.add_argument("--max-submission-delay", type=float, default=2.0)
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()

    try:
        answers_data = _load_answers_from_json(args.answers_file)
    except Exception as exc:
        print({"success": False, "message": f"Cannot load answers file: {exc}"})
        return

    filler = GoogleFormFiller(
        headless=args.headless,
        min_delay=args.min_action_delay,
        max_delay=args.max_action_delay,
    )
    result = filler.submit_multiple(
        args.form_url,
        answers_data,
        args.count,
        min_submission_delay=args.min_submission_delay,
        max_submission_delay=args.max_submission_delay,
        stop_on_error=args.stop_on_error,
    )
    print(result)


if __name__ == "__main__":
    main()
