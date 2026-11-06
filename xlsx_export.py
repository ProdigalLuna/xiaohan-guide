# -*- coding: utf-8 -*-
"""纯标准库生成 Excel（.xlsx）对比表格，供用户下载到本地用 Excel / WPS 打开。

不依赖第三方库，直接按 OOXML 规范打包 zip。
"""

import os
import re
import time
import zipfile

XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

XLSX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="商品对比" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="¥#,##0"/></numFmts>
<fonts count="3">
<font><sz val="11"/><color rgb="FF12202E"/><name val="Microsoft YaHei"/></font>
<font><b/><sz val="11"/><color rgb="FF12202E"/><name val="Microsoft YaHei"/></font>
<font><b/><sz val="14"/><color rgb="FFC85100"/><name val="Microsoft YaHei"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFF4EC"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD9DEE8"/></left><right style="thin"><color rgb="FFD9DEE8"/></right><top style="thin"><color rgb="FFD9DEE8"/></top><bottom style="thin"><color rgb="FFD9DEE8"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def col_letter(index):
    """1 -> A, 27 -> AA"""
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xml_escape(value):
    return (str(value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _cell(ref, value, style):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = _xml_escape(value).replace("\n", "&#10;")
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(model):
    headers = model.get("headers") or []
    rows = model.get("rows") or []
    notes = model.get("notes") or []
    title = model.get("title") or "商品对比表"
    width = max(len(headers), max((len(r) for r in rows), default=0))
    width = max(width, 2)

    lines = []
    # 标题
    lines.append(
        '<row r="1" ht="26" customHeight="1">'
        + _cell(f"A1", title, 3) + "</row>"
    )
    # 表头
    r = 2
    if headers:
        cells = "".join(_cell(f"{col_letter(i + 1)}{r}", h, 1) for i, h in enumerate(headers))
        lines.append(f'<row r="{r}" ht="22" customHeight="1">{cells}</row>')
        r += 1
    # 数据行
    for row in rows:
        cells = []
        for i, value in enumerate(row):
            cells.append(_cell(f"{col_letter(i + 1)}{r}", value, 2))
        lines.append(f'<row r="{r}" ht="20" customHeight="1">{"".join(cells)}</row>')
        r += 1

    # 结论文本
    if notes:
        r += 1
        lines.append(f'<row r="{r}" ht="22" customHeight="1">{_cell(f"A{r}", "选购结论", 3)}</row>')
        r += 1
        for note in notes:
            lines.append(
                f'<row r="{r}" ht="20" customHeight="1">'
                + _cell(f"A{r}", note, 4)
                + "</row>"
            )
            r += 1

    r += 1
    lines.append(
        f'<row r="{r}">{_cell(f"A{r}", "数据说明：价格与参数来自商品页/站内公开标价，实际以下单页面为准；缺失项已标注【暂无公开参数】。", 4)}</row>'
    )

    cols = ""
    if width:
        widths = [16, 12] + [30] * (width - 2)
        cols = "<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{widths[i] if i < len(widths) else 26}" customWidth="1"/>'
            for i in range(width)
        ) + "</cols>"

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f"{cols}<sheetData>{''.join(lines)}</sheetData>"
        '<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )


def write_xlsx(model, path):
    """把 {title, headers, rows, notes} 写成 xlsx。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", XLSX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", XLSX_ROOT_RELS)
        zf.writestr("xl/workbook.xml", XLSX_WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", XLSX_WORKBOOK_RELS)
        zf.writestr("xl/styles.xml", XLSX_STYLES)
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(model))
    return path


def safe_filename(name, fallback="商品对比表.xlsx"):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip()) or fallback
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return name[:80]


def new_filename():
    return time.strftime("compare_%Y%m%d_%H%M%S_") + os.urandom(4).hex() + ".xlsx"
