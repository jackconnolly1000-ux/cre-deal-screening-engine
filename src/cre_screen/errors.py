from __future__ import annotations


class CreScreenError(Exception):
    """Base class for every custom exception in cre_screen."""


class ConfigError(CreScreenError):
    """A config file is missing, malformed, or contains an unknown key."""


class CacheMissError(CreScreenError):
    """Replay mode found no cached LLM response for this request."""

    def __init__(self, task: str, key: str) -> None:
        super().__init__(f"LLM cache miss in replay mode: task={task} key={key}")
        self.task = task
        self.key = key


class PromptTooLargeError(CreScreenError):
    """Estimated prompt size exceeds max_prompt_tokens."""


class BudgetExceededError(CreScreenError):
    """A live call would pass the per-run or lifetime spend cap."""


class UnsupportedFileError(CreScreenError):
    """The file extension or content type is not supported."""


class ParseError(CreScreenError):
    """A printed value could not be parsed."""


class MoneyParseError(ParseError):
    def __init__(self, text: str) -> None:
        super().__init__(f"cannot parse money value: {text!r}")
        self.text = text


class DateParseError(ParseError):
    def __init__(self, text: str) -> None:
        super().__init__(f"cannot parse date value: {text!r}")
        self.text = text


class NumberParseError(ParseError):
    def __init__(self, text: str) -> None:
        super().__init__(f"cannot parse number value: {text!r}")
        self.text = text


class HeaderMapError(CreScreenError):
    """Header mapping failed validation, even after the LLM retry."""


class LLMUnavailableError(CreScreenError):
    """A step needs the LLM but no client is available in this context."""
