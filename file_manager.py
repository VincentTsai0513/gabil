from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from image_tasks import DEFAULT_HEIGHT, DEFAULT_RESOLUTION, DEFAULT_WIDTH, ImageTask


PROJECTS_DIR = Path("projects")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
INVALID_FILENAME_CHARS = r'[<>:"/\\|?*\x00-\x1f]'
DEFAULT_STYLE_OPTIONS = [
    "寫實攝影風",
    "商品攝影",
    "電影感",
    "日系插畫",
    "3D render",
    "水彩插畫",
]
PRODUCT_REFERENCE_DEFAULTS = {
    "product_features": "",
    "product_specifications": "",
    "product_contents": "",
}


def normalize_dimension(value: Any, fallback: str) -> str:
    match = re.search(r"\d+", str(value or ""))
    return match.group(0) if match else fallback


def format_resolution(width: Any, height: Any) -> str:
    clean_width = normalize_dimension(width, DEFAULT_WIDTH)
    clean_height = normalize_dimension(height, DEFAULT_HEIGHT)

    return f"{clean_width}px x {clean_height}px"


def split_resolution(resolution: Any) -> tuple[str, str]:
    values = re.findall(r"\d+", str(resolution or ""))

    if len(values) >= 2:
        return values[0], values[1]

    return DEFAULT_WIDTH, DEFAULT_HEIGHT


def sanitize_filename(value: str, fallback: str = "untitled", max_length: int = 80) -> str:
    name = re.sub(INVALID_FILENAME_CHARS, "_", value.strip())
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._ ")

    if not name:
        name = fallback

    return name[:max_length].strip("._ ") or fallback


def sanitize_project_name(project_name: str) -> str:
    return sanitize_filename(project_name, fallback="untitled_project", max_length=120)


def get_project_paths(project_name: str, base_dir: Path = PROJECTS_DIR) -> dict[str, Path]:
    safe_name = sanitize_project_name(project_name)
    root = base_dir / safe_name

    return {
        "root": root,
        "jpg": root / "JPG",
        "input": root / "input",
        "output": root / "output",
        "prompts": root / "prompts",
    }


def _format_modified_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    return f"{size / (1024 * 1024):.1f} MB"


def _latest_project_timestamp(project_dir: Path) -> float:
    timestamps = [project_dir.stat().st_mtime]

    for child in project_dir.rglob("*"):
        try:
            timestamps.append(child.stat().st_mtime)
        except OSError:
            continue

    return max(timestamps)


def _count_direct_files(directory: Path) -> int:
    if not directory.exists():
        return 0

    return sum(1 for path in directory.iterdir() if path.is_file() and not path.name.startswith("."))


def list_existing_projects(base_dir: Path = PROJECTS_DIR) -> list[dict[str, Any]]:
    if not base_dir.exists():
        return []

    projects: list[dict[str, Any]] = []

    for project_dir in base_dir.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue

        paths = get_project_paths(project_dir.name, base_dir)
        modified_timestamp = _latest_project_timestamp(project_dir)
        projects.append(
            {
                "name": project_dir.name,
                "path": str(project_dir),
                "jpg_count": _count_direct_files(paths["jpg"]),
                "input_count": _count_direct_files(paths["input"]),
                "output_count": _count_direct_files(paths["output"]),
                "prompt_count": _count_direct_files(paths["prompts"]),
                "modified_timestamp": modified_timestamp,
                "modified_at": _format_modified_time(modified_timestamp),
            }
        )

    return sorted(projects, key=lambda project: project["modified_timestamp"], reverse=True)


def list_folder_files(directory: Path) -> list[dict[str, str]]:
    if not directory.exists():
        return []

    files: list[dict[str, str]] = []

    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if path.name.startswith("."):
            continue

        try:
            stat = path.stat()
        except OSError:
            continue

        files.append(
            {
                "檔名": f"{path.name}/" if path.is_dir() else path.name,
                "類型": "資料夾" if path.is_dir() else path.suffix.lower().lstrip(".") or "檔案",
                "大小": "-" if path.is_dir() else _format_file_size(stat.st_size),
                "修改時間": _format_modified_time(stat.st_mtime),
            }
        )

    return files


def list_image_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []

    return sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


def find_task_reference_image(
    reference_dir: Path,
    task: ImageTask,
    fallback_dirs: list[Path] | None = None,
) -> Path | None:
    reference_name = Path(task.reference_image).name if task.reference_image.strip() else ""
    search_dirs = [reference_dir, *(fallback_dirs or [])]

    if reference_name:
        for directory in search_dirs:
            reference_path = directory / reference_name
            if reference_path.exists() and reference_path.is_file():
                return reference_path

    index_prefix = f"{task.index:02d}_"
    for directory in search_dirs:
        for image_path in list_image_files(directory):
            if image_path.name.startswith(index_prefix):
                return image_path

    return None


def load_prompt_text(prompts_dir: Path) -> str:
    prompts_path = prompts_dir / "prompts.txt"

    if not prompts_path.exists():
        return ""

    return prompts_path.read_text(encoding="utf-8")


def ensure_project_dirs(project_name: str, base_dir: Path = PROJECTS_DIR) -> dict[str, Path]:
    paths = get_project_paths(project_name, base_dir)

    for key in ("root", "input", "output", "prompts"):
        paths[key].mkdir(parents=True, exist_ok=True)

    return paths


def _safe_project_root(project_name: str, base_dir: Path = PROJECTS_DIR) -> Path:
    base_root = base_dir.resolve()
    project_root = get_project_paths(project_name, base_dir)["root"].resolve(strict=False)

    if project_root == base_root or base_root not in project_root.parents:
        raise ValueError("專案路徑不在 projects 資料夾內，已停止操作。")

    return project_root


def delete_project_folder(project_name: str, base_dir: Path = PROJECTS_DIR) -> Path:
    project_root = _safe_project_root(project_name, base_dir)

    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(f"找不到專案資料夾：{project_root}")

    shutil.rmtree(project_root)
    return project_root


def move_project_folder(project_name: str, destination_dir: str, base_dir: Path = PROJECTS_DIR) -> Path:
    project_root = _safe_project_root(project_name, base_dir)

    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(f"找不到專案資料夾：{project_root}")

    destination_parent = Path(destination_dir).expanduser()
    if not destination_parent.exists() or not destination_parent.is_dir():
        raise FileNotFoundError(f"目的地資料夾不存在：{destination_parent}")

    resolved_project_root = project_root.resolve()
    resolved_destination_parent = destination_parent.resolve()
    if resolved_destination_parent == resolved_project_root or resolved_project_root in resolved_destination_parent.parents:
        raise ValueError("不能把資料夾搬到自己裡面。")

    target_path = destination_parent / project_root.name
    if target_path.exists():
        raise FileExistsError(f"目的地已經有同名資料夾：{target_path}")

    moved_path = shutil.move(str(project_root), str(target_path))
    return Path(moved_path)


def save_uploaded_file(uploaded_file: Any, input_dir: Path, task: ImageTask) -> Path:
    original_name = Path(uploaded_file.name)
    extension = original_name.suffix.lower() or ".png"
    title = sanitize_filename(task.title, fallback=f"image_{task.index:02d}")
    target_path = input_dir / f"{task.index:02d}_{title}{extension}"

    with target_path.open("wb") as output_file:
        output_file.write(uploaded_file.getbuffer())

    return target_path


def unique_file_path(directory: Path, filename: str) -> Path:
    original_name = Path(filename)
    extension = original_name.suffix.lower()
    stem = sanitize_filename(original_name.stem, fallback="image")
    target_path = directory / f"{stem}{extension}"
    counter = 2

    while target_path.exists():
        target_path = directory / f"{stem}_{counter}{extension}"
        counter += 1

    return target_path


def save_original_uploaded_file(uploaded_file: Any, jpg_dir: Path) -> Path:
    original_name = Path(uploaded_file.name)
    extension = original_name.suffix.lower() or ".jpg"
    jpg_dir.mkdir(parents=True, exist_ok=True)
    target_path = unique_file_path(jpg_dir, f"{original_name.stem}{extension}")

    with target_path.open("wb") as output_file:
        output_file.write(uploaded_file.getbuffer())

    return target_path


def write_prompt_files(
    records: list[dict[str, Any]],
    prompts_dir: Path,
    txt_filename: str = "prompts.txt",
    json_filename: str = "prompts.json",
) -> tuple[Path, Path]:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    txt_path = prompts_dir / txt_filename
    json_path = prompts_dir / json_filename

    with txt_path.open("w", encoding="utf-8") as txt_file:
        for record in records:
            txt_file.write(f"===== {int(record['index']):02d}. {record['title']} =====\n")
            txt_file.write(str(record["prompt"]).strip())
            txt_file.write("\n\n")

    with json_path.open("w", encoding="utf-8") as json_file:
        json.dump(records, json_file, ensure_ascii=False, indent=2)

    return txt_path, json_path


def save_project_tasks(tasks: list[ImageTask], prompts_dir: Path) -> Path:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    tasks_path = prompts_dir / "tasks.json"

    with tasks_path.open("w", encoding="utf-8") as task_file:
        json.dump([task.to_dict() for task in tasks], task_file, ensure_ascii=False, indent=2)

    return tasks_path


def normalize_product_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return dict(PRODUCT_REFERENCE_DEFAULTS)

    return {
        key: str(value.get(key, "") or "").strip()
        for key in PRODUCT_REFERENCE_DEFAULTS
    }


def save_project_config(
    prompts_dir: Path,
    width: Any,
    height: Any,
    product_reference: dict[str, Any] | None = None,
) -> Path:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    config_path = prompts_dir / "project_config.json"
    clean_width = normalize_dimension(width, DEFAULT_WIDTH)
    clean_height = normalize_dimension(height, DEFAULT_HEIGHT)
    clean_product_reference = normalize_product_reference(product_reference)

    with config_path.open("w", encoding="utf-8") as config_file:
        json.dump(
            {
                "resolution_width": clean_width,
                "resolution_height": clean_height,
                "resolution": format_resolution(clean_width, clean_height),
                **clean_product_reference,
            },
            config_file,
            ensure_ascii=False,
            indent=2,
        )

    return config_path


def load_project_config(prompts_dir: Path) -> dict[str, str]:
    config_path = prompts_dir / "project_config.json"

    if not config_path.exists():
        return {
            "resolution_width": DEFAULT_WIDTH,
            "resolution_height": DEFAULT_HEIGHT,
            "resolution": DEFAULT_RESOLUTION,
            **PRODUCT_REFERENCE_DEFAULTS,
        }

    with config_path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)

    if not isinstance(data, dict):
        return {
            "resolution_width": DEFAULT_WIDTH,
            "resolution_height": DEFAULT_HEIGHT,
            "resolution": DEFAULT_RESOLUTION,
            **PRODUCT_REFERENCE_DEFAULTS,
        }

    width = data.get("resolution_width")
    height = data.get("resolution_height")

    if not width or not height:
        width, height = split_resolution(data.get("resolution"))

    clean_width = normalize_dimension(width, DEFAULT_WIDTH)
    clean_height = normalize_dimension(height, DEFAULT_HEIGHT)

    return {
        "resolution_width": clean_width,
        "resolution_height": clean_height,
        "resolution": format_resolution(clean_width, clean_height),
        **normalize_product_reference(data),
    }


def load_project_tasks(prompts_dir: Path) -> list[dict[str, Any]]:
    tasks_path = prompts_dir / "tasks.json"

    if not tasks_path.exists():
        return []

    with tasks_path.open("r", encoding="utf-8") as task_file:
        data = json.load(task_file)

    return data if isinstance(data, list) else []


def _style_history_path(base_dir: Path = PROJECTS_DIR) -> Path:
    return base_dir / "style_history.json"


def load_style_history(base_dir: Path = PROJECTS_DIR) -> list[str]:
    path = _style_history_path(base_dir)
    styles = list(DEFAULT_STYLE_OPTIONS)

    if path.exists():
        with path.open("r", encoding="utf-8") as style_file:
            data = json.load(style_file)
        if isinstance(data, list):
            styles.extend(str(item) for item in data if str(item).strip())

    return list(dict.fromkeys(style.strip() for style in styles if style.strip()))


def save_style_history(styles: list[str], base_dir: Path = PROJECTS_DIR) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = _style_history_path(base_dir)
    cleaned = list(dict.fromkeys(style.strip() for style in styles if style.strip()))

    with path.open("w", encoding="utf-8") as style_file:
        json.dump(cleaned, style_file, ensure_ascii=False, indent=2)

    return path


def update_style_history(styles: list[str], base_dir: Path = PROJECTS_DIR) -> list[str]:
    current = load_style_history(base_dir)
    merged = list(dict.fromkeys(current + [style.strip() for style in styles if style.strip()]))
    save_style_history(merged, base_dir)
    return merged


def list_output_images(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []

    return sorted(
        [
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


def output_task_index(path: Path) -> int | None:
    match = re.match(r"^(\d{2})_", path.name)
    if not match:
        return None

    return int(match.group(1))


def find_task_output_image(output_dir: Path, task: ImageTask) -> Path | None:
    for image_path in list_output_images(output_dir):
        if output_task_index(image_path) == task.index:
            return image_path

    return None


def list_missing_output_tasks(output_dir: Path, tasks: list[ImageTask]) -> list[ImageTask]:
    occupied_indexes = {
        task_index
        for image_path in list_output_images(output_dir)
        if (task_index := output_task_index(image_path)) is not None
    }

    return [task for task in tasks if task.index not in occupied_indexes]


def _convert_or_move_to_png(source: Path, target: Path) -> None:
    if source.suffix.lower() == ".png":
        shutil.move(str(source), str(target))
        return

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("整理非 PNG 圖片需要 Pillow，請先執行 pip install -r requirements.txt。") from exc

    with Image.open(source) as image:
        image.save(target, format="PNG")

    source.unlink()


def next_organized_output_path(output_dir: Path, task: ImageTask) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    title = sanitize_filename(task.title, fallback=f"image_{task.index:02d}")
    target_path = output_dir / f"{task.index:02d}_{title}.png"

    if target_path.exists():
        target_path = output_dir / f"{task.index:02d}_{title}_{uuid.uuid4().hex[:6]}.png"

    return target_path


def save_generated_png(
    image_bytes: bytes,
    target_path: Path,
    width: Any,
    height: Any,
) -> Path:
    clean_width = int(normalize_dimension(width, DEFAULT_WIDTH))
    clean_height = int(normalize_dimension(height, DEFAULT_HEIGHT))

    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("儲存生成圖片需要 Pillow，請先執行 pip install -r requirements.txt。") from exc

    with Image.open(BytesIO(image_bytes)) as image:
        output_image = image.convert("RGBA") if image.mode in {"P", "LA"} else image.copy()

    if output_image.size != (clean_width, clean_height):
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        output_image = output_image.resize((clean_width, clean_height), resampling)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    output_image.save(target_path, format="PNG")
    return target_path


def organize_output_images(output_dir: Path, tasks: list[ImageTask]) -> list[dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    task_indexes = {task.index for task in tasks}
    occupied_indexes: set[int] = set()
    unorganized_images: list[Path] = []

    for image in list_output_images(output_dir):
        task_index = output_task_index(image)
        if task_index in task_indexes and task_index not in occupied_indexes:
            occupied_indexes.add(task_index)
        else:
            unorganized_images.append(image)

    missing_tasks = [task for task in tasks if task.index not in occupied_indexes]
    images = unorganized_images[: len(missing_tasks)]
    temp_files: list[tuple[str, Path]] = []
    result: list[dict[str, str]] = []

    for image in images:
        temp_path = output_dir / f".__organizing_{uuid.uuid4().hex}{image.suffix.lower()}"
        original_name = image.name
        image.rename(temp_path)
        temp_files.append((original_name, temp_path))

    for task, (original_name, temp_path) in zip(missing_tasks, temp_files):
        target_path = next_organized_output_path(output_dir, task)

        _convert_or_move_to_png(temp_path, target_path)
        result.append({"old_name": original_name, "new_name": target_path.name})

    return result
