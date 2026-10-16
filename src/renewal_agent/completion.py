"""Original ChatCompletion JSON authority, before SDK/schema normalization."""

from .source import MAX_INTEGER, draft, parse_json


def native_identifier(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 240
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError("original bounded completion identifier required")
    return value


def completed(raw):
    if type(raw) is not bytes or len(raw) > 1024 * 1024:
        raise ValueError("bounded complete original completion bytes required")
    value = parse_json(raw)
    if (
        type(value) is not dict
        or value.get("object") != "chat.completion"
        or value.get("error") is not None
    ):
        raise ValueError("original ChatCompletion object required")
    native_identifier(value.get("id"))
    native_identifier(value.get("model"))
    if (
        type(value.get("created")) is not int
        or not 0 <= value["created"] <= MAX_INTEGER
    ):
        raise ValueError("original completion creation value required")
    choices = value.get("choices")
    if type(choices) is not list or len(choices) != 1:
        raise ValueError("exactly one original completed choice required")
    choice = choices[0]
    if (
        type(choice) is not dict
        or type(choice.get("index")) is not int
        or choice["index"] != 0
        or choice.get("finish_reason") != "stop"
    ):
        raise ValueError("original completion finish_reason must explicitly be stop")
    message = choice.get("message")
    if (
        type(message) is not dict
        or message.get("role") != "assistant"
        or type(message.get("content")) is not str
        or message.get("refusal") is not None
        or message.get("tool_calls") is not None
        or message.get("function_call") is not None
    ):
        raise ValueError("original assistant draft is refused/incomplete/unsupported")
    parsed = draft(parse_json(message["content"].encode("utf-8", errors="strict")))
    # Unknown original fields are retained by the caller and never grant authority.
    return value, parsed
