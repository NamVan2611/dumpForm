"""
auto_suggest.py
---------------
Automatically generate ratio configurations for many form questions.

Goals:
- Radio question ratios always sum to 100
- Checkbox question ratios look realistic (totals may exceed 100)
- Support batch generation for many questions
- Add randomness while keeping outputs valid
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any
import os
import google.generativeai as genai

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("gemini-1.5-flash")

@dataclass(frozen=True)
class QuestionSpec:
    """Input model for one question."""

    question: str
    qtype: str  # expected: "radio" or "checkbox"
    options: list[str]


class AutoSuggestError(ValueError):
    """Raised for invalid auto-suggest input."""


class AutoSuggest:
    """Generate ratio configs automatically for form questions."""

    def __init__(self, *, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    @staticmethod
    def _normalize_qtype(qtype: str) -> str:
        raw = (qtype or "").strip().lower()
        aliases = {
            "multiple_choice": "radio",
            "single_choice": "radio",
            "checkboxes": "checkbox",
            "text": "paragraph",
            "short_answer": "paragraph",
            "paragraph": "paragraph"
        }
        return aliases.get(raw, raw)

    def _validate_spec(self, spec: QuestionSpec) -> None:
        if not spec.question.strip():
            raise AutoSuggestError("question must be non-empty.")
        qtype = self._normalize_qtype(spec.qtype)
        if qtype not in {"radio", "checkbox", "paragraph"}:
            raise AutoSuggestError(f"Unsupported qtype '{spec.qtype}' for question '{spec.question}'.")
        if qtype == "paragraph":
            return
        if not spec.options:
            raise AutoSuggestError(
                f"Question '{spec.question}' must have at least 1 option."
            )
        for opt in spec.options:
            if not isinstance(opt, str) or not opt.strip():
                raise AutoSuggestError(f"Question '{spec.question}' has invalid option value.")
        

    def _weights_to_percent(self, weights: list[float]) -> list[int]:
        """
        Convert raw positive weights to integer percentages summing to 100.
        Uses largest-remainder rounding for exact sum.
        """
        total = sum(weights)
        normalized = [(w / total) * 100.0 for w in weights]
        base = [int(x) for x in normalized]
        remain = 100 - sum(base)
        fractions = sorted(
            ((normalized[i] - base[i], i) for i in range(len(weights))),
            key=lambda x: x[0],
            reverse=True,
        )
        for _, idx in fractions[:remain]:
            base[idx] += 1
        return base

    def suggest_radio_ratios(self, options: list[str]) -> dict[str, int]:
        """
        Generate radio ratios with exact total 100.

        Strategy:
        - Sample random weights in a moderate range
        - Convert to exact integer percentages
        - Shuffle naturally by random sampling
        """
        if not options:
            raise AutoSuggestError("Radio question needs at least 1 option.")
        if len(options) == 1:
            return {options[0]: 100}

        # Slightly biased realistic spread (not too extreme most of the time).
        weights = [self._rng.uniform(0.6, 1.8) for _ in options]
        percentages = self._weights_to_percent(weights)
        return {opt: pct for opt, pct in zip(options, percentages)}

    def suggest_checkbox_ratios(
        self,
        options: list[str],
        *,
        min_select_avg: float = 1.2,
        max_select_avg: float = 2.6,
    ) -> dict[str, int]:
        """
        Generate checkbox ratios with realistic independent selection probabilities.

        Notes:
        - For checkbox, total ratio can be >100 because users may select multiple options.
        - We estimate an average selected-option count and distribute it across options.
        """
        n = len(options)
        if n < 1:
            raise AutoSuggestError("Checkbox question needs at least 1 option.")
        if n == 1:
            return {options[0]: 100}
        if min_select_avg <= 0 or max_select_avg <= 0 or min_select_avg > max_select_avg:
            raise AutoSuggestError("Invalid checkbox average selection bounds.")

        # Target average number of options selected per response.
        target_avg = self._rng.uniform(min_select_avg, min(max_select_avg, float(n)))
        target_total_percent = target_avg * 100.0

        # Build base preference weights with one or two "popular" options.
        weights = [self._rng.uniform(0.5, 1.4) for _ in options]
        popular_count = 1 if n < 4 else self._rng.choice([1, 2])
        popular_indices = self._rng.sample(range(n), k=popular_count)
        for idx in popular_indices:
            weights[idx] *= self._rng.uniform(1.3, 2.1)

        # Convert weights to raw percents that sum to target_total_percent.
        w_sum = sum(weights)
        raw = [(w / w_sum) * target_total_percent for w in weights]

        # Clamp each option to a realistic range [5, 95], then rebalance.
        clamped = [max(5.0, min(95.0, p)) for p in raw]
        c_sum = sum(clamped)
        if c_sum == 0:
            clamped = [100.0 / n for _ in options]
            c_sum = 100.0
        scale = target_total_percent / c_sum
        scaled = [max(1.0, min(99.0, p * scale)) for p in clamped]

        # Integer rounding, preserving near-target total.
        ints = [int(x) for x in scaled]
        desired_total = int(round(target_total_percent))
        remain = desired_total - sum(ints)
        fractions = sorted(
            ((scaled[i] - ints[i], i) for i in range(n)),
            key=lambda x: x[0],
            reverse=True,
        )
        if remain > 0:
            for _, idx in fractions[:remain]:
                ints[idx] += 1
        elif remain < 0:
            for _, idx in reversed(fractions[: abs(remain)]):
                if ints[idx] > 1:
                    ints[idx] -= 1

        return {opt: pct for opt, pct in zip(options, ints)}

    def suggest_for_question(self, spec: QuestionSpec):
        self._validate_spec(spec)
        qtype = self._normalize_qtype(spec.qtype)

        if qtype == "paragraph":
            text = self.suggest_paragraph_ai(spec.question)
            return {
                "question": spec.question,
                "type": "paragraph",
                "answer": text,
            }

        elif qtype == "radio":
            ratio = self.suggest_radio_ratios(spec.options)

        else:
            ratio = self.suggest_checkbox_ratios(spec.options)

        return {
            "question": spec.question,
            "type": qtype,
            "ratio": ratio,
        }
            
    def suggest_paragraph_ai(self, question: str) -> str:
        try:
            prompt = f"""
                Bạn là người trả lời khảo sát.
                Hãy trả lời ngắn gọn 1-2 câu tiếng Việt.
                Câu hỏi: {question}
                """

            response = model.generate_content(prompt)

            text = response.text.strip()
            return text

        except Exception:
            return "Tôi không có ý kiến cụ thể, nhưng nhìn chung mọi thứ đều ổn."
    
    def suggest_many(self, questions: list[dict[str, Any] | QuestionSpec]) -> dict[str, dict[str, Any]]:
        """
        Generate ratio configs for many questions.

        Input per question can be:
        - QuestionSpec(...)
        - {"question": "...", "type": "radio|checkbox", "options": [...]}

        Return format:
        {
          "<question text>": {
            "type": "radio|checkbox",
            "ratio": {"Option A": 40, "Option B": 30, ...}
          },
          ...
        }
        """
        if not isinstance(questions, list) or not questions:
            raise AutoSuggestError("questions must be a non-empty list.")

        output: dict[str, dict[str, Any]] = {}
        for item in questions:
            if isinstance(item, QuestionSpec):
                spec = item
            elif isinstance(item, dict):
                spec = QuestionSpec(
                    question=str(item.get("question", "")),
                    qtype=str(item.get("type", "")),
                    options=list(item.get("options", [])),
                )
            else:
                raise AutoSuggestError("Each question must be a dict or QuestionSpec.")

            generated = self.suggest_for_question(spec)
            if generated["type"] == "paragraph":
                output[generated["question"]] = {
                    "type": "paragraph",
                    "answer": generated["answer"],
                }
            else:
                output[generated["question"]] = {
                    "type": generated["type"],
                    "ratio": generated["ratio"],
                }
        return output


if __name__ == "__main__":
    # Demo usage
    sample_questions = [
        {
            "question": "Bạn có thường mua đồ handmade không?",
            "type": "radio",
            "options": ["Có", "Không", "Thỉnh thoảng"],
        },
        {
            "question": "Bạn quan tâm điều gì khi mua đồ handmade?",
            "type": "checkbox",
            "options": ["Giá", "Thiết kế", "Chất lượng", "Độc đáo", "Thương hiệu"],
        },
    ]

    suggest = AutoSuggest(seed=42)
    result = suggest.suggest_many(sample_questions)
    print(result)
