"""
form_parser.py
--------------
Parse questions from a Google Form **edit** URL using Playwright.

The editor is only available when you are signed in to Google and have access
to the form. Run with ``headless=False`` the first time if you need to log in
in the browser profile used by Playwright.

Typical URL shape::
    https://docs.google.com/forms/d/<FORM_ID>/edit
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Google Forms editor shows these (and others); we normalize to the four requested types.
_TYPE_ALIASES: tuple[tuple[str, str], ...] = (
    ("short answer", "text_input"),
    ("paragraph", "text_input"),
    ("multiple choice", "radio"),
    ("checkboxes", "checkbox"),
    ("checkbox", "checkbox"),
    ("dropdown", "dropdown"),
)


def _validate_edit_url(url: str) -> str:
    """Return stripped URL or raise ValueError if it does not look like an edit link."""
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL is empty.")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https.")
    if not parsed.netloc:
        raise ValueError("URL is missing a host.")
    path = parsed.path.rstrip("/") or ""
    if not path.endswith("/edit"):
        raise ValueError(
            "Expected a Google Form edit link whose path ends with '/edit' "
            "(e.g. https://docs.google.com/forms/d/<id>/edit)."
        )
    return cleaned


def _normalize_type(google_label: str) -> str:
    """Map Google Forms editor label text to one of the supported type strings."""
    if not google_label:
        return "unknown"
    lower = google_label.lower().strip()
    for needle, canonical in _TYPE_ALIASES:
        if needle in lower:
            return canonical
    # Short heuristics for minor label variants
    if "choice" in lower and "multiple" in lower:
        return "radio"
    if "check" in lower and "box" in lower:
        return "checkbox"
    if "drop" in lower and "down" in lower:
        return "dropdown"
    return "unknown"


def _detect_login_redirect(page_url: str, page_title: str) -> bool:
    """Heuristic: user was sent to Google account sign-in."""
    u = page_url.lower()
    t = (page_title or "").lower()
    if "accounts.google.com" in u or "signin" in u:
        return True
    if "sign in" in t and "google" in t:
        return True
    return False


def _extract_questions_js() -> str:
    """
    Browser-side extraction script. Google Forms minifies/obfuscates classes;
    we rely on roles, contenteditable fields, and data-item-id when present.
    """
    return r"""
    () => {
      const results = [];

      /** @type {HTMLElement[]} */
      const allItemNodes = Array.from(document.querySelectorAll('[data-item-id]'));
      // Keep only top-level item roots (avoid nested duplicate nodes for the same question).
      let roots = allItemNodes.filter((el) => {
        return !allItemNodes.some((other) => other !== el && other.contains(el));
      });

      // Fallback if attribute naming changes: list items under the form body
      if (roots.length === 0) {
        roots = Array.from(document.querySelectorAll('div[role="listitem"]'));
      }

      const isVisible = (el) => {
        if (!el || !(el instanceof HTMLElement)) return false;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0')
          return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };

      const collectTypeLabel = (root) => {
        const selectors = [
          '[role="combobox"]',
          '[role="listbox"]',
          '[aria-haspopup="listbox"]',
          'div[aria-expanded]',
        ];
        const candidates = [];
        for (const sel of selectors) {
          root.querySelectorAll(sel).forEach((el) => {
            const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
            if (t.length > 2 && t.length < 120) candidates.push(t);
          });
        }
        // Prefer strings that look like a known question type menu label
        const hints = /short answer|paragraph|multiple choice|checkbox|dropdown|linear scale|date|time/i;
        const scored = candidates.filter((c) => hints.test(c));
        if (scored.length) return scored[0];
        return candidates[0] || '';
      };

      const dedupePush = (arr, val) => {
        const v = (val || '').trim();
        if (v && !arr.includes(v)) arr.push(v);
      };

      for (const root of roots) {
        if (!isVisible(root)) continue;

        const editables = Array.from(root.querySelectorAll('[contenteditable="true"]'))
          .filter(isVisible);
        const title = (editables[0] && editables[0].innerText) ? editables[0].innerText.trim() : '';

        // Skip empty blocks and obvious non-question chrome (very short noise)
        if (!title && editables.length === 0) continue;

        const typeRaw = collectTypeLabel(root);

        const options = [];
        // Options usually follow the title in separate contenteditable rows
        for (let i = 1; i < editables.length; i++) {
          dedupePush(options, editables[i].innerText);
        }

        // Also pick up option rows rendered as plain text near radios/checkboxes
        root.querySelectorAll('label, [role="radio"], [role="checkbox"]').forEach((el) => {
          const p = el.closest('[data-item-id]') || el.parentElement;
          if (p !== root && p && !root.contains(p)) return;
          const txt = (el.textContent || '').trim();
          if (txt && txt.length < 500) dedupePush(options, txt);
        });

        // Section titles can look like questions; if there's no type control and no real title, skip
        if (!title && !typeRaw && options.length === 0) continue;

        results.push({
          question: title || '',
          typeLabel: typeRaw || '',
          options: options,
        });
      }

      return results;
    }
    """


def parse_google_form(
    edit_url: str,
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    navigation_timeout_ms: int = 90_000,
    wait_after_load_ms: int = 2_000,
) -> list[dict[str, Any]]:
    """
    Open a Google Form edit URL and return a list of question dictionaries.

    Parameters
    ----------
    edit_url:
        Must be an ``https://docs.google.com/forms/d/.../edit`` style link.
    headless:
        Set False to see the browser (useful for first-time Google login).
    slow_mo_ms:
        Optional delay between Playwright actions for debugging.
    navigation_timeout_ms:
        Max time to wait for navigation and selectors.
    wait_after_load_ms:
        Extra pause after load so the SPA can render questions.

    Returns
    -------
    list of dicts with keys: ``question``, ``type``, ``options``.

    Raises
    ------
    ValueError
        If the URL is not a valid edit link.
    RuntimeError
        On likely login redirect or empty extraction when the page did not load as expected.
    PlaywrightTimeoutError / PlaywrightError
        On browser timeouts or Playwright failures.
    """
    url = _validate_edit_url(edit_url)
    out: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        try:
            context = browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(navigation_timeout_ms)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            except PlaywrightTimeoutError as e:
                raise RuntimeError(
                    "Timed out loading the form editor. Check the URL and network access."
                ) from e

            if _detect_login_redirect(page.url, page.title()):
                raise RuntimeError(
                    "Redirected to Google sign-in. Open the form while logged in, or run with "
                    "headless=False and complete login in the Playwright browser window."
                )

            # Wait for question-related DOM; editor is a heavy SPA.
            try:
                page.wait_for_selector(
                    '[data-item-id], div[role="listitem"]',
                    timeout=navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                # Form might still be loading or access denied
                if "Sorry" in (page.content() or "") or "access" in (page.content() or "").lower():
                    raise RuntimeError(
                        "Could not access the form (permission denied or link invalid). "
                        "Ensure you can open this edit URL when signed in."
                    ) from None
                raise RuntimeError(
                    "Timed out waiting for form questions to appear. "
                    "If the form loads slowly, increase navigation_timeout_ms."
                ) from None

            time.sleep(max(0, wait_after_load_ms) / 1000.0)

            raw_rows: list[dict[str, Any]] = page.evaluate(_extract_questions_js())

            for row in raw_rows:
                label = row.get("typeLabel") or ""
                canonical = _normalize_type(label)
                opts = row.get("options") or []
                # Text questions do not list selectable options in the same way as choice fields.
                if canonical == "text_input":
                    opts = []

                out.append(
                    {
                        "question": row.get("question") or "",
                        "type": canonical,
                        "options": opts,
                    }
                )

            # De-duplicate consecutive identical questions (rare duplicate nodes)
            deduped: list[dict[str, Any]] = []
            seen = set()
            for item in out:
                key = (item["question"], item["type"], tuple(item["options"]))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            out = deduped

        finally:
            browser.close()

    return out


def parse_google_form_json(
    edit_url: str,
    *,
    headless: bool = True,
    indent: int | None = 2,
    **kwargs: Any,
) -> str:
    """Same as :func:`parse_google_form` but returns a JSON string."""
    data = parse_google_form(edit_url, headless=headless, **kwargs)
    return json.dumps(data, ensure_ascii=False, indent=indent)


def main() -> None:
    """Minimal CLI: python form_parser.py <edit_url>"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Parse questions from a Google Form edit URL.")
    parser.add_argument("edit_url", help="Google Form URL ending with /edit")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser (helps with Google login).",
    )
    parser.add_argument(
        "--no-indent",
        action="store_true",
        help="Print compact JSON.",
    )
    args = parser.parse_args()

    try:
        payload = parse_google_form(
            args.edit_url,
            headless=not args.headed,
        )
    except ValueError as e:
        print(f"Invalid input: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(3)
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        print(f"Browser error: {e}", file=sys.stderr)
        sys.exit(4)

    indent = None if args.no_indent else 2
    print(json.dumps(payload, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
