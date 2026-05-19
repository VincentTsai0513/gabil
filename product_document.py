from __future__ import annotations

from datetime import datetime
import re
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from file_manager import sanitize_filename


PRODUCT_FIELD_LABELS = {
    "product_features": "產品特色",
    "product_specifications": "產品規格",
    "product_contents": "產品內容",
}


def build_product_document_prompt(project_name: str, product_reference: dict[str, str]) -> str:
    fields = []
    for key, label in PRODUCT_FIELD_LABELS.items():
        value = str(product_reference.get(key, "") or "").strip()
        fields.append(f"{label}：\n{value or '（未提供）'}")

    return "\n\n".join(
        [
            "你是一位繁體中文電商商品文案編輯，請把以下產品資料潤飾成可直接放入 Word 的商品文案。",
            "重要規則：只能根據提供資料改寫，不要杜撰未提供的規格、數值、認證、材質、保固、贈品或功能。",
            "請保留品牌、型號、規格數字與專有名詞；每點要短而具體，語氣專業、清楚、適合商品頁與型錄。",
            "只輸出下面三個段落，不要加主標題、摘要、說明、結語或其他段落。",
            "每個項目必須用全形符號「・」開頭；如果同一段有更多項目，就繼續往下列。",
            "輸出格式必須完全像這樣：",
            "產品特色▼",
            "・IP67防水防塵",
            "",
            "產品規格▼",
            "・功率:10W",
            "",
            "產品內容▼",
            "・主機x1",
            "",
            f"專案：{project_name}",
            "原始資料：",
            "\n\n".join(fields),
        ]
    )


def write_product_document(
    *,
    project_name: str,
    polished_text: str,
    product_reference: dict[str, str],
    project_root: Path,
) -> Path:
    project_root.mkdir(parents=True, exist_ok=True)
    title = f"{project_name}產品特色和規格"
    filename = sanitize_filename(title, fallback="產品特色和規格", max_length=120) + ".docx"
    target_path = _unique_docx_path(project_root / filename)
    document_xml = _build_document_xml(project_name, polished_text, product_reference)

    with ZipFile(target_path, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", _content_types_xml())
        docx.writestr("_rels/.rels", _root_rels_xml())
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/styles.xml", _styles_xml())
        docx.writestr("word/numbering.xml", _numbering_xml())
        docx.writestr("word/settings.xml", _settings_xml())
        docx.writestr("docProps/core.xml", _core_xml(title))
        docx.writestr("docProps/app.xml", _app_xml())
        docx.writestr("word/_rels/document.xml.rels", _document_rels_xml())

    return target_path


def _unique_docx_path(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _build_document_xml(project_name: str, polished_text: str, product_reference: dict[str, str]) -> str:
    sections = _normalize_product_sections(polished_text, product_reference)
    blocks: list[str] = []

    for index, (label, items) in enumerate(sections):
        if index:
            blocks.append(_paragraph("", style="Spacer"))
        blocks.append(_paragraph(f"{label}▼", style="Heading1"))
        blocks.extend(_paragraph(f"・{item}") for item in items)

    body = "\n".join(blocks)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
{body}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""


def _normalize_product_sections(
    polished_text: str,
    product_reference: dict[str, str],
) -> list[tuple[str, list[str]]]:
    sections_from_text = _extract_sections(polished_text)
    normalized_sections: list[tuple[str, list[str]]] = []

    for key, label in PRODUCT_FIELD_LABELS.items():
        items = sections_from_text.get(label) or _split_reference_items(product_reference.get(key, ""))
        normalized_sections.append((label, items))

    return normalized_sections


def _extract_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {label: [] for label in PRODUCT_FIELD_LABELS.values()}
    active_label = ""

    for raw_line in _strip_code_fences(text).splitlines():
        line = _clean_markdown_text(raw_line.strip())
        if not line:
            continue

        label = _section_label_from_line(line)
        if label:
            active_label = label
            continue

        if not active_label:
            continue

        item = _clean_item_text(line)
        if item:
            sections[active_label].append(item)

    return {label: items for label, items in sections.items() if items}


def _section_label_from_line(line: str) -> str:
    cleaned = re.sub(r"^[#\s]+", "", line).strip()
    cleaned = cleaned.rstrip("▼：:").strip()

    for label in PRODUCT_FIELD_LABELS.values():
        if cleaned == label:
            return label

    return ""


def _split_reference_items(value: object) -> list[str]:
    items: list[str] = []

    for raw_line in str(value or "").splitlines():
        item = _clean_item_text(raw_line.strip())
        if item:
            items.append(item)

    return items


def _clean_item_text(line: str) -> str:
    line = _clean_markdown_text(line)
    line = re.sub(r"^[-*•・\u2022]\s*", "", line)
    line = re.sub(r"^\d+[.)、]\s*", "", line)
    return line.strip()


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:text|markdown|md)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_markdownish(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    previous_blank = False

    for raw_line in text.splitlines():
        line = _clean_markdown_text(raw_line.strip())
        if not line:
            if not previous_blank:
                blocks.append(("space", ""))
            previous_blank = True
            continue

        previous_blank = False
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            blocks.append(("heading1" if level <= 2 else "heading2", heading_match.group(2).strip()))
            continue

        bullet_match = re.match(r"^[-*•・]\s*(.+)$", line)
        if bullet_match:
            blocks.append(("bullet", bullet_match.group(1).strip()))
            continue

        number_match = re.match(r"^\d+[.)、]\s*(.+)$", line)
        if number_match:
            blocks.append(("number", number_match.group(1).strip()))
            continue

        if _looks_like_heading(line):
            blocks.append(("heading1", line.rstrip("：:").strip()))
            continue

        blocks.append(("body", line))

    return blocks


def _clean_markdown_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    return text.strip()


def _looks_like_heading(text: str) -> bool:
    normalized = text.rstrip("：:").strip()
    known_headings = {
        "產品賣點摘要",
        "產品特色",
        "產品規格",
        "產品內容",
        "商品頁文案建議",
        "文案建議",
    }
    return normalized in known_headings


def _paragraph(text: str, style: str = "Normal", num_id: int | None = None) -> str:
    ppr_parts = []
    if style and style != "Normal":
        ppr_parts.append(f'<w:pStyle w:val="{style}"/>')
    if num_id is not None:
        ppr_parts.append(
            f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
        )

    ppr = f"<w:pPr>{''.join(ppr_parts)}</w:pPr>" if ppr_parts else ""
    safe_text = escape(text)
    return f'    <w:p>{ppr}<w:r><w:t xml:space="preserve">{safe_text}</w:t></w:r></w:p>'


def _content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _document_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft JhengHei"/>
        <w:sz w:val="22"/>
        <w:szCs w:val="22"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="160" w:line="276" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft JhengHei"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="0" w:after="180"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="34"/><w:szCs w:val="34"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120"/></w:pPr>
    <w:rPr><w:color w:val="5B677A"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Meta">
    <w:name w:val="Meta"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="280"/></w:pPr>
    <w:rPr><w:color w:val="6B7280"/><w:sz w:val="18"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="0F172A"/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:keepNext/><w:spacing w:before="180" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="334155"/><w:sz w:val="23"/><w:szCs w:val="23"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Spacer">
    <w:name w:val="Spacer"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="40"/></w:pPr>
    <w:rPr><w:sz w:val="6"/></w:rPr>
  </w:style>
</w:styles>"""


def _numbering_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="•"/>
      <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
      <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""


def _settings_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:compat/>
</w:settings>"""


def _core_xml(title: str) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_title = escape(title)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{safe_title}</dc:title>
  <dc:creator>AI 圖片批次 Prompt 管理器</dc:creator>
  <cp:lastModifiedBy>AI 圖片批次 Prompt 管理器</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>"""


def _app_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>AI 圖片批次 Prompt 管理器</Application>
</Properties>"""
