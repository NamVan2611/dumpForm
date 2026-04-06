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

# Google Forms editor labels (English + Vietnamese UI); normalized to the four output types.
_TYPE_ALIASES: tuple[tuple[str, str], ...] = (
    # English
    ("short answer", "text_input"),
    ("paragraph", "text_input"),
    ("multiple choice", "radio"),
    ("checkboxes", "checkbox"),
    ("checkbox", "checkbox"),
    ("dropdown", "dropdown"),
    # Vietnamese (docs.google.com forms UI)
    ("câu trả lời ngắn", "text_input"),
    ("đoạn văn", "text_input"),
    ("trắc nghiệm", "radio"),
    ("hộp kiểm", "checkbox"),
    ("danh sách thả xuống", "dropdown"),
    ("thả xuống", "dropdown"),
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
    # Collapse whitespace so substrings match across line breaks in the UI.
    lower = " ".join(google_label.lower().split())
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


def _canonical_from_hints(label: str, dom_hint: str) -> str:
    """Prefer menu label; fall back to DOM-derived hint (radio/checkbox/dropdown/text_input)."""
    from_label = _normalize_type(label)
    if from_label != "unknown":
        return from_label
    hint = (dom_hint or "").strip().lower()
    if hint in ("radio", "checkbox", "dropdown", "text_input"):
        return hint
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
      /*
       * Use *innermost* [data-item-id] nodes only. The previous "outermost" filter kept a single
       * wrapper that contained the whole form, merging every question into one blob.
       */
      let roots = allItemNodes.filter((el) => !el.querySelector('[data-item-id]'));

      // Fallback: list items in the editor canvas (one per block).
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

      /** Guess type from interactive controls when the menu label is localized or missing. */
      const inferTypeFromDom = (root) => {
        const radios = root.querySelectorAll('[role="radio"]');
        const checks = root.querySelectorAll('[role="checkbox"]');
        const optionRadios = Array.from(radios).filter((n) => {
          const t = (n.getAttribute('aria-label') || '').toLowerCase();
          if (t.includes('add') || t.includes('thêm')) return false;
          return true;
        });
        const optionChecks = Array.from(checks).filter((n) => {
          const t = (n.getAttribute('aria-label') || '').toLowerCase();
          if (t.includes('add') || t.includes('thêm')) return false;
          return true;
        });
        if (optionChecks.length >= 1) return 'checkbox';
        if (optionRadios.length >= 1) return 'radio';
        /*
         * Do not use [role="listbox"] / combobox here: the *question type* picker
         * (Trắc nghiệm, Câu trả lời ngắn, …) uses the same roles and would label
         * every item as dropdown. Dropdown questions are detected via menu text instead.
         */
        const editables = root.querySelectorAll('[contenteditable="true"]');
        if (editables.length <= 1) return 'text_input';
        return '';
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
            if (t.length > 2 && t.length < 160) candidates.push(t);
          });
        }
        const hints =
          /short answer|paragraph|multiple choice|checkbox|dropdown|linear scale|date|time|đoạn văn|câu trả lời|trắc nghiệm|hộp kiểm|thả xuống|danh sách|nhiều lựa chọn/i;
        const scored = candidates.filter((c) => hints.test(c));
        if (scored.length) return scored[0];
        return candidates[0] || '';
      };

      const dedupePush = (arr, val) => {
        const v = (val || '').trim();
        if (v && !arr.includes(v)) arr.push(v);
      };

      /**
       * Option labels in the editor are often NOT extra contenteditable nodes; they sit in
       * plain div/span rows next to each [role=radio] or [role=checkbox]. Walk from each
       * control to find text (contenteditable, input, dir=auto, aria-label, row innerText).
       */
      const collectChoiceOptions = (root, questionTitle) => {
        const out = [];
        const titleNorm = (questionTitle || '').trim().replace(/\s+/g, ' ');

        const isAddOptionControl = (ctrl) => {
          const al = ((ctrl.getAttribute && ctrl.getAttribute('aria-label')) || '').toLowerCase();
          if (/thêm tùy chọn|add option|add choice|add another|add or|thêm câu trả lời/i.test(al))
            return true;
          const tt = (ctrl.getAttribute && ctrl.getAttribute('data-tooltip')) || '';
          if (/add option|thêm/i.test(tt)) return true;
          return false;
        };

        const cleanLabel = (raw) => {
          let t = (raw || '').trim().replace(/\s+/g, ' ');
          if (!t) return '';
          if (titleNorm && t === titleNorm) return '';
          if (titleNorm && t.startsWith(titleNorm)) t = t.slice(titleNorm.length).trim();
          t = t.replace(/^[A-Z0-9][.)]\s*/, '').trim();
          return t;
        };

        const titleEl = root.querySelector('[contenteditable="true"]');

        const harvestFromRow = (ctrl) => {
          if (!ctrl || isAddOptionControl(ctrl)) return '';

          let best = '';
          let el = ctrl;

          for (let depth = 0; depth < 16; depth++) {
            el = el.parentElement;
            if (!el || !root.contains(el)) break;

            const autos = el.querySelectorAll('div[dir="auto"], span[dir="auto"]');
            for (const a of autos) {
              if (titleEl && (titleEl === a || titleEl.contains(a))) continue;
              const t = cleanLabel(a.innerText || '');
              if (t && t.length < 2000) {
                best = t;
                break;
              }
            }
            if (best) break;

            const ces = Array.from(el.querySelectorAll('[contenteditable="true"]')).filter(isVisible);
            for (const ce of ces) {
              if (titleEl && (titleEl === ce || titleEl.contains(ce))) continue;
              const t = cleanLabel(ce.innerText || '');
              if (t) {
                best = t;
                break;
              }
            }
            if (best) break;

            const inp = el.querySelector(
              'input[type="text"], input:not([type]), textarea'
            );
            if (inp) {
              const v = cleanLabel(inp.value || '');
              const ar = cleanLabel(inp.getAttribute('aria-label') || '');
              const pick = v || ar;
              if (pick) {
                best = pick;
                break;
              }
            }
          }

          if (!best) {
            const p = ctrl.parentElement;
            if (p) {
              const kids = Array.from(p.children);
              const ix = kids.indexOf(ctrl);
              for (let j = ix + 1; j < kids.length; j++) {
                const t = cleanLabel(kids[j].innerText || '');
                if (t) {
                  best = t;
                  break;
                }
                const ce = kids[j].querySelector('[contenteditable="true"]');
                if (ce) {
                  const t2 = cleanLabel(ce.innerText || '');
                  if (t2) {
                    best = t2;
                    break;
                  }
                }
              }
            }
          }

          if (!best) {
            let n = ctrl.nextSibling;
            while (n) {
              if (n.nodeType === 1) {
                const eln = /** @type {HTMLElement} */ (n);
                const t = cleanLabel(eln.innerText || '');
                if (t) {
                  best = t;
                  break;
                }
              }
              n = n.nextSibling;
            }
          }

          if (!best) {
            const al = cleanLabel((ctrl.getAttribute && ctrl.getAttribute('aria-label')) || '');
            if (al) best = al;
          }

          if (!best) {
            const row =
              ctrl.closest('div[role="listitem"]') ||
              ctrl.closest('div[role="option"]') ||
              ctrl.parentElement;
            if (row && root.contains(row)) {
              let t = cleanLabel(row.innerText || '');
              if (t && t.length < 800) best = t;
            }
          }

          return best;
        };

        const seenControls = new Set();
        const runControls = (nodeList) => {
          nodeList.forEach((ctrl) => {
            if (!ctrl || !root.contains(ctrl) || seenControls.has(ctrl)) return;
            seenControls.add(ctrl);
            if (isAddOptionControl(ctrl)) return;
            const txt = harvestFromRow(ctrl);
            if (txt) dedupePush(out, txt);
          });
        };

        const radioGroups = root.querySelectorAll('[role="radiogroup"]');
        if (radioGroups.length) {
          radioGroups.forEach((g) => {
            if (!root.contains(g)) return;
            runControls(g.querySelectorAll('[role="radio"]'));
          });
        } else {
          runControls(root.querySelectorAll('[role="radio"]'));
        }

        /*
         * Only scan checkboxes when this block has no choice radios (checkbox-style questions).
         * Random [role=group] + [role=checkbox] elsewhere in the card would otherwise pollute options.
         */
        const hasChoiceRadio = root.querySelector('[role="radiogroup"] [role="radio"], [role="radio"]');
        if (!hasChoiceRadio) {
          const cbGroups = root.querySelectorAll('[role="group"]');
          const checkboxes = root.querySelectorAll('[role="checkbox"]');
          if (cbGroups.length) {
            cbGroups.forEach((g) => {
              if (!root.contains(g)) return;
              runControls(g.querySelectorAll('[role="checkbox"]'));
            });
          } else {
            runControls(checkboxes);
          }
        }

        return out;
      };

      for (const root of roots) {
        if (!isVisible(root)) continue;

        const editables = Array.from(root.querySelectorAll('[contenteditable="true"]'))
          .filter(isVisible);
        const title = (editables[0] && editables[0].innerText) ? editables[0].innerText.trim() : '';

        if (!title && editables.length === 0) continue;

        const typeRaw = collectTypeLabel(root);
        const domHint = inferTypeFromDom(root);

        const options = [];
        for (let i = 1; i < editables.length; i++) {
          dedupePush(options, editables[i].innerText);
        }
        collectChoiceOptions(root, title).forEach((t) => dedupePush(options, t));

        if (!title && !typeRaw && options.length === 0 && !domHint) continue;

        results.push({
          question: title || '',
          typeLabel: typeRaw || '',
          domHint: domHint || '',
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
    wait_after_load_ms: int = 5_000,
    browser_locale: str = "vi-VN",
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
    browser_locale:
        Playwright context locale (e.g. ``vi-VN`` for Vietnamese type labels, ``en-US`` for English).

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
            # Match form language so type dropdown labels match our alias table (e.g. Vietnamese).
            context = browser.new_context(
                locale=browser_locale,
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
            # Long forms lazy-render items; scroll so every question block mounts in the DOM.
            for _ in range(8):
                page.evaluate(
                    "() => { window.scrollTo(0, document.body.scrollHeight); "
                    "document.documentElement.scrollTop = document.documentElement.scrollHeight; }"
                )
                time.sleep(0.35)

            raw_rows: list[dict[str, Any]] = page.evaluate(_extract_questions_js())

            for row in raw_rows:
                label = row.get("typeLabel") or ""
                dom_hint = row.get("domHint") or ""
                canonical = _canonical_from_hints(label, dom_hint)
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
    parser.add_argument(
        "--locale",
        default="vi-VN",
        metavar="TAG",
        help="Browser locale for Google Forms UI (default: vi-VN). Use en-US for English labels.",
    )
    args = parser.parse_args()

    try:
        payload = parse_google_form(
            args.edit_url,
            headless=not args.headed,
            browser_locale=args.locale,
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
