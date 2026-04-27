# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import re
from typing import Any
from urllib.parse import urlparse

import requests

_OLLAMA_TAG_CACHE: dict[str, set[str]] = {}


def is_qwen_model(model: str) -> bool:
    return "qwen" in str(model or "").lower()


def is_local_ollama_endpoint(endpoint: str) -> bool:
    parsed = urlparse(str(endpoint or ""))
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port == 11434


def available_ollama_models(endpoint: str, timeout: int = 5) -> set[str]:
    if not is_local_ollama_endpoint(endpoint):
        return set()
    parsed = urlparse(endpoint)
    cache_key = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    if cache_key in _OLLAMA_TAG_CACHE:
        return set(_OLLAMA_TAG_CACHE[cache_key])

    tags_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}/api/tags"
    response = requests.get(tags_url, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    names = {
        str(item.get("name") or "").strip()
        for item in (body.get("models") or [])
        if str(item.get("name") or "").strip()
    }
    _OLLAMA_TAG_CACHE[cache_key] = set(names)
    return names


def resolve_task_model(task: str, endpoint: str, model: str) -> str:
    model = str(model or "").strip()
    if not model:
        return model
    if task != "polish" or not is_local_ollama_endpoint(endpoint):
        return model

    family = model.split(":", 1)[0].lower()
    available = available_ollama_models(endpoint)
    if not available:
        return model

    preferred_lighter: list[str] = []
    if family == "qwen3.5":
        preferred_lighter = ["qwen3.5:27b", "qwen3.5:9b", "qwen3.5:latest"]
    elif family == "qwen3":
        preferred_lighter = ["qwen3:14b", "qwen3:8b", "qwen3:4b"]

    for candidate in preferred_lighter:
        if candidate in available and candidate != model:
            return candidate
    return model


def render_json_user_message(payload: dict[str, Any], model: str) -> str:
    prefix = []
    if is_qwen_model(model):
        prefix.append("/no_think")
    prefix.extend(
        [
            "Return exactly one JSON object.",
            "Do not use markdown, code fences, or explanatory text.",
            "Do not repeat the payload.",
            "Payload:",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        ]
    )
    return "\n".join(prefix)


def parse_json_response_content(content: str) -> dict[str, Any]:
    cleaned = str(content or "").strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if not cleaned:
        raise ValueError("Model returned empty content")

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = json.loads(_extract_first_json_object(cleaned))

    if not isinstance(parsed, dict):
        raise ValueError("Model response was not a JSON object")
    return parsed


def request_structured_json(
    payload: dict[str, Any],
    endpoint: str,
    model: str,
    system_prompt: str,
    api_key: str = "",
    timeout: int = 20,
    max_tokens: int = 400,
) -> dict[str, Any]:
    if is_local_ollama_endpoint(endpoint):
        try:
            return _request_structured_json_ollama_native(
                payload,
                endpoint,
                model,
                system_prompt,
                timeout=timeout,
                max_tokens=max_tokens,
            )
        except Exception:
            pass

    prompt = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": render_json_user_message(payload, model),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.post(
        endpoint.rstrip("/") + "/chat/completions",
        json=prompt,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    return parse_json_response_content(content)


def _request_structured_json_ollama_native(
    payload: dict[str, Any],
    endpoint: str,
    model: str,
    system_prompt: str,
    timeout: int = 20,
    max_tokens: int = 400,
) -> dict[str, Any]:
    parsed = urlparse(endpoint)
    native_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}/api/chat"
    prompt = {
        "model": model,
        "think": False,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
            "num_predict": max_tokens,
        },
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": render_json_user_message(payload, model),
            },
        ],
    }
    response = requests.post(native_url, json=prompt, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    content = ((body.get("message") or {}).get("content") or "").strip()
    return parse_json_response_content(content)


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model response")

    depth = 0
    in_string = False
    escaped = False
    for idx, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    raise ValueError("Unterminated JSON object in model response")
