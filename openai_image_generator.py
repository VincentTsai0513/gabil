from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import mimetypes
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
import uuid


GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
EDITS_URL = "https://api.openai.com/v1/images/edits"
MIN_GPT_IMAGE_2_PIXELS = 655_360
MAX_GPT_IMAGE_2_PIXELS = 8_294_400
MAX_GPT_IMAGE_2_EDGE = 3_840
MAX_GPT_IMAGE_2_RATIO = 3


class OpenAIImageGenerationError(RuntimeError):
    pass


@dataclass
class GeneratedImage:
    image_bytes: bytes
    endpoint: str
    usage: dict[str, Any]


def resolve_openai_image_size(model: str, width: Any, height: Any) -> tuple[str, str]:
    requested_width = _positive_int(width)
    requested_height = _positive_int(height)

    if requested_width is None or requested_height is None:
        return "auto", "沒有有效的專案解析度，已改用 OpenAI auto 尺寸。"

    if model.strip().lower().startswith("gpt-image-2"):
        return _resolve_gpt_image_2_size(requested_width, requested_height)

    return _resolve_legacy_gpt_image_size(requested_width, requested_height)


def generate_openai_image(
    *,
    api_key: str,
    model: str,
    prompt: str,
    size: str,
    quality: str,
    reference_image: Path | None = None,
    timeout: int = 300,
) -> GeneratedImage:
    if not api_key.strip():
        raise OpenAIImageGenerationError("缺少 OpenAI API key。")

    cleaned_model = model.strip() or "gpt-image-2"
    cleaned_quality = quality.strip() or "medium"
    cleaned_size = size.strip() or "auto"

    if reference_image:
        payload = {
            "model": cleaned_model,
            "prompt": prompt,
            "size": cleaned_size,
            "quality": cleaned_quality,
            "output_format": "png",
        }
        response = _post_multipart(
            EDITS_URL,
            api_key,
            payload,
            [reference_image],
            timeout=timeout,
        )
        endpoint = "edits"
    else:
        payload = {
            "model": cleaned_model,
            "prompt": prompt,
            "size": cleaned_size,
            "quality": cleaned_quality,
            "output_format": "png",
            "n": 1,
        }
        response = _post_json(GENERATIONS_URL, api_key, payload, timeout=timeout)
        endpoint = "generations"

    return GeneratedImage(
        image_bytes=_decode_image_bytes(response),
        endpoint=endpoint,
        usage=response.get("usage") if isinstance(response.get("usage"), dict) else {},
    )


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None

    return number if number > 0 else None


def _resolve_gpt_image_2_size(width: int, height: int) -> tuple[str, str]:
    if max(width, height) / min(width, height) > MAX_GPT_IMAGE_2_RATIO:
        return (
            "auto",
            "專案解析度長短邊比例超過 3:1，已改用 OpenAI auto 尺寸。",
        )

    scaled_width = float(width)
    scaled_height = float(height)

    max_edge_scale = min(MAX_GPT_IMAGE_2_EDGE / scaled_width, MAX_GPT_IMAGE_2_EDGE / scaled_height, 1)
    scaled_width *= max_edge_scale
    scaled_height *= max_edge_scale

    pixels = scaled_width * scaled_height
    if pixels > MAX_GPT_IMAGE_2_PIXELS:
        scale = math.sqrt(MAX_GPT_IMAGE_2_PIXELS / pixels)
        scaled_width *= scale
        scaled_height *= scale
    elif pixels < MIN_GPT_IMAGE_2_PIXELS:
        scale = math.sqrt(MIN_GPT_IMAGE_2_PIXELS / pixels)
        scaled_width *= scale
        scaled_height *= scale

    resolved_width = _nearest_multiple(scaled_width, 16)
    resolved_height = _nearest_multiple(scaled_height, 16)

    if not _is_valid_gpt_image_2_size(resolved_width, resolved_height):
        resolved_width = _floor_multiple(scaled_width, 16)
        resolved_height = _floor_multiple(scaled_height, 16)

    if not _is_valid_gpt_image_2_size(resolved_width, resolved_height):
        return "auto", "專案解析度無法轉成 OpenAI 支援尺寸，已改用 auto。"

    resolved_size = f"{resolved_width}x{resolved_height}"
    requested_size = f"{width}x{height}"
    if resolved_size == requested_size:
        return resolved_size, ""

    return resolved_size, f"OpenAI 產圖尺寸使用 {resolved_size}，完成後會存成專案解析度 {requested_size}。"


def _resolve_legacy_gpt_image_size(width: int, height: int) -> tuple[str, str]:
    if width > height * 1.2:
        resolved_size = "1536x1024"
    elif height > width * 1.2:
        resolved_size = "1024x1536"
    else:
        resolved_size = "1024x1024"

    requested_size = f"{width}x{height}"
    if resolved_size == requested_size:
        return resolved_size, ""

    return resolved_size, f"此模型只支援固定尺寸，OpenAI 產圖尺寸使用 {resolved_size}，完成後會存成專案解析度 {requested_size}。"


def _nearest_multiple(value: float, multiple: int) -> int:
    lower = _floor_multiple(value, multiple)
    upper = lower + multiple

    if abs(value - upper) <= abs(value - lower):
        return upper

    return lower


def _floor_multiple(value: float, multiple: int) -> int:
    return max(multiple, int(value // multiple) * multiple)


def _is_valid_gpt_image_2_size(width: int, height: int) -> bool:
    pixels = width * height
    ratio = max(width, height) / min(width, height)

    return (
        width % 16 == 0
        and height % 16 == 0
        and max(width, height) <= MAX_GPT_IMAGE_2_EDGE
        and ratio <= MAX_GPT_IMAGE_2_RATIO
        and MIN_GPT_IMAGE_2_PIXELS <= pixels <= MAX_GPT_IMAGE_2_PIXELS
    )


def _post_json(url: str, api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    return _read_json_response(request, timeout)


def _post_multipart(
    url: str,
    api_key: str,
    fields: dict[str, Any],
    image_paths: list[Path],
    timeout: int,
) -> dict[str, Any]:
    boundary = f"----codex-image-boundary-{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields.items():
        _append_multipart_field(body, boundary, name, str(value))

    for image_path in image_paths:
        if not image_path.exists() or not image_path.is_file():
            raise OpenAIImageGenerationError(f"找不到參考圖片：{image_path}")
        _append_multipart_file(body, boundary, "image[]", image_path)

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    return _read_json_response(request, timeout)


def _append_multipart_field(body: bytearray, boundary: str, name: str, value: str) -> None:
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
    body.extend(value.encode("utf-8"))
    body.extend(b"\r\n")


def _append_multipart_file(body: bytearray, boundary: str, name: str, path: Path) -> None:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(path.read_bytes())
    body.extend(b"\r\n")


def _read_json_response(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise OpenAIImageGenerationError(_format_api_error(exc.code, raw_error)) from exc
    except urllib.error.URLError as exc:
        raise OpenAIImageGenerationError(f"無法連線到 OpenAI API：{exc.reason}") from exc

    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise OpenAIImageGenerationError("OpenAI API 回傳了無法解析的結果。") from exc

    if not isinstance(parsed, dict):
        raise OpenAIImageGenerationError("OpenAI API 回傳格式不是物件。")

    return parsed


def _format_api_error(status_code: int, raw_error: str) -> str:
    try:
        parsed = json.loads(raw_error)
    except json.JSONDecodeError:
        parsed = {}

    message = ""
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")

    return f"OpenAI API 錯誤 {status_code}: {message or raw_error}"


def _decode_image_bytes(response: dict[str, Any]) -> bytes:
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise OpenAIImageGenerationError("OpenAI API 沒有回傳圖片資料。")

    first_image = data[0]
    if not isinstance(first_image, dict):
        raise OpenAIImageGenerationError("OpenAI API 圖片資料格式不正確。")

    image_base64 = first_image.get("b64_json")
    if not isinstance(image_base64, str) or not image_base64:
        raise OpenAIImageGenerationError("OpenAI API 沒有回傳 base64 圖片。")

    try:
        return base64.b64decode(image_base64)
    except ValueError as exc:
        raise OpenAIImageGenerationError("OpenAI API 回傳的圖片 base64 無法解碼。") from exc
