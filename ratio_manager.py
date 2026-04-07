"""
ratio_manager.py
----------------
Utilities to convert ratio configurations into exact answer distributions.

Features:
- Validate ratio config per question type
- Radio: total ratio must equal 100%
- Checkbox: total ratio may exceed 100%
- Convert percentages to exact counts for a fixed number of submissions
- Return answer lists with randomized order
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from core import RatioError, get_logger


@dataclass(frozen=True)
class RatioOption:
    """Single option and its percentage in ratio config."""

    answer: str
    percentage: float


class RatioValidationError(RatioError):
    """Raised when ratio configuration is invalid."""


class RatioManager:
    """Manage ratio-based distributions for form answers."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._logger = get_logger("ratio_manager")

    @staticmethod
    def _normalize_question_type(question_type: str) -> str:
        q_type = (question_type or "").strip().lower()
        aliases = {
            "multiple_choice": "radio",
            "single_choice": "radio",
            "radio_button": "radio",
            "checkboxes": "checkbox",
        }
        return aliases.get(q_type, q_type)

    @staticmethod
    def _to_ratio_options(ratio_config: dict[str, float]) -> list[RatioOption]:
        """
        Convert dict config to RatioOption list and validate basic values.

        Expected format:
            {"A": 40, "B": 30, "C": 30}
        """
        if not isinstance(ratio_config, dict) or not ratio_config:
            raise RatioValidationError("ratio_config must be a non-empty dict of {answer: percentage}.")

        options: list[RatioOption] = []
        for answer, pct in ratio_config.items():
            if not isinstance(answer, str) or not answer.strip():
                raise RatioValidationError("Each ratio key (answer) must be a non-empty string.")
            try:
                pct_val = float(pct)
            except (TypeError, ValueError) as exc:
                raise RatioValidationError(f"Percentage for answer '{answer}' must be numeric.") from exc
            if pct_val < 0:
                raise RatioValidationError(f"Percentage for answer '{answer}' cannot be negative.")
            options.append(RatioOption(answer=answer.strip(), percentage=pct_val))
        return options

    def validate_ratio_config(self, question_type: str, ratio_config: dict[str, float]) -> None:
        """
        Validate ratio rules by question type.

        Rules:
        - radio: total percentage must equal 100%
        - checkbox: total percentage can be any non-negative value (including >100)
        """
        q_type = self._normalize_question_type(question_type)
        options = self._to_ratio_options(ratio_config)
        total = sum(opt.percentage for opt in options)

        if q_type == "radio":
            # Small tolerance for floating-point input like 33.3 + 33.3 + 33.4
            if abs(total - 100.0) > 1e-9:
                raise RatioValidationError(
                    f"Radio ratio total must equal 100, got {total:g}."
                )
        elif q_type == "checkbox":
            # Checkbox can exceed 100%; no upper bound here.
            if total < 0:
                raise RatioValidationError("Checkbox ratio total must be non-negative.")
        else:
            raise RatioValidationError(
                f"Unsupported question_type '{question_type}'. Use 'radio' or 'checkbox'."
            )

    @staticmethod
    def _largest_remainder_counts(total_submissions: int, percentages: list[float]) -> list[int]:
        """
        Convert percentages to integer counts that sum exactly to total_submissions.

        Uses the largest remainder method:
        1) take floor of each exact count
        2) distribute remaining slots to largest fractional remainders
        """
        exact = [(p / 100.0) * total_submissions for p in percentages]
        base = [int(x) for x in exact]
        remainder = total_submissions - sum(base)

        if remainder > 0:
            fractions = sorted(
                ((exact[i] - base[i], i) for i in range(len(percentages))),
                key=lambda item: item[0],
                reverse=True,
            )
            for _, idx in fractions[:remainder]:
                base[idx] += 1
        return base

    @staticmethod
    def _single_percentage_count(total_submissions: int, percentage: float) -> int:
        """
        Convert a single percentage to exact integer count for one option.
        """
        exact = (percentage / 100.0) * total_submissions
        lower = int(exact)
        return lower + 1 if (exact - lower) >= 0.5 else lower

    @staticmethod
    def _assert_radio_accuracy(
        answers: list[str],
        options: list[RatioOption],
        expected_counts: list[int],
    ) -> None:
        """Verify generated radio output exactly matches computed counts."""
        if len(answers) != sum(expected_counts):
            raise RatioValidationError("Radio distribution length mismatch.")
        for opt, expected in zip(options, expected_counts):
            actual = sum(1 for item in answers if item == opt.answer)
            if actual != expected:
                raise RatioValidationError(
                    f"Radio distribution mismatch for '{opt.answer}': expected {expected}, got {actual}."
                )

    @staticmethod
    def _assert_checkbox_accuracy(
        selections: list[list[str]],
        options: list[RatioOption],
        expected_counts: list[int],
    ) -> None:
        """Verify each checkbox option appears expected number of times."""
        flattened: list[str] = []
        for row in selections:
            flattened.extend(row)
        for opt, expected in zip(options, expected_counts):
            actual = sum(1 for item in flattened if item == opt.answer)
            if actual != expected:
                raise RatioValidationError(
                    f"Checkbox distribution mismatch for '{opt.answer}': expected {expected}, got {actual}."
                )

    def build_radio_distribution(
        self,
        total_submissions: int,
        ratio_config: dict[str, float],
        *,
        shuffle: bool = True,
    ) -> list[str]:
        """
        Build exact answer list for radio question.

        Example:
            total_submissions = 301
            ratio_config = {"A": 40, "B": 30, "C": 30}

        Returns a list with exact counts, e.g.:
            ["A", "B", "C", ...] length == 301
        """
        if total_submissions <= 0:
            raise RatioValidationError("total_submissions must be > 0.")
        self.validate_ratio_config("radio", ratio_config)

        options = self._to_ratio_options(ratio_config)
        counts = self._largest_remainder_counts(
            total_submissions,
            [opt.percentage for opt in options],
        )

        answers: list[str] = []
        for opt, count in zip(options, counts):
            answers.extend([opt.answer] * count)

        self._assert_radio_accuracy(answers, options, counts)
        if shuffle:
            self._rng.shuffle(answers)
        self._logger.info("Built radio distribution for %s submissions.", total_submissions)
        return answers

    def build_checkbox_distribution(
        self,
        total_submissions: int,
        ratio_config: dict[str, float],
        *,
        allow_empty: bool = True,
        shuffle: bool = True,
    ) -> list[list[str]]:
        """
        Build exact multi-select answer list for checkbox question.

        Interpretation:
        - Each option percentage is applied independently across submissions.
        - Because selections are independent, total percentages may exceed 100.
        - Output is list with length == total_submissions.
          Each item is list of selected options for one submission.
        """
        if total_submissions <= 0:
            raise RatioValidationError("total_submissions must be > 0.")
        self.validate_ratio_config("checkbox", ratio_config)

        options = self._to_ratio_options(ratio_config)
        per_option_counts = [
            min(total_submissions, self._single_percentage_count(total_submissions, opt.percentage))
            for opt in options
        ]

        # For each option, choose exact submission indices where it appears.
        selections: list[list[str]] = [[] for _ in range(total_submissions)]
        all_indices = list(range(total_submissions))
        for opt, count in zip(options, per_option_counts):
            picked_indices = self._rng.sample(all_indices, k=min(count, total_submissions))
            for idx in picked_indices:
                selections[idx].append(opt.answer)

        if not allow_empty:
            # Ensure every submission has at least one option selected.
            fallback = options[0].answer if options else ""
            for i in range(total_submissions):
                if not selections[i] and fallback:
                    selections[i].append(fallback)

        if shuffle:
            # Shuffle global order and per-row option order.
            self._rng.shuffle(selections)
            for row in selections:
                self._rng.shuffle(row)
        self._assert_checkbox_accuracy(selections, options, per_option_counts)
        self._logger.info("Built checkbox distribution for %s submissions.", total_submissions)
        return selections

    def build_distribution(
        self,
        question_type: str,
        total_submissions: int,
        ratio_config: dict[str, float],
        *,
        shuffle: bool = True,
        checkbox_allow_empty: bool = True,
    ) -> list[Any]:
        """
        Generic distribution builder by question type.

        Returns:
        - radio -> list[str]
        - checkbox -> list[list[str]]
        """
        q_type = self._normalize_question_type(question_type)
        if q_type == "radio":
            return self.build_radio_distribution(
                total_submissions,
                ratio_config,
                shuffle=shuffle,
            )
        if q_type == "checkbox":
            return self.build_checkbox_distribution(
                total_submissions,
                ratio_config,
                allow_empty=checkbox_allow_empty,
                shuffle=shuffle,
            )
        raise RatioValidationError(
            f"Unsupported question_type '{question_type}'. Use 'radio' or 'checkbox'."
        )


if __name__ == "__main__":
    # Quick demo with the user's example.
    manager = RatioManager()
    demo = manager.build_radio_distribution(
        total_submissions=301,
        ratio_config={"Option A": 40, "Option B": 30, "Option C": 30},
        shuffle=True,
    )
    print(f"Generated {len(demo)} answers.")
    print("First 20:", demo[:20])
