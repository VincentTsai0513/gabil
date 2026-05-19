from __future__ import annotations

from typing import Any

from image_tasks import DEFAULT_RESOLUTION, ImageTask


def build_prompt(task: ImageTask, project_name: str = "", resolution: str = DEFAULT_RESOLUTION) -> str:
    title = task.title.strip() or f"Image {task.index:02d}"
    style = task.style.strip() or "high quality, visually polished"
    content = task.content.strip() or "Create an image based on the provided concept."
    text_slogan = task.text_slogan.strip()
    reference = task.reference_image.strip()
    output_resolution = resolution.strip() or DEFAULT_RESOLUTION

    lines = [
        "Create one image for an AI image generation model.",
        f"Project: {project_name.strip() or 'Untitled project'}",
        f"Image number: {task.index:02d}",
        f"Title: {title}",
        f"Output size: {output_resolution}",
        "",
        "Main content:",
        content,
        "",
        "Visual style:",
        style,
        "",
        "Composition and quality requirements:",
        "- Produce a coherent, detailed, high-resolution image.",
        "- Use natural lighting, balanced composition, and clean visual hierarchy unless the style says otherwise.",
        "- If a product is visible, preserve its existing brand logo, model number, labels, screen text, packaging text, and product identity marks unless the task explicitly asks to change them.",
    ]

    if text_slogan:
        lines.extend(
            [
                "",
                "Marketing text / slogan editing:",
                f'- User provided slogan idea: "{text_slogan}"',
                "- Polish or lightly rewrite the slogan into concise, natural marketing copy that fits the image, product, language, and visual mood.",
                "- Integrate the final slogan as designed overlay typography or scene text so it feels native to the image.",
                "- This slogan instruction applies only to added or edited marketing text; do not remove, rewrite, blur, or replace existing product logos, brand names, model numbers, package labels, serial marks, buttons, screen labels, or other product text.",
                "- Do not add unrelated text, watermarks, or UI elements.",
            ]
        )
    else:
        lines.extend(
            [
                "- Do not add new slogans, captions, watermarks, UI text, or decorative lettering.",
                "- Empty slogan means no extra marketing text; it does not mean removing product logos, model numbers, labels, package text, or other existing product text.",
            ]
        )

    if reference:
        lines.extend(
            [
                "",
                "Reference image:",
                f"Use the uploaded reference image as visual guidance: {reference}",
                "Match only the intended visual traits from the reference; do not copy irrelevant background or artifacts.",
            ]
        )

    return "\n".join(lines)


def build_prompt_record(
    task: ImageTask,
    project_name: str = "",
    resolution: str = DEFAULT_RESOLUTION,
) -> dict[str, Any]:
    return {
        "index": task.index,
        "title": task.title.strip() or f"Image {task.index:02d}",
        "style": task.style.strip(),
        "content": task.content.strip(),
        "text_slogan": task.text_slogan.strip(),
        "resolution": resolution.strip() or DEFAULT_RESOLUTION,
        "reference_image": task.reference_image.strip(),
        "prompt": build_prompt(task, project_name, resolution),
    }


def build_all_prompt_records(
    tasks: list[ImageTask],
    project_name: str = "",
    resolution: str = DEFAULT_RESOLUTION,
) -> list[dict[str, Any]]:
    return [build_prompt_record(task, project_name, resolution) for task in tasks]
