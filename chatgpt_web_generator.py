from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import unquote_to_bytes


CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_PROFILE_DIR = Path("projects") / ".chatgpt_browser_profile"
CHATGPT_DOWNLOADS_DIR = Path("projects") / ".chatgpt_downloads"
DEFAULT_CHROME_PROFILE_NAME = "Default"
CHROME_CANDIDATES = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
]


class ChatGPTWebAutomationError(RuntimeError):
    pass


@dataclass
class ChatGPTGeneratedImage:
    image_bytes: bytes
    source: str


def find_chrome_executable() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate

    raise ChatGPTWebAutomationError("找不到 Google Chrome、Chromium 或 Microsoft Edge。")


def default_chrome_user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"

    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Google" / "Chrome" / "User Data"

    return Path.home() / ".config" / "google-chrome"


def infer_chrome_profile_name(user_data_dir: Path | None = None) -> str:
    local_state_path = (user_data_dir or default_chrome_user_data_dir()).expanduser() / "Local State"

    try:
        with local_state_path.open("r", encoding="utf-8") as local_state_file:
            data = json.load(local_state_file)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_CHROME_PROFILE_NAME

    profile_data = data.get("profile", {}) if isinstance(data, dict) else {}
    last_used = profile_data.get("last_used") if isinstance(profile_data, dict) else None

    return str(last_used).strip() or DEFAULT_CHROME_PROFILE_NAME


class ChatGPTWebSession:
    def __init__(
        self,
        *,
        profile_dir: Path = CHATGPT_PROFILE_DIR,
        profile_name: str | None = None,
        create_profile_dir: bool = True,
        downloads_dir: Path = CHATGPT_DOWNLOADS_DIR,
        chatgpt_url: str = CHATGPT_URL,
        timeout_seconds: int = 420,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser()
        self.profile_name = str(profile_name or "").strip()
        self.create_profile_dir = create_profile_dir
        self.downloads_dir = Path(downloads_dir).expanduser()
        self.chatgpt_url = chatgpt_url
        self.timeout_ms = max(60, int(timeout_seconds)) * 1000
        self.playwright: Any = None
        self.context: Any = None
        self.page: Any = None

    def __enter__(self) -> "ChatGPTWebSession":
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ChatGPTWebAutomationError("缺少 Playwright。請先執行 pip install -r requirements.txt。") from exc

        self.PlaywrightTimeoutError = PlaywrightTimeoutError
        if self.create_profile_dir:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        elif not self.profile_dir.exists():
            raise ChatGPTWebAutomationError(f"找不到 Chrome 使用者資料夾：{self.profile_dir}")
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                executable_path=str(find_chrome_executable()),
                headless=False,
                accept_downloads=True,
                downloads_path=str(self.downloads_dir),
                viewport={"width": 1440, "height": 1000},
                args=self._launch_args(),
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(self.chatgpt_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._wait_for_prompt_box()
        except ChatGPTWebAutomationError:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            raise ChatGPTWebAutomationError(self._format_startup_error(exc)) from exc

        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()

    def _launch_args(self) -> list[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        if self.profile_name:
            args.append(f"--profile-directory={self.profile_name}")

        return args

    def _profile_hint(self) -> str:
        if self.profile_name:
            return f"目前使用 Chrome 使用者資料夾「{self.profile_dir}」與 profile「{self.profile_name}」。"

        return f"目前使用工具專用 Chrome profile「{self.profile_dir}」。"

    def _format_startup_error(self, exc: Exception) -> str:
        error_text = str(exc)
        if _looks_like_profile_lock_error(error_text) and self.profile_name:
            return (
                "無法開啟你平常的 Chrome 登入資料，通常是因為一般 Chrome 還在使用同一個 profile。"
                "請先完全關閉 Chrome 後再重新生成；或在設定改用工具專用 Chrome profile 並登入一次。"
                f" {self._profile_hint()} 原始訊息：{error_text}"
            )

        return f"啟動或載入 ChatGPT 網頁版失敗：{error_text}"

    def generate_image(self, prompt: str, reference_image: Path | None = None) -> ChatGPTGeneratedImage:
        try:
            self._wait_for_prompt_box()

            if reference_image is not None:
                self._upload_reference_image(reference_image)
                time.sleep(3)

            existing_sources = self._collect_large_image_sources()
            existing_assistant_count = self._count_assistant_messages()
            self._fill_prompt(prompt)
            self._send_prompt()

            image_source = self._wait_for_new_image_source(existing_sources, existing_assistant_count)
            return ChatGPTGeneratedImage(
                image_bytes=self._read_image_source(image_source),
                source=image_source[:120],
            )
        except ChatGPTWebAutomationError:
            raise
        except Exception as exc:
            raise ChatGPTWebAutomationError(f"ChatGPT 網頁版操作失敗：{exc}") from exc

    def generate_text(self, prompt: str) -> str:
        try:
            self._wait_for_prompt_box()
            existing_texts = self._collect_assistant_texts()
            self._fill_prompt(prompt)
            self._send_prompt()
            return self._wait_for_new_assistant_text(existing_texts)
        except ChatGPTWebAutomationError:
            raise
        except Exception as exc:
            raise ChatGPTWebAutomationError(f"ChatGPT 網頁版文字潤飾失敗：{exc}") from exc

    def _prompt_locator(self) -> Any:
        selectors = [
            '#prompt-textarea[contenteditable="true"]:visible',
            '[data-testid="prompt-textarea"][contenteditable="true"]:visible',
            '[data-testid="prompt-textarea"] [contenteditable="true"]:visible',
            '[data-testid="prompt-textarea"]:visible',
            '[contenteditable="true"][role="textbox"]:visible',
            'div[contenteditable="true"]:visible',
            'textarea[name="prompt-textarea"]:visible',
            'textarea[aria-label*="ChatGPT"]:visible',
            'textarea[aria-label*="對話"]:visible',
            'textarea[placeholder*="Message"]:visible',
            'textarea[placeholder*="Ask"]:visible',
            'textarea[placeholder*="想問"]:visible',
            "textarea:visible",
        ]

        for selector in selectors:
            locator = self.page.locator(selector)
            if locator.count() > 0:
                return locator.last

        raise ChatGPTWebAutomationError("找不到 ChatGPT 訊息輸入框。")

    def _wait_for_prompt_box(self) -> None:
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        last_error = ""

        while time.monotonic() < deadline:
            try:
                locator = self._prompt_locator()
                locator.wait_for(state="visible", timeout=2000)
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1)

        raise ChatGPTWebAutomationError(
            "Chrome 已開啟 ChatGPT，但看不到可輸入訊息的位置。"
            "請在開啟的 Chrome 視窗登入 ChatGPT 或完成驗證後，再重新按一次生成按鈕。"
            f"{self._profile_hint()}"
            f" 原始訊息：{last_error}"
        )

    def _upload_reference_image(self, reference_image: Path) -> None:
        if not reference_image.exists() or not reference_image.is_file():
            raise ChatGPTWebAutomationError(f"找不到參考圖片：{reference_image}")

        file_input = self.page.locator('input[type="file"]')
        if file_input.count() == 0:
            for selector in [
                '[data-testid="composer-plus-btn"]:visible',
                'button[aria-label*="Attach"]:visible',
                'button[aria-label*="Upload"]:visible',
                'button[aria-label*="新增"]:visible',
            ]:
                button = self.page.locator(selector)
                if button.count() > 0:
                    button.first.click(timeout=3000)
                    time.sleep(1)
                    break

        file_input = self.page.locator('input[type="file"]')
        if file_input.count() == 0:
            raise ChatGPTWebAutomationError("找不到 ChatGPT 的檔案上傳欄位。")

        file_input.last.set_input_files(str(reference_image))

    def _fill_prompt(self, prompt: str) -> None:
        locator = self._prompt_locator()
        locator.click(timeout=5000)
        locator.fill(prompt, timeout=10000)

    def _send_prompt(self) -> None:
        for selector in [
            '[data-testid="send-button"]:visible',
            'button[aria-label*="Send"]:visible',
            'button[aria-label*="傳送"]:visible',
            'button[data-testid*="send"]:visible',
        ]:
            button = self.page.locator(selector)
            if button.count() > 0:
                try:
                    button.last.click(timeout=5000)
                    return
                except Exception:
                    continue

        self._prompt_locator().press("Enter", timeout=5000)

    def _collect_large_image_sources(self) -> set[str]:
        sources = self.page.evaluate(
            """
            () => Array.from(document.images)
                .filter((img) => img.naturalWidth >= 256 && img.naturalHeight >= 256)
                .map((img) => img.currentSrc || img.src)
                .filter(Boolean)
            """
        )
        return set(str(source) for source in sources)

    def _count_assistant_messages(self) -> int:
        return int(
            self.page.evaluate(
                """
                () => document.querySelectorAll('[data-message-author-role="assistant"]').length
                """
            )
        )

    def _collect_new_assistant_image_candidates(self, existing_assistant_count: int) -> list[dict[str, Any]]:
        candidates = self.page.evaluate(
            """
            (existingAssistantCount) => {
                const assistantMessages = Array.from(
                    document.querySelectorAll('[data-message-author-role="assistant"]')
                );
                let roots = assistantMessages.slice(existingAssistantCount);

                if (!assistantMessages.length) {
                    roots = [document];
                }

                return roots.flatMap((root) => Array.from(root.querySelectorAll("img")))
                    .filter((img) => {
                        const rect = img.getBoundingClientRect();
                        const style = window.getComputedStyle(img);
                        return img.naturalWidth >= 512
                            && img.naturalHeight >= 512
                            && rect.width > 0
                            && rect.height > 0
                            && style.visibility !== "hidden"
                            && style.display !== "none";
                    })
                    .map((img) => ({
                        src: img.currentSrc || img.src,
                        width: img.naturalWidth,
                        height: img.naturalHeight,
                        alt: img.alt || "",
                    }))
                    .filter((item) => item.src);
            }
            """,
            existing_assistant_count,
        )
        return list(candidates)

    def _collect_assistant_texts(self) -> list[str]:
        texts = self.page.evaluate(
            """
            () => Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'))
                .map((element) => (element.innerText || element.textContent || "").trim())
                .filter(Boolean)
            """
        )
        return [str(text).strip() for text in texts if str(text).strip()]

    def _is_response_generating(self) -> bool:
        for selector in [
            '[data-testid="stop-button"]:visible',
            'button[aria-label*="Stop"]:visible',
            'button[aria-label*="停止"]:visible',
            'button[data-testid*="stop"]:visible',
        ]:
            try:
                if self.page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue

        return False

    def _wait_for_new_assistant_text(self, existing_texts: list[str]) -> str:
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        existing_count = len(existing_texts)
        last_text = ""
        stable_since = 0.0

        while time.monotonic() < deadline:
            texts = self._collect_assistant_texts()
            new_texts = texts[existing_count:] if len(texts) > existing_count else []
            candidate = new_texts[-1].strip() if new_texts else ""

            if candidate and candidate != last_text:
                last_text = candidate
                stable_since = time.monotonic()
            elif candidate and stable_since and time.monotonic() - stable_since >= 4 and not self._is_response_generating():
                return candidate

            time.sleep(1)

        if last_text:
            return last_text

        raise ChatGPTWebAutomationError("等待 ChatGPT 文字回覆逾時。")

    def _wait_for_new_image_source(
        self,
        existing_sources: set[str],
        existing_assistant_count: int,
    ) -> str:
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        last_source = ""
        stable_since = 0.0

        while time.monotonic() < deadline:
            candidates = self._collect_new_assistant_image_candidates(existing_assistant_count)

            for candidate in reversed(candidates):
                source = str(candidate["src"])
                if source not in existing_sources and not _looks_like_ui_image(source, str(candidate.get("alt", ""))):
                    if source != last_source:
                        last_source = source
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= 2 and not self._is_response_generating():
                        return source

            time.sleep(2)

        raise ChatGPTWebAutomationError("等待 ChatGPT 生成圖片逾時。")

    def _read_image_source(self, image_source: str) -> bytes:
        errors: list[str] = []

        if image_source.startswith("data:"):
            try:
                return _decode_data_url(image_source)
            except ValueError as exc:
                errors.append(f"data URL 解碼失敗：{exc}")

        if image_source.startswith(("http://", "https://")):
            try:
                response = self.context.request.get(
                    image_source,
                    headers={"referer": self.chatgpt_url},
                    timeout=30000,
                )
                body = response.body()
                content_type = response.headers.get("content-type", "")
                if response.ok and _looks_like_image_response(body, content_type):
                    return body

                errors.append(f"request 讀圖失敗：HTTP {response.status}, content-type={content_type or 'unknown'}")
            except Exception as exc:
                errors.append(f"request 讀圖失敗：{exc}")

        for reader_name, script in [
            (
                "fetch",
                """
                async (src) => {
                    const response = await fetch(src, { credentials: "include", cache: "no-store" });
                    if (!response.ok) {
                        throw new Error(`Image fetch failed: ${response.status}`);
                    }
                    const blob = await response.blob();
                    return await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = () => reject(reader.error);
                        reader.readAsDataURL(blob);
                    });
                }
                """,
            ),
            (
                "xhr",
                """
                async (src) => await new Promise((resolve, reject) => {
                    const request = new XMLHttpRequest();
                    request.open("GET", src);
                    request.responseType = "blob";
                    request.onload = () => {
                        if (request.status !== 0 && (request.status < 200 || request.status >= 300)) {
                            reject(new Error(`Image XHR failed: ${request.status}`));
                            return;
                        }
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = () => reject(reader.error);
                        reader.readAsDataURL(request.response);
                    };
                    request.onerror = () => reject(new Error("Image XHR network error"));
                    request.send();
                })
                """,
            ),
            (
                "canvas",
                """
                async (src) => {
                    const images = Array.from(document.images);
                    const image = images.reverse().find((img) => (img.currentSrc || img.src) === src);
                    if (!image) {
                        throw new Error("Image element not found");
                    }
                    if (!image.complete) {
                        await image.decode();
                    }
                    const width = image.naturalWidth || image.width;
                    const height = image.naturalHeight || image.height;
                    if (!width || !height) {
                        throw new Error("Image has no rendered dimensions");
                    }
                    const canvas = document.createElement("canvas");
                    canvas.width = width;
                    canvas.height = height;
                    const context = canvas.getContext("2d");
                    context.drawImage(image, 0, 0, width, height);
                    return canvas.toDataURL("image/png");
                }
                """,
            ),
        ]:
            try:
                data_url = self.page.evaluate(script, image_source)
                return _decode_data_url(data_url)
            except Exception as exc:
                errors.append(f"{reader_name} 讀圖失敗：{exc}")

        try:
            return self._screenshot_image_source(image_source)
        except Exception as exc:
            errors.append(f"元素截圖失敗：{exc}")

        raise ChatGPTWebAutomationError(
            "ChatGPT 已產生圖片，但自動讀取圖片失敗。"
            "請重試一次；若仍失敗，可能是 ChatGPT 暫時限制圖片來源讀取。"
            f" 細節：{' / '.join(errors[-3:])}"
        )

    def _screenshot_image_source(self, image_source: str) -> bytes:
        image_index = self.page.evaluate(
            """
            (src) => {
                const images = Array.from(document.images);
                for (let index = images.length - 1; index >= 0; index -= 1) {
                    const image = images[index];
                    const rect = image.getBoundingClientRect();
                    const style = window.getComputedStyle(image);
                    if (
                        (image.currentSrc || image.src) === src
                        && image.naturalWidth >= 128
                        && image.naturalHeight >= 128
                        && rect.width > 0
                        && rect.height > 0
                        && style.visibility !== "hidden"
                        && style.display !== "none"
                    ) {
                        return index;
                    }
                }
                return -1;
            }
            """,
            image_source,
        )
        if image_index < 0:
            raise ChatGPTWebAutomationError("找不到可截圖的生成圖片元素。")

        return self.page.locator("img").nth(image_index).screenshot(timeout=15000)


def _decode_data_url(data_url: str) -> bytes:
    if not isinstance(data_url, str) or "," not in data_url:
        raise ValueError("不是有效的 data URL")

    header, payload = data_url.split(",", 1)
    if ";base64" in header:
        return base64.b64decode(payload)

    return unquote_to_bytes(payload)


def _looks_like_image_response(body: bytes, content_type: str) -> bool:
    if content_type.lower().startswith("image/"):
        return True

    return body.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"RIFF",
        )
    )


def _looks_like_ui_image(source: str, alt_text: str) -> bool:
    combined = f"{source} {alt_text}".lower()
    blocked_markers = [
        "avatar",
        "favicon",
        "logo",
        "sprite",
        "profile",
        "emoji",
        "placeholder",
    ]

    return any(marker in combined for marker in blocked_markers)


def _looks_like_profile_lock_error(error_text: str) -> bool:
    lowered = error_text.lower()
    markers = [
        "processsingleton",
        "singletonlock",
        "profile appears to be in use",
        "user data directory is already in use",
        "another browser is running",
    ]

    return any(marker in lowered for marker in markers)
