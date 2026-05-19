from __future__ import annotations

import base64
import hashlib
from html import escape
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

import streamlit as st

from file_manager import (
    delete_project_folder,
    ensure_project_dirs,
    find_task_reference_image,
    find_task_output_image,
    format_resolution,
    list_missing_output_tasks,
    list_existing_projects,
    list_folder_files,
    load_project_config,
    load_prompt_text,
    load_project_tasks,
    load_style_history,
    move_project_folder,
    normalize_dimension,
    next_organized_output_path,
    save_project_config,
    save_project_tasks,
    save_generated_png,
    save_original_uploaded_file,
    sanitize_filename,
    sanitize_project_name,
    update_style_history,
    unique_file_path,
    write_prompt_files,
)
from chatgpt_web_generator import (
    CHATGPT_PROFILE_DIR,
    ChatGPTWebAutomationError,
    ChatGPTWebSession,
    default_chrome_user_data_dir,
    infer_chrome_profile_name,
)
from image_tasks import DEFAULT_HEIGHT, DEFAULT_WIDTH, TASK_COUNT, ImageTask, create_default_tasks, normalize_tasks
from prompt_builder import build_all_prompt_records, build_prompt
from product_document import build_product_document_prompt, write_product_document


st.set_page_config(page_title="AI 圖片批次 Prompt 管理器", layout="wide")

CUSTOM_STYLE_OPTION = "自訂 / 手動輸入"
MOVE_FOLDER_LIST_LIMIT = 48
DEFAULT_CHATGPT_TIMEOUT_SECONDS = 420
CHATGPT_BROWSER_PROFILE_CHROME = "使用平常 Chrome 登入狀態"
CHATGPT_BROWSER_PROFILE_TOOL = "使用工具專用 Chrome profile"
PRODUCT_REFERENCE_FIELDS = {
    "product_features": "產品特色",
    "product_specifications": "產品規格",
    "product_contents": "產品內容",
}


def init_state() -> None:
    if "current_page" not in st.session_state:
        st.session_state.current_page = "home"
    if "project_name" not in st.session_state:
        st.session_state.project_name = ""
    if "active_project" not in st.session_state:
        st.session_state.active_project = ""
    if "resolution_width" not in st.session_state:
        st.session_state.resolution_width = int(DEFAULT_WIDTH)
    if "resolution_height" not in st.session_state:
        st.session_state.resolution_height = int(DEFAULT_HEIGHT)
    for key in PRODUCT_REFERENCE_FIELDS:
        if key not in st.session_state:
            st.session_state[key] = ""
    if "style_history" not in st.session_state:
        st.session_state.style_history = load_style_history()
    if "tasks" not in st.session_state:
        st.session_state.tasks = [task.to_dict() for task in create_default_tasks()]
    if "chatgpt_timeout_seconds" not in st.session_state:
        configured_timeout = os.getenv("CHATGPT_WEB_TIMEOUT_SECONDS", str(DEFAULT_CHATGPT_TIMEOUT_SECONDS))
        st.session_state.chatgpt_timeout_seconds = int(configured_timeout) if configured_timeout.isdigit() else DEFAULT_CHATGPT_TIMEOUT_SECONDS
    st.session_state.chatgpt_timeout_seconds = min(
        1200,
        max(60, int(st.session_state.chatgpt_timeout_seconds)),
    )
    default_chrome_dir = default_chrome_user_data_dir()
    if "chatgpt_browser_profile_mode" not in st.session_state:
        configured_mode = os.getenv("CHATGPT_BROWSER_PROFILE_MODE", "chrome").strip().lower()
        st.session_state.chatgpt_browser_profile_mode = (
            CHATGPT_BROWSER_PROFILE_TOOL
            if configured_mode in {"tool", "dedicated", "private"}
            else CHATGPT_BROWSER_PROFILE_CHROME
        )
    if "chatgpt_chrome_user_data_dir" not in st.session_state:
        st.session_state.chatgpt_chrome_user_data_dir = str(default_chrome_dir)
    if "chatgpt_chrome_profile_name" not in st.session_state:
        configured_profile = os.getenv("CHATGPT_CHROME_PROFILE_NAME", "").strip()
        st.session_state.chatgpt_chrome_profile_name = configured_profile or infer_chrome_profile_name(default_chrome_dir)
    if "pending_project_action" not in st.session_state:
        st.session_state.pending_project_action = ""
    if "pending_project_name" not in st.session_state:
        st.session_state.pending_project_name = ""
    if "home_notice" not in st.session_state:
        st.session_state.home_notice = None
    if "project_notice" not in st.session_state:
        st.session_state.project_notice = None
    if "active_task_index" not in st.session_state:
        st.session_state.active_task_index = 1


def style_from_widgets(task_index: int, fallback: str = "") -> str:
    style_value = st.session_state.get(f"task_{task_index}_style")

    if style_value is not None:
        return str(style_value)

    custom_value = st.session_state.get(f"task_{task_index}_style_custom")
    if custom_value:
        return str(custom_value)

    choice = st.session_state.get(f"task_{task_index}_style_choice", CUSTOM_STYLE_OPTION)

    if choice != CUSTOM_STYLE_OPTION:
        return str(choice)

    return str(st.session_state.get(f"task_{task_index}_style_custom", fallback))


def get_tasks_from_widgets() -> list[ImageTask]:
    tasks: list[ImageTask] = []

    for i in range(1, TASK_COUNT + 1):
        existing = ImageTask.from_dict(st.session_state.tasks[i - 1])
        tasks.append(
            ImageTask(
                index=i,
                title=st.session_state.get(f"task_{i}_title", existing.title),
                style=style_from_widgets(i, existing.style),
                content=st.session_state.get(f"task_{i}_content", existing.content),
                text_slogan=st.session_state.get(f"task_{i}_text_slogan", existing.text_slogan),
                reference_image=st.session_state.get(f"task_{i}_reference_image", existing.reference_image),
            )
        )

    return tasks


def sync_tasks_to_state(tasks: list[ImageTask]) -> None:
    st.session_state.tasks = [task.to_dict() for task in tasks]


def mark_task_active(task_index: int) -> None:
    st.session_state.active_task_index = min(TASK_COUNT, max(1, int(task_index)))


def get_active_task_index() -> int:
    try:
        return min(TASK_COUNT, max(1, int(st.session_state.get("active_task_index", 1))))
    except (TypeError, ValueError):
        return 1


def open_project(project_name: str) -> None:
    paths = ensure_project_dirs(project_name)
    saved_tasks = load_project_tasks(paths["prompts"])
    config = load_project_config(paths["prompts"])
    style_history = load_style_history()
    tasks = normalize_tasks(saved_tasks, count=TASK_COUNT)

    st.session_state.project_name = project_name
    st.session_state.active_project = project_name
    st.session_state.resolution_width = int(config["resolution_width"])
    st.session_state.resolution_height = int(config["resolution_height"])
    st.session_state.active_task_index = 1
    for key in PRODUCT_REFERENCE_FIELDS:
        st.session_state[key] = str(config.get(key, "") or "")
    st.session_state.style_history = style_history
    st.session_state.tasks = [task.to_dict() for task in tasks]

    for task in tasks:
        st.session_state[f"task_{task.index}_title"] = task.title
        st.session_state[f"task_{task.index}_style"] = task.style
        st.session_state.pop(f"task_{task.index}_style_choice", None)
        st.session_state.pop(f"task_{task.index}_style_custom", None)
        st.session_state.pop(f"task_{task.index}_reference_picker", None)
        st.session_state[f"task_{task.index}_content"] = task.content
        st.session_state[f"task_{task.index}_text_slogan"] = task.text_slogan
        st.session_state[f"task_{task.index}_reference_image"] = task.reference_image


def navigate_home() -> None:
    st.session_state.current_page = "home"


def navigate_project(project_name: str) -> None:
    open_project(project_name)
    st.session_state.current_page = "project"


def consume_project_action_query() -> None:
    action = str(st.query_params.get("project_action", ""))
    project_name = str(st.query_params.get("project", ""))

    if action not in {"delete", "move"} or not project_name:
        return

    st.session_state.pending_project_action = action
    st.session_state.pending_project_name = project_name
    st.session_state.current_page = "home"
    st.query_params.clear()
    st.rerun()


def clear_pending_project_action() -> None:
    st.session_state.pending_project_action = ""
    st.session_state.pending_project_name = ""


def set_home_notice(kind: str, message: str) -> None:
    st.session_state.home_notice = {"kind": kind, "message": message}


def set_project_notice(kind: str, message: str) -> None:
    st.session_state.project_notice = {"kind": kind, "message": message}


def render_home_notice() -> None:
    notice = st.session_state.get("home_notice")
    if not isinstance(notice, dict):
        return

    kind = str(notice.get("kind", "info"))
    message = str(notice.get("message", ""))
    if not message:
        return

    if kind == "success":
        st.success(message)
    elif kind == "error":
        st.error(message)
    else:
        st.info(message)

    st.session_state.home_notice = None


def render_project_notice() -> None:
    notice = st.session_state.get("project_notice")
    if not isinstance(notice, dict):
        return

    kind = str(notice.get("kind", "info"))
    message = str(notice.get("message", ""))
    if not message:
        return

    if kind == "success":
        st.success(message)
    elif kind == "error":
        st.error(message)
    else:
        st.info(message)

    st.session_state.project_notice = None


def render_project_action_styles() -> None:
    st.markdown(
        """
        <style>
            a.project-action-button {
                align-items: center;
                border-radius: 0.5rem;
                color: #ffffff !important;
                display: flex;
                font-size: 0.875rem;
                font-weight: 700;
                justify-content: center;
                line-height: 1.2;
                min-height: 2.4rem;
                padding: 0.45rem 0.6rem;
                text-decoration: none !important;
                width: 100%;
            }

            a.project-action-button:hover {
                color: #ffffff !important;
                filter: brightness(0.94);
                text-decoration: none !important;
            }

            a.project-action-button.move {
                background: #2563eb;
            }

            a.project-action-button.delete {
                background: #dc2626;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_project_action_link(label: str, action: str, project_name: str, css_class: str) -> None:
    query = urlencode({"project_action": action, "project": project_name})
    st.markdown(
        f'<a class="project-action-button {css_class}" href="?{query}" target="_self">{escape(label)}</a>',
        unsafe_allow_html=True,
    )


def render_project_delete_panel(project_name: str) -> None:
    project_key = sanitize_project_name(project_name)
    st.warning(f"將刪除「{project_name}」整個資料夾與裡面全部檔案，這個動作不能復原。")
    col_confirm, col_cancel = st.columns([1, 1])

    if col_confirm.button("確認刪除", key=f"confirm_delete_{project_key}", width="stretch"):
        try:
            deleted_path = delete_project_folder(project_name)
        except (FileNotFoundError, OSError, ValueError) as exc:
            set_home_notice("error", str(exc))
        else:
            if st.session_state.active_project == project_name:
                st.session_state.active_project = ""
            set_home_notice("success", f"已刪除：{deleted_path}")

        clear_pending_project_action()
        st.rerun()

    if col_cancel.button("取消", key=f"cancel_delete_{project_key}", width="stretch"):
        clear_pending_project_action()
        st.rerun()


def get_move_quick_locations() -> dict[str, Path]:
    home = Path.home()
    workspace = Path.cwd()
    candidates = [
        ("桌面", home / "Desktop"),
        ("文件", home / "Documents"),
        ("下載", home / "Downloads"),
        ("目前工具資料夾", workspace),
        ("目前工具上一層", workspace.parent),
        ("使用者主資料夾", home),
    ]

    locations: dict[str, Path] = {}
    for label, path in candidates:
        if path.exists() and path.is_dir():
            locations[label] = path

    return locations


def normalize_browser_folder_path(raw_path: object, fallback: Path) -> Path:
    path = Path(str(raw_path or fallback)).expanduser()

    try:
        resolved_path = path.resolve()
    except OSError:
        return fallback

    return resolved_path if resolved_path.exists() and resolved_path.is_dir() else fallback


def list_selectable_folders(current_dir: Path) -> tuple[list[Path], str]:
    try:
        children = [
            path
            for path in current_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
    except OSError as exc:
        return [], f"無法讀取這個資料夾：{exc}"

    return sorted(children, key=lambda path: path.name.lower())[:MOVE_FOLDER_LIST_LIMIT], ""


def render_folder_picker(project_key: str) -> str:
    quick_locations = get_move_quick_locations()
    fallback = quick_locations.get("文件", Path.home())
    browser_key = f"move_browser_path_{project_key}"
    manual_key = f"move_manual_destination_{project_key}"
    quick_key = f"move_quick_location_{project_key}"

    if browser_key not in st.session_state:
        st.session_state[browser_key] = str(fallback)

    current_dir = normalize_browser_folder_path(st.session_state[browser_key], fallback)
    st.session_state[browser_key] = str(current_dir)

    if quick_locations:
        quick_labels = list(quick_locations.keys())
        matching_label = next(
            (
                label
                for label, path in quick_locations.items()
                if normalize_browser_folder_path(path, fallback) == current_dir
            ),
            quick_labels[0],
        )
        if st.session_state.get(quick_key) not in quick_labels:
            st.session_state[quick_key] = matching_label

        col_quick, col_go = st.columns([3, 1])
        selected_label = col_quick.selectbox(
            "快速位置",
            quick_labels,
            key=quick_key,
        )
        if col_go.button("前往", key=f"move_quick_go_{project_key}", width="stretch"):
            st.session_state[browser_key] = str(quick_locations[selected_label])
            st.rerun()

    st.caption("目前選取的目的地資料夾")
    st.code(str(current_dir), language="text")

    col_parent, col_current = st.columns([1, 3])
    if col_parent.button("上一層", key=f"move_parent_{project_key}", width="stretch"):
        st.session_state[browser_key] = str(current_dir.parent)
        st.rerun()
    col_current.caption("按下方子資料夾可以進入；按確認會搬到目前選取的位置。")

    folders, error_message = list_selectable_folders(current_dir)
    if error_message:
        st.warning(error_message)
    elif folders:
        st.caption("子資料夾")
        folder_columns = st.columns(3)
        for index, folder in enumerate(folders):
            column = folder_columns[index % len(folder_columns)]
            if column.button(folder.name, key=f"move_folder_{project_key}_{index}_{folder.name}", width="stretch"):
                st.session_state[browser_key] = str(folder)
                st.rerun()
    else:
        st.caption("這個資料夾底下沒有可選的子資料夾。")

    if len(folders) == MOVE_FOLDER_LIST_LIMIT:
        st.caption(f"只顯示前 {MOVE_FOLDER_LIST_LIMIT} 個子資料夾；如需其他位置，可用快速位置或備用路徑。")

    with st.expander("手動路徑備用", expanded=False):
        manual_destination = st.text_input(
            "貼上目的地資料夾路徑",
            key=manual_key,
            placeholder="/Users/halelua/Desktop",
        )
        if st.button("使用這個路徑", key=f"use_manual_destination_{project_key}", width="stretch"):
            manual_path = Path(manual_destination.strip()).expanduser()
            if manual_path.exists() and manual_path.is_dir():
                st.session_state[browser_key] = str(manual_path.resolve())
                st.rerun()
            else:
                st.error("這個路徑不存在或不是資料夾。")

    return str(current_dir)


def render_project_move_panel(project_name: str) -> None:
    project_key = sanitize_project_name(project_name)

    st.info(f"將搬移「{project_name}」整個資料夾。請先選目的地資料夾，再按確認搬移。")
    destination_dir = render_folder_picker(project_key)

    col_confirm, col_cancel = st.columns([1, 1])
    if col_confirm.button("確認搬移到目前選取資料夾", key=f"confirm_move_{project_key}", width="stretch"):
        try:
            moved_path = move_project_folder(project_name, destination_dir)
        except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
            set_home_notice("error", str(exc))
            st.rerun()
        else:
            if st.session_state.active_project == project_name:
                st.session_state.active_project = ""
            set_home_notice("success", f"已搬移到：{moved_path}")
            clear_pending_project_action()
            st.rerun()

    if col_cancel.button("取消", key=f"cancel_move_{project_key}", width="stretch"):
        clear_pending_project_action()
        st.rerun()


def render_previous_projects() -> None:
    projects = list_existing_projects()

    st.subheader("已做過的專案")
    render_project_action_styles()

    if not projects:
        st.caption("目前還沒有已建立的專案。")
        return

    st.caption("點選任一專案即可回到當初建立專案時的版型，查看照片與指令。")

    header_cols = st.columns([3, 1, 1, 1, 1.5, 2.3])
    header_cols[0].caption("專案")
    header_cols[1].caption("JPG")
    header_cols[2].caption("OUTPUT")
    header_cols[3].caption("PROMPTS")
    header_cols[4].caption("最後更新")
    header_cols[5].caption("操作")

    for project in projects:
        project_name = str(project["name"])
        project_key = sanitize_project_name(project_name)

        with st.container(border=True):
            col_name, col_input, col_output, col_prompt, col_modified, col_action = st.columns(
                [3, 1, 1, 1, 1.5, 2.3]
            )
            col_name.markdown(f"**{project_name}**")
            col_name.caption(str(project["path"]))
            col_input.write(f"{project.get('jpg_count', 0)} 個")
            col_output.write(f"{project['output_count']} 個")
            col_prompt.write(f"{project['prompt_count']} 個")
            col_modified.write(str(project["modified_at"]))

            col_open, col_move, col_delete = col_action.columns(3)

            with col_open:
                if st.button("進入", key=f"open_project_{project_key}", width="stretch"):
                    navigate_project(project_name)
                    st.rerun()
            with col_move:
                render_project_action_link("搬移", "move", project_name, "move")
            with col_delete:
                render_project_action_link("刪除", "delete", project_name, "delete")

            if (
                st.session_state.pending_project_name == project_name
                and st.session_state.pending_project_action == "delete"
            ):
                render_project_delete_panel(project_name)

            if (
                st.session_state.pending_project_name == project_name
                and st.session_state.pending_project_action == "move"
            ):
                render_project_move_panel(project_name)


def render_home_page() -> None:
    st.title("AI 圖片批次 Prompt 管理器")
    render_home_notice()

    with st.container(border=True):
        project_name = st.text_input(
            "專案名稱",
            value=st.session_state.project_name,
            placeholder="例如：春季新品視覺、角色設定圖、廣告素材 A 組",
        )

        col_create, col_info = st.columns([1, 3])
        with col_create:
            if st.button("建立 / 開啟專案", type="primary", width="stretch"):
                if project_name.strip():
                    navigate_project(project_name.strip())
                    st.rerun()
                else:
                    st.error("請先輸入專案名稱。")

        with col_info:
            st.caption("建立新專案或從下方已做過的專案進入編輯頁。")

    render_previous_projects()


def get_resolution() -> str:
    width = normalize_dimension(st.session_state.resolution_width, DEFAULT_WIDTH)
    height = normalize_dimension(st.session_state.resolution_height, DEFAULT_HEIGHT)

    return format_resolution(width, height)


def get_product_reference() -> dict[str, str]:
    return {
        key: str(st.session_state.get(key, "") or "").strip()
        for key in PRODUCT_REFERENCE_FIELDS
    }


def render_project_settings(paths: dict[str, object]) -> str:
    with st.container(border=True):
        st.subheader("專案設定")
        col_width, col_height = st.columns(2)

        with col_width:
            st.number_input(
                "寬度 px",
                min_value=1,
                step=1,
                key="resolution_width",
                help="這是整個專案共用的輸出寬度，會自動寫進 8 組 prompt。",
            )

        with col_height:
            st.number_input(
                "高度 px",
                min_value=1,
                step=1,
                key="resolution_height",
                help="這是整個專案共用的輸出高度，會自動寫進 8 組 prompt。",
            )

        st.caption(
            f"Prompt 會使用：{get_resolution()}"
        )

        st.divider()
        st.subheader("原始照片匯入")
        render_original_photo_import(paths)

        st.divider()
        st.subheader("產品文檔參考")
        col_features, col_specs, col_contents = st.columns(3)
        field_columns = [col_features, col_specs, col_contents]
        placeholders = {
            "product_features": "例如：核心賣點、使用情境、差異化優勢",
            "product_specifications": "例如：尺寸、材質、容量、相容性、保固",
            "product_contents": "例如：盒裝內容、配件、包裝項目",
        }

        for column, (key, label) in zip(field_columns, PRODUCT_REFERENCE_FIELDS.items()):
            with column:
                st.text_area(
                    label,
                    key=key,
                    height=140,
                    placeholder=placeholders[key],
                )

        if st.button("儲存專案設定", width="stretch"):
            config_path = save_project_config(
                paths["prompts"],
                st.session_state.resolution_width,
                st.session_state.resolution_height,
                get_product_reference(),
            )
            st.success(f"已儲存專案設定：{config_path}")

    return get_resolution()


def render_style_input(task: ImageTask) -> None:
    style_key = f"task_{task.index}_style"
    if style_key not in st.session_state:
        st.session_state[style_key] = task.style

    st.text_input(
        "風格 style",
        key=style_key,
        placeholder="例如：寫實攝影風、商品攝影、電影感，也可以直接輸入任何風格",
        help="這裡是一般文字輸入欄位，可以自由打字，不是篩選器。",
        on_change=mark_task_active,
        args=(task.index,),
    )

    if st.session_state.style_history:
        st.caption("常用風格參考：" + "、".join(st.session_state.style_history[:6]))


def resolve_project_output_file(output_dir: Path, filename: str) -> Path:
    safe_name = Path(filename).name
    if not safe_name:
        raise FileNotFoundError("找不到指定的輸出圖片。")

    output_root = output_dir.resolve(strict=False)
    output_path = (output_dir / safe_name).resolve(strict=False)

    if output_path == output_root or output_root not in output_path.parents:
        raise ValueError("輸出圖片路徑不在 OUTPUT 資料夾內，已停止操作。")
    if not output_path.exists() or not output_path.is_file():
        raise FileNotFoundError(f"找不到輸出圖片：{safe_name}")

    return output_path


def reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=True)
        return

    if sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(path)], check=True)
        return

    subprocess.run(["xdg-open", str(path.parent)], check=True)


def consume_project_file_action_query(paths: dict[str, Path]) -> None:
    action = str(st.query_params.get("project_file_action", ""))
    filename = str(st.query_params.get("file", ""))

    if action != "reveal_output" or not filename:
        return

    try:
        output_path = resolve_project_output_file(paths["output"], filename)
        reveal_in_file_manager(output_path)
    except (FileNotFoundError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        set_project_notice("error", str(exc))
    else:
        set_project_notice("success", f"已在資料夾中選取：{output_path.name}")

    st.query_params.clear()
    st.rerun()


def delete_output_image_file(output_path: Path, output_dir: Path) -> Path:
    resolved_output_path = resolve_project_output_file(output_dir, output_path.name)
    resolved_output_path.unlink()
    return resolved_output_path


@st.dialog("確認刪除照片")
def render_output_delete_dialog(output_path_text: str, output_dir_text: str) -> None:
    output_path = Path(output_path_text)
    output_dir = Path(output_dir_text)
    st.warning(f"確定要刪除這張輸出完成的圖片嗎？\n\n{output_path.name}")

    col_delete, col_cancel = st.columns(2)
    if col_delete.button("確認刪除", type="primary", width="stretch"):
        try:
            deleted_path = delete_output_image_file(output_path, output_dir)
        except (FileNotFoundError, OSError, ValueError) as exc:
            set_project_notice("error", str(exc))
        else:
            set_project_notice("success", f"已刪除：{deleted_path.name}")
        st.rerun()

    if col_cancel.button("取消", width="stretch"):
        st.rerun()


def image_preview_data_uri(image_path: Path, max_size: int = 900) -> str | None:
    try:
        from PIL import Image, ImageOps

        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image)
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((max_size, max_size), resampling)
            buffer = BytesIO()
            if image.mode in {"RGBA", "LA", "P"}:
                image.convert("RGBA").save(buffer, format="PNG")
                mime_type = "image/png"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=90, optimize=True)
                mime_type = "image/jpeg"
            image_bytes = buffer.getvalue()
    except Exception:
        try:
            image_bytes = image_path.read_bytes()
        except OSError:
            return None

        suffix = image_path.suffix.lower()
        mime_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def render_image_preview_frame(
    image_path: Path | None,
    empty_label: str,
    caption: str = "",
    link_url: str = "",
    link_title: str = "",
    empty_icon: str = "",
) -> None:
    width = normalize_dimension(st.session_state.resolution_width, DEFAULT_WIDTH)
    height = normalize_dimension(st.session_state.resolution_height, DEFAULT_HEIGHT)

    if image_path:
        data_uri = image_preview_data_uri(image_path)
        if data_uri:
            content = (
                f'<img src="{data_uri}" alt="{escape(caption or image_path.name)}" '
                'style="display:block;height:100%;object-fit:contain;width:100%;">'
            )
            if link_url:
                content = (
                    f'<a href="{escape(link_url)}" target="_self" title="{escape(link_title)}" '
                    'style="display:block;height:100%;width:100%;">'
                    f"{content}</a>"
                )
        else:
            content = f'<span>{escape(empty_label)}</span>'
    else:
        icon_html = (
            f'<div style="font-size:2.1rem;line-height:1;margin-bottom:0.45rem;">{escape(empty_icon)}</div>'
            if empty_icon
            else ""
        )
        content = (
            '<div style="align-items:center;display:flex;flex-direction:column;justify-content:center;">'
            f"{icon_html}"
            f'<div>{escape(empty_label)}</div>'
            "</div>"
        )

    caption_html = (
        f'<div style="color:#94a3b8;font-size:0.85rem;margin-top:0.45rem;text-align:center;">'
        f"{escape(caption)}</div>"
        if caption
        else ""
    )
    st.markdown(
        f"""
        <div style="
            align-items: center;
            aspect-ratio: {escape(width)} / {escape(height)};
            background: #f8fafc;
            border: 1px solid #334155;
            border-radius: 0.5rem;
            color: #64748b;
            display: flex;
            font-size: 0.95rem;
            font-weight: 700;
            justify-content: center;
            min-height: 160px;
            overflow: hidden;
            width: 100%;
        ">
            {content}
        </div>
        {caption_html}
        """,
        unsafe_allow_html=True,
    )


def save_reference_uploaded_file(uploaded_file: object, jpg_dir: Path) -> Path | None:
    original_name = Path(str(getattr(uploaded_file, "name", "reference.jpg")))
    valid_extensions = {".png", ".jpg", ".jpeg", ".webp"}
    extension = original_name.suffix.lower() or ".jpg"
    if extension not in valid_extensions:
        st.error("請選擇 PNG、JPG、JPEG 或 WEBP 圖片檔。")
        return None

    jpg_dir.mkdir(parents=True, exist_ok=True)
    image_bytes = bytes(uploaded_file.getbuffer())
    safe_stem = sanitize_filename(original_name.stem, fallback="reference")
    existing_path = jpg_dir / f"{safe_stem}{extension}"

    if existing_path.exists():
        try:
            if existing_path.read_bytes() == image_bytes:
                return existing_path
        except OSError:
            pass

    try:
        target_path = unique_file_path(jpg_dir, f"{original_name.stem}{extension}")
        with target_path.open("wb") as output_file:
            output_file.write(image_bytes)
        return target_path
    except OSError as exc:
        st.error(f"無法匯入參考照片到 JPG 資料夾：{exc}")
        return None


def render_task_input_preview(paths: dict[str, Path], task: ImageTask) -> str:
    reference_key = f"task_{task.index}_reference_image"
    if reference_key not in st.session_state:
        st.session_state[reference_key] = task.reference_image

    jpg_dir = paths["jpg"]
    input_dir = paths["input"]
    current_reference_name = str(st.session_state.get(reference_key, task.reference_image) or "")
    upload_nonce_key = f"task_{task.index}_reference_upload_nonce"
    upload_processed_key = f"task_{task.index}_reference_upload_processed"
    if upload_nonce_key not in st.session_state:
        st.session_state[upload_nonce_key] = 0

    header_col, clear_col = st.columns([3, 1])
    header_col.caption("INPUT 參考照片")
    with clear_col:
        if st.button(
            "清除",
            key=f"task_{task.index}_reference_clear",
            width="stretch",
            disabled=not bool(current_reference_name.strip()),
        ):
            mark_task_active(task.index)
            current_reference_name = ""
            st.session_state[reference_key] = ""
            st.session_state[upload_processed_key] = ""
            st.session_state[upload_nonce_key] += 1
            st.rerun()

    reference_task = ImageTask(
        index=task.index,
        reference_image=current_reference_name,
    )
    reference_path = find_task_reference_image(jpg_dir, reference_task, fallback_dirs=[input_dir])

    if reference_path:
        folder_label = "JPG" if reference_path.parent == jpg_dir else "INPUT"
        render_image_preview_frame(
            reference_path,
            "請選擇照片",
            f"{folder_label}：{reference_path.name}",
        )
    elif current_reference_name.strip():
        st.warning(f"找不到 JPG/{current_reference_name} 或 input/{current_reference_name}")
        render_image_preview_frame(None, "請選擇照片", empty_icon="+")
    else:
        render_image_preview_frame(None, "請選擇照片", empty_icon="+")

    uploaded_reference = st.file_uploader(
        "選擇 INPUT 參考照片",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=False,
        key=f"task_{task.index}_reference_upload_{st.session_state[upload_nonce_key]}",
        help=f"選取後會存回本專案 JPG 資料夾：{jpg_dir}",
        label_visibility="collapsed",
        on_change=mark_task_active,
        args=(task.index,),
    )
    if uploaded_reference is not None:
        signature = uploaded_file_signature(uploaded_reference)
        if st.session_state.get(upload_processed_key) != signature:
            imported_path = save_reference_uploaded_file(uploaded_reference, jpg_dir)
            if imported_path:
                current_reference_name = imported_path.name
                st.session_state[reference_key] = current_reference_name
                st.session_state[upload_processed_key] = signature
                st.success(f"已選擇：{current_reference_name}")
                st.rerun()

    return str(st.session_state.get(reference_key, current_reference_name) or "")


def render_task_output_preview(paths: dict[str, Path], task: ImageTask, output_path: Path | None = None) -> None:
    if output_path is None:
        output_task = ImageTask(index=task.index)
        output_path = find_task_output_image(paths["output"], output_task)

    header_col, action_col = st.columns([3, 1])
    header_col.caption("輸出完成的圖片")
    if output_path:
        with action_col:
            if st.button("刪除", key=f"task_{task.index}_output_delete", width="stretch"):
                mark_task_active(task.index)
                render_output_delete_dialog(str(output_path), str(paths["output"]))
    else:
        action_col.write("")

    if output_path:
        query = urlencode({"project_file_action": "reveal_output", "file": output_path.name})
        render_image_preview_frame(
            output_path,
            "尚未輸出",
            f"OUTPUT：{output_path.name}",
            link_url=f"?{query}",
            link_title="在資料夾中顯示這張圖片",
        )
    else:
        render_image_preview_frame(None, "尚未輸出")

def streamlit_label_text(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def task_expander_label(task: ImageTask, has_output: bool) -> str:
    title = task.title or st.session_state.get(f"task_{task.index}_title", "") or "未命名圖片"
    label = streamlit_label_text(f"{task.index:02d}. {title}")
    return label if has_output else f":red[{label}]"


def render_task_status_styles() -> None:
    st.markdown(
        """
        <style>
            div[data-testid="stExpander"] details summary {
                background: #1f2937;
                border-radius: 0.45rem;
                padding: 0.35rem 0.75rem;
            }

            div[data-testid="stExpander"] details summary [data-testid="stMarkdownContainer"],
            div[data-testid="stExpander"] details summary p,
            div[data-testid="stExpander"] details summary [data-testid="stMarkdownContainer"] p {
                color: #ffffff !important;
                font-weight: 700;
            }

            div[data-testid="stExpander"] details summary span[data-testid="stIconMaterial"],
            div[data-testid="stExpander"] details summary svg {
                color: #ffffff !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_task_editor(paths: dict[str, Path]) -> list[ImageTask]:
    st.subheader("8 張圖片任務")
    render_task_status_styles()
    tasks = normalize_tasks(st.session_state.tasks, count=TASK_COUNT)
    active_task_index = get_active_task_index()

    for task in tasks:
        output_path = find_task_output_image(paths["output"], ImageTask(index=task.index))
        with st.expander(task_expander_label(task, output_path is not None), expanded=task.index == active_task_index):
            preview_input_col, preview_output_col = st.columns(2)
            with preview_input_col:
                reference_name = render_task_input_preview(paths, task)
            with preview_output_col:
                render_task_output_preview(paths, task, output_path)

            col_left, col_right = st.columns([2, 1])

            with col_left:
                title_key = f"task_{task.index}_title"
                content_key = f"task_{task.index}_content"
                text_slogan_key = f"task_{task.index}_text_slogan"
                if title_key not in st.session_state:
                    st.session_state[title_key] = task.title
                if content_key not in st.session_state:
                    st.session_state[content_key] = task.content
                if text_slogan_key not in st.session_state:
                    st.session_state[text_slogan_key] = task.text_slogan

                st.text_input(
                    "圖片標題",
                    key=title_key,
                    on_change=mark_task_active,
                    args=(task.index,),
                )
                render_style_input(task)
                st.text_area(
                    "內容 content",
                    height=120,
                    key=content_key,
                    on_change=mark_task_active,
                    args=(task.index,),
                )
                st.text_input(
                    "文字 Slogan（AI 會潤飾後融入畫面）",
                    key=text_slogan_key,
                    placeholder="輸入想傳達的文字方向；AI 會改寫成適合圖片的文案",
                    on_change=mark_task_active,
                    args=(task.index,),
                )

            with col_right:
                preview_task = ImageTask(
                    index=task.index,
                    title=st.session_state.get(f"task_{task.index}_title", task.title),
                    style=style_from_widgets(task.index, task.style),
                    content=st.session_state.get(f"task_{task.index}_content", task.content),
                    text_slogan=st.session_state.get(f"task_{task.index}_text_slogan", task.text_slogan),
                    reference_image=st.session_state.get(f"task_{task.index}_reference_image", reference_name),
                )
                st.text_area(
                    "Prompt 預覽",
                    value=build_prompt(
                        preview_task,
                        st.session_state.active_project,
                        get_resolution(),
                    ),
                    height=260,
                    disabled=True,
                    key=f"task_{task.index}_prompt_preview",
                )

    updated_tasks = get_tasks_from_widgets()
    sync_tasks_to_state(updated_tasks)

    return updated_tasks


def has_task_instruction(task: ImageTask) -> bool:
    return any(
        [
            task.title.strip(),
            task.style.strip(),
            task.content.strip(),
            task.text_slogan.strip(),
            task.reference_image.strip(),
        ]
    )


def save_project_exports(paths: dict[str, object], tasks: list[ImageTask]) -> tuple[object, object, object, object]:
    resolution = get_resolution()
    records = build_all_prompt_records(tasks, st.session_state.active_project, resolution)
    txt_path, json_path = write_prompt_files(records, paths["prompts"])
    tasks_path = save_project_tasks(tasks, paths["prompts"])
    config_path = save_project_config(
        paths["prompts"],
        st.session_state.resolution_width,
        st.session_state.resolution_height,
        get_product_reference(),
    )
    st.session_state.style_history = update_style_history([task.style for task in tasks])

    return txt_path, json_path, tasks_path, config_path


def render_chatgpt_web_generation_settings() -> None:
    with st.expander("ChatGPT 網頁產圖設定", expanded=False):
        st.number_input(
            "每張圖片最多等待秒數",
            min_value=60,
            max_value=1200,
            step=30,
            key="chatgpt_timeout_seconds",
            help="ChatGPT 網頁版產圖時間不固定；逾時會跳過該張並記錄錯誤。",
        )
        st.radio(
            "ChatGPT 登入資料來源",
            [CHATGPT_BROWSER_PROFILE_CHROME, CHATGPT_BROWSER_PROFILE_TOOL],
            horizontal=True,
            key="chatgpt_browser_profile_mode",
            help="使用平常 Chrome profile 可沿用你已登入 ChatGPT 的狀態；工具專用 profile 則需要在該視窗登入一次。",
        )

        if st.session_state.chatgpt_browser_profile_mode == CHATGPT_BROWSER_PROFILE_CHROME:
            st.text_input(
                "Chrome 使用者資料夾",
                key="chatgpt_chrome_user_data_dir",
                help="macOS 預設通常是 ~/Library/Application Support/Google/Chrome。",
            )
            st.text_input(
                "Chrome profile 名稱",
                key="chatgpt_chrome_profile_name",
                help="常見值是 Default、Profile 1、Profile 2。預設會讀取 Chrome 上次使用的 profile。",
            )
            st.caption("如果一般 Chrome 已經開著而無法接管，請先完全關閉 Chrome 後再按生成。")
        else:
            st.caption(f"工具專用 profile 位置：{CHATGPT_PROFILE_DIR}。第一次使用需要在開啟的視窗登入 ChatGPT。")

        st.caption(
            "先按登入按鈕確認 ChatGPT 可用；正式生成時才會依序上傳參考圖、送出 prompt，並把生成圖片存進 OUTPUT。"
        )


def get_chatgpt_session_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "timeout_seconds": int(st.session_state.chatgpt_timeout_seconds),
    }

    if st.session_state.chatgpt_browser_profile_mode != CHATGPT_BROWSER_PROFILE_CHROME:
        return kwargs

    chrome_user_data_dir = Path(str(st.session_state.chatgpt_chrome_user_data_dir)).expanduser()
    profile_name = str(st.session_state.chatgpt_chrome_profile_name).strip()
    if not profile_name:
        profile_name = infer_chrome_profile_name(chrome_user_data_dir)

    kwargs.update(
        {
            "profile_dir": chrome_user_data_dir,
            "profile_name": profile_name,
            "create_profile_dir": False,
        }
    )
    return kwargs


def describe_chatgpt_profile() -> str:
    if st.session_state.chatgpt_browser_profile_mode != CHATGPT_BROWSER_PROFILE_CHROME:
        return f"目前使用：工具專用 Chrome profile（{CHATGPT_PROFILE_DIR}）"

    chrome_user_data_dir = Path(str(st.session_state.chatgpt_chrome_user_data_dir)).expanduser()
    profile_name = str(st.session_state.chatgpt_chrome_profile_name).strip() or infer_chrome_profile_name(chrome_user_data_dir)

    return f"目前使用：平常 Chrome 登入狀態（資料夾：{chrome_user_data_dir}，profile：{profile_name}）"


def prepare_chatgpt_login() -> None:
    st.info("正在開啟 ChatGPT。請在彈出的 Chrome 視窗登入或完成驗證；看到 ChatGPT 輸入框後，工具會自動確認完成。")
    st.caption(describe_chatgpt_profile())
    status = st.empty()

    try:
        status.write("等待 ChatGPT 登入狀態...")
        with ChatGPTWebSession(**get_chatgpt_session_kwargs()):
            pass
    except (ChatGPTWebAutomationError, RuntimeError, OSError) as exc:
        st.error("ChatGPT 登入確認失敗。")
        st.write(str(exc))
        return
    finally:
        status.empty()

    st.success("已確認 ChatGPT 可以使用。接下來可以按「GPT 生成缺少圖片並整理檔案」。")


def generate_product_document(paths: dict[str, Path]) -> None:
    product_reference = get_product_reference()
    if not any(product_reference.values()):
        st.warning("請先填入產品特色、產品規格或產品內容，才能產出商品文檔。")
        return

    config_path = save_project_config(
        paths["prompts"],
        st.session_state.resolution_width,
        st.session_state.resolution_height,
        product_reference,
    )
    prompt = build_product_document_prompt(st.session_state.active_project, product_reference)
    status = st.empty()

    try:
        status.write("正在把產品資料送到 ChatGPT 潤飾...")
        with ChatGPTWebSession(**get_chatgpt_session_kwargs()) as chatgpt:
            polished_text = chatgpt.generate_text(prompt)
    except (ChatGPTWebAutomationError, RuntimeError, OSError) as exc:
        status.empty()
        st.error("商品文檔產出失敗。")
        st.write(str(exc))
        return

    try:
        document_path = write_product_document(
            project_name=st.session_state.active_project,
            polished_text=polished_text,
            product_reference=product_reference,
            project_root=paths["root"],
        )
    except OSError as exc:
        status.empty()
        st.error(f"Word 文檔寫入失敗：{exc}")
        return

    status.empty()
    st.success(f"已產出商品文檔：{document_path}")
    st.caption(f"專案設定已同步儲存：{config_path}")


def generate_and_save_images(
    paths: dict[str, Path],
    tasks: list[ImageTask],
    progress_placeholder: object | None = None,
    status_placeholder: object | None = None,
) -> None:
    active_tasks = [task for task in tasks if has_task_instruction(task)]
    if not active_tasks:
        st.warning("目前沒有可產圖的任務。請至少填入圖片標題、風格、內容或參考照片。")
        return

    missing_tasks = list_missing_output_tasks(paths["output"], active_tasks)
    skipped_tasks = [task for task in active_tasks if find_task_output_image(paths["output"], task) is not None]

    txt_path, json_path, tasks_path, config_path = save_project_exports(paths, tasks)
    st.info(f"已先儲存任務與 prompt：{txt_path}, {json_path}, {tasks_path}, {config_path}")

    if not missing_tasks:
        st.success("OUTPUT 已有全部可產圖任務的圖片，沒有空缺需要補。若要重生某張，刪除對應編號的圖片後再執行。")
        return

    if skipped_tasks:
        skipped_labels = ", ".join(f"{task.index:02d}" for task in skipped_tasks)
        missing_labels = ", ".join(f"{task.index:02d}" for task in missing_tasks)
        st.info(f"已略過已有 OUTPUT 的編號：{skipped_labels}；這次只補：{missing_labels}。")

    st.info("即將開啟 ChatGPT 網頁版並開始補缺少的圖片。若看到登入或驗證畫面，請先完成後等待工具繼續。")
    st.caption(describe_chatgpt_profile())

    progress_target = progress_placeholder if progress_placeholder is not None else st
    status = status_placeholder if status_placeholder is not None else st.empty()
    progress = progress_target.progress(0)
    generated_rows: list[dict[str, str]] = []
    error_rows: list[dict[str, str]] = []

    try:
        with ChatGPTWebSession(**get_chatgpt_session_kwargs()) as chatgpt:
            for position, task in enumerate(missing_tasks, start=1):
                task_title = task.title or f"Image {task.index:02d}"
                status.write(f"正在生成 {task.index:02d}. {task_title}")
                reference_path = find_task_reference_image(paths["jpg"], task, fallback_dirs=[paths["input"]])
                prompt = build_prompt(task, st.session_state.active_project, get_resolution())

                try:
                    generated = chatgpt.generate_image(prompt, reference_path)
                    output_path = next_organized_output_path(paths["output"], task)
                    save_generated_png(
                        generated.image_bytes,
                        output_path,
                        st.session_state.resolution_width,
                        st.session_state.resolution_height,
                    )
                    generated_rows.append(
                        {
                            "任務": f"{task.index:02d}. {task_title}",
                            "檔案": output_path.name,
                            "模式": "參考圖 + ChatGPT 網頁" if reference_path else "文字 + ChatGPT 網頁",
                        }
                    )
                except (ChatGPTWebAutomationError, RuntimeError, OSError) as exc:
                    error_rows.append(
                        {
                            "任務": f"{task.index:02d}. {task_title}",
                            "錯誤": str(exc),
                        }
                    )

                progress.progress(position / len(missing_tasks))
    except (ChatGPTWebAutomationError, RuntimeError, OSError) as exc:
        error_rows.append({"任務": "啟動 ChatGPT 網頁版", "錯誤": str(exc)})

    status.write("生成流程已完成。")

    if generated_rows:
        st.success(f"已生成並整理 {len(generated_rows)} 張圖片。")
        st.dataframe(generated_rows, width="stretch", hide_index=True)

    if error_rows:
        st.error(f"{len(error_rows)} 張圖片生成失敗。")
        st.dataframe(error_rows, width="stretch", hide_index=True)


def render_saved_instructions(paths: dict[str, object]) -> None:
    prompt_text = load_prompt_text(paths["prompts"])
    config = load_project_config(paths["prompts"])
    saved_tasks = normalize_tasks(load_project_tasks(paths["prompts"]), count=TASK_COUNT)
    saved_tasks = [task for task in saved_tasks if has_task_instruction(task)]
    project_key = sanitize_project_name(st.session_state.active_project)

    if not saved_tasks and not prompt_text:
        st.info("這個專案目前還沒有儲存過的指令。")
        return

    if saved_tasks:
        st.caption("以下內容來自 prompts/tasks.json，是上次儲存任務時的指令內容。")

        for task in saved_tasks:
            with st.expander(f"{task.index:02d}. {task.title or '未命名圖片'}"):
                if task.style:
                    st.caption(f"風格：{task.style}")
                if task.text_slogan:
                    st.caption(f"文字 Slogan：{task.text_slogan}")
                if task.reference_image:
                    st.caption(f"參考照片：{task.reference_image}")
                st.text_area(
                    "內容 content",
                    value=task.content,
                    height=120,
                    disabled=True,
                    key=f"saved_task_content_{project_key}_{task.index}",
                )
                st.text_area(
                    "完整 Prompt",
                    value=build_prompt(task, st.session_state.active_project, config["resolution"]),
                    height=220,
                    disabled=True,
                    key=f"saved_task_prompt_{project_key}_{task.index}",
                )

    if prompt_text:
        st.text_area(
            "prompts.txt",
            value=prompt_text,
            height=360,
            disabled=True,
            key=f"saved_prompts_txt_{project_key}",
        )


def render_file_table(folder_label: str, files: list[dict[str, str]]) -> None:
    if not files:
        st.info(f"{folder_label} 資料夾目前沒有檔案。")
        return

    st.dataframe(files, width="stretch", hide_index=True)


def uploaded_file_signature(uploaded_file: object) -> str:
    name = str(getattr(uploaded_file, "name", "uploaded"))
    buffer = uploaded_file.getbuffer()
    digest = hashlib.sha256(buffer).hexdigest()

    return f"{name}:{digest}"


def render_original_photo_import(paths: dict[str, Path]) -> None:
    project_key = sanitize_project_name(st.session_state.active_project)
    processed_key = f"original_photo_import_processed_{project_key}"
    if processed_key not in st.session_state:
        st.session_state[processed_key] = []

    uploaded_files = st.file_uploader(
        "匯入原始照片檔案",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key=f"original_photo_import_{project_key}",
        help="可一次拖曳多個檔案；檔案會直接存入本專案的 JPG 資料夾。",
    )

    if not uploaded_files:
        return

    processed = set(st.session_state.get(processed_key, []))
    saved_paths: list[Path] = []

    for uploaded_file in uploaded_files:
        signature = uploaded_file_signature(uploaded_file)
        if signature in processed:
            continue

        saved_path = save_original_uploaded_file(uploaded_file, paths["jpg"])
        saved_paths.append(saved_path)
        processed.add(signature)

    st.session_state[processed_key] = sorted(processed)

    if saved_paths:
        st.success(f"已匯入 {len(saved_paths)} 個檔案到 JPG 資料夾。")
        st.caption(str(paths["jpg"]))
        st.dataframe(
            [{"檔名": path.name} for path in saved_paths],
            width="stretch",
            hide_index=True,
        )


def render_project_browser(paths: dict[str, object]) -> None:
    st.subheader("專案瀏覽")
    prompt_tab, jpg_tab, input_tab, output_tab = st.tabs(["當初下的指令", "JPG 原始照片", "INPUT 檔案", "OUTPUT 檔案"])

    with prompt_tab:
        render_saved_instructions(paths)

    with jpg_tab:
        st.caption(str(paths["jpg"]))
        render_file_table("JPG", list_folder_files(paths["jpg"]))

    with input_tab:
        st.caption(str(paths["input"]))
        render_file_table("INPUT", list_folder_files(paths["input"]))

    with output_tab:
        st.caption(str(paths["output"]))
        render_file_table("OUTPUT", list_folder_files(paths["output"]))


def render_export_tools(
    paths: dict[str, Path],
    tasks: list[ImageTask],
    progress_placeholder: object | None = None,
    status_placeholder: object | None = None,
) -> None:
    st.subheader("匯出與整理")
    render_chatgpt_web_generation_settings()

    st.caption(describe_chatgpt_profile())
    active_tasks = [task for task in tasks if has_task_instruction(task)]
    missing_tasks = list_missing_output_tasks(paths["output"], active_tasks)
    if active_tasks and missing_tasks:
        missing_labels = ", ".join(f"{task.index:02d}" for task in missing_tasks)
        st.caption(f"目前 OUTPUT 缺少：{missing_labels}。按生成時只會補這些編號。")
    elif active_tasks:
        st.caption("目前 OUTPUT 已有全部可產圖任務；若要重生某張，刪除對應編號後再按生成。")

    col_login, col_generate, col_document = st.columns(3)

    with col_login:
        if st.button("開啟 / 登入 ChatGPT", width="stretch"):
            prepare_chatgpt_login()

    with col_generate:
        if st.button("GPT 生成缺少圖片並整理檔案", type="primary", width="stretch"):
            generate_and_save_images(
                paths,
                tasks,
                progress_placeholder=progress_placeholder,
                status_placeholder=status_placeholder,
            )

    with col_document:
        if st.button("產出商品文檔", width="stretch"):
            generate_product_document(paths)


def render_folder_overview(paths: dict[str, object]) -> None:
    with st.sidebar:
        st.header("資料夾")
        st.code(
            "\n".join(
                [
                    str(paths["root"]),
                    str(paths["jpg"]),
                    str(paths["input"]),
                    str(paths["output"]),
                    str(paths["prompts"]),
                ]
            ),
            language="text",
        )
        st.caption("原始照片會匯入 JPG；生成結果會存進 output。")


def render_project_page() -> None:
    if not st.session_state.active_project:
        navigate_home()
        st.rerun()
        return

    paths = ensure_project_dirs(st.session_state.active_project)
    consume_project_file_action_query(paths)

    header_col, progress_col, action_col = st.columns([3.2, 1.6, 1.2])
    with header_col:
        st.title(st.session_state.active_project)
        st.caption(f"編輯頁：projects/{sanitize_project_name(st.session_state.active_project)}")
    with progress_col:
        st.write("")
        header_status = st.empty()
        header_progress = st.empty()
    with action_col:
        st.write("")
        home_col, refresh_col = st.columns(2)
        with home_col:
            if st.button("回首頁", width="stretch"):
                navigate_home()
                st.rerun()
        with refresh_col:
            if st.button("刷新", width="stretch"):
                st.rerun()

    render_project_notice()
    render_folder_overview(paths)
    render_project_settings(paths)
    tasks = render_task_editor(paths)
    render_export_tools(paths, tasks, progress_placeholder=header_progress, status_placeholder=header_status)


def main() -> None:
    init_state()
    consume_project_action_query()

    if st.session_state.current_page == "project":
        render_project_page()
    else:
        render_home_page()


if __name__ == "__main__":
    main()
