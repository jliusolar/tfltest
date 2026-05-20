from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import math
import mimetypes
import os
import re
import sqlite3
import subprocess
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
SAMPLES_DIR = ROOT / "samples"
DB_PATH = DATA_DIR / "tlf_assistant.sqlite3"
APP_VERSION = "0.2.13"
LOCAL_ENV_PATH = ROOT / "local.env"
OUTPUT_EXTENSIONS = {".rtf", ".txt", ".lst", ".html", ".htm", ".pdf", ".xlsx", ".docx"}
PROGRAM_EXTENSIONS = {".sas"}
SCAN_PROGRESS: dict[str, dict[str, Any]] = {}
SCAN_PROGRESS_LOCK = threading.Lock()


def load_local_env() -> None:
    if not LOCAL_ENV_PATH.exists():
        return
    for raw_line in LOCAL_ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "per",
    "the",
    "to",
    "with",
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists examples (
                id integer primary key autoincrement,
                study_id text,
                tlf_number text,
                tlf_type text,
                title text,
                population text,
                endpoint text,
                treatment_structure text,
                source_datasets text,
                dataset_path text,
                macros text,
                notes text,
                program_name text,
                program_text text,
                output_name text,
                output_text text,
                shell_document_path text,
                shell_name text,
                shell_text text,
                shell_blob blob,
                mddt_path text,
                mddt_name text,
                mddt_text text,
                mddt_blob blob,
                extracted_json text,
                created_at text not null
            );

            create table if not exists generation_runs (
                id integer primary key autoincrement,
                shell_name text,
                shell_text text,
                shell_json text,
                generated_program text,
                generated_program_path text,
                generation_method text,
                retrieval_json text,
                validation_json text,
                created_at text not null
            );

            create index if not exists idx_examples_tlf_type on examples(tlf_type);
            create index if not exists idx_examples_tlf_number on examples(tlf_number);
            """
        )
        ensure_columns(
            conn,
            "examples",
            {
                "dataset_path": "text",
                "shell_document_path": "text",
                "shell_blob": "blob",
                "mddt_path": "text",
                "mddt_name": "text",
                "mddt_text": "text",
                "mddt_blob": "blob",
            },
        )
        ensure_columns(
            conn,
            "generation_runs",
            {
                "generated_program_path": "text",
                "generation_method": "text",
            },
        )


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"alter table {table} add column {name} {definition}")


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def decode_uploaded_file(file_obj: dict[str, Any] | None) -> tuple[str, bytes, str]:
    if not file_obj:
        return "", b"", ""
    name = clean_filename(str(file_obj.get("name") or "uploaded.txt"))
    data = file_obj.get("content_base64") or ""
    if "," in data and data.strip().lower().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raw = str(data).encode("utf-8", errors="replace")
    return name, raw, extract_text(name, raw)


def decode_uploaded_file_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    raw_items = value if isinstance(value, list) else [value]
    files: list[dict[str, Any]] = []
    for file_obj in raw_items:
        name, raw, text = decode_uploaded_file(file_obj)
        if name and raw:
            files.append({"name": name, "raw": raw, "text": text})
    return files


def read_text_from_path(path_value: str) -> tuple[str, str, str]:
    if not path_value:
        return "", "", ""
    path = resolve_user_path(path_value)
    if not path.exists() or not path.is_file():
        return str(path), "", ""
    raw = path.read_bytes()
    return str(path), path.name, extract_text(str(path), raw)


def clean_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name) or "uploaded.txt"


def extract_text(name: str, raw: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if not raw:
        return ""
    if suffix == ".docx":
        return extract_docx_text(raw)
    if suffix == ".xlsx":
        return extract_xlsx_text(raw)
    if suffix == ".rtf":
        return strip_rtf(decode_bytes(raw))
    if suffix == ".pdf":
        return extract_pdf_text(raw)
    if suffix in {".htm", ".html"}:
        return strip_html(decode_bytes(raw))
    return decode_bytes(raw)


def extract_first_page_text(name: str, raw: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if not raw:
        return ""
    if suffix == ".rtf":
        return strip_rtf(first_page_rtf(decode_bytes(raw)))
    if suffix in {".txt", ".lst", ".log", ".csv"}:
        return first_page_plain_text(decode_bytes(raw))
    if suffix in {".htm", ".html"}:
        return strip_html(first_page_html(decode_bytes(raw)))
    if suffix == ".pdf":
        return first_page_plain_text(extract_pdf_text(raw))
    if suffix == ".docx":
        return first_page_plain_text(extract_docx_text(raw))
    if suffix == ".xlsx":
        return first_page_plain_text(extract_xlsx_text(raw))
    return first_page_plain_text(decode_bytes(raw))


def first_page_rtf(text: str) -> str:
    parts = re.split(r"\\page\b|\\sect\b", text, maxsplit=1, flags=re.IGNORECASE)
    return parts[0] if parts else text[:30000]


def first_page_html(text: str) -> str:
    parts = re.split(
        r"(?i)page-break-(?:after|before)\s*:\s*always|class\s*=\s*['\"][^'\"]*page-break",
        text,
        maxsplit=1,
    )
    return parts[0] if parts else text[:50000]


def first_page_plain_text(text: str) -> str:
    if "\f" in text:
        text = text.split("\f", 1)[0]
    lines = text.splitlines()
    if len(lines) > 120:
        lines = lines[:120]
    return normalize_text("\n".join(lines))[:30000]


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_docx_text(raw: bytes) -> str:
    lines: list[str] = []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp_path) as archive:
            xml_names = [
                item
                for item in archive.namelist()
                if item.startswith("word/") and item.endswith(".xml")
            ]
            for xml_name in xml_names:
                try:
                    root = ElementTree.fromstring(archive.read(xml_name))
                except ElementTree.ParseError:
                    continue
                text_bits = [
                    node.text or ""
                    for node in root.iter()
                    if node.tag.endswith("}t") or node.tag == "t"
                ]
                if text_bits:
                    lines.append(" ".join(text_bits))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return "\n".join(line for line in lines if line.strip())


def extract_xlsx_text(raw: bytes) -> str:
    pieces: list[str] = []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp_path) as archive:
            for name in archive.namelist():
                if not (
                    name == "xl/sharedStrings.xml"
                    or name.startswith("xl/worksheets/")
                    and name.endswith(".xml")
                ):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError:
                    continue
                values = [
                    node.text or ""
                    for node in root.iter()
                    if node.tag.endswith("}t")
                    or node.tag.endswith("}v")
                    or node.tag == "t"
                    or node.tag == "v"
                ]
                if values:
                    pieces.append(" ".join(values))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return "\n".join(item for item in pieces if item.strip())


def strip_rtf(text: str) -> str:
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\tab", " ", text)
    text = re.sub(r"[{}]", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = re.sub(r"\\.", " ", text)
    return normalize_text(text)


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(normalize_text(text))


def extract_pdf_text(raw: bytes) -> str:
    # This is intentionally conservative: enough for simple text PDFs, while the UI
    # labels PDF support as best-effort until a dedicated parser is configured.
    text = decode_bytes(raw)
    strings = []
    for match in re.finditer(r"\(((?:[^()]|\\.){2,})\)", text):
        value = match.group(1)
        cleaned = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
        cleaned = re.sub(r"\\[nrtbf]", " ", cleaned)
        if sum(ch.isprintable() for ch in cleaned) >= max(3, len(cleaned) * 0.7):
            strings.append(cleaned)
    fallback = re.sub(r"[^A-Za-z0-9 .,;:_/\-()%\n]+", " ", text)
    return normalize_text("\n".join(strings) if strings else fallback[:50000])


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def parse_sas_program(program_text: str) -> dict[str, Any]:
    title_matches = re.findall(
        r"(?im)^\s*title\d*\s+(?:'([^']*)'|\"([^\"]*)\")\s*;",
        program_text,
    )
    footnote_matches = re.findall(
        r"(?im)^\s*footnote\d*\s+(?:'([^']*)'|\"([^\"]*)\")\s*;",
        program_text,
    )
    titles = [a or b for a, b in title_matches if a or b]
    footnotes = [a or b for a, b in footnote_matches if a or b]

    datasets: set[str] = set()
    for match in re.findall(
        r"(?is)\b(?:set|merge|update)\s+([^;]+);|(?:data|out)\s*=\s*([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)",
        program_text,
    ):
        for part in match:
            if not part:
                continue
            for token in re.findall(r"[A-Za-z_][\w.]*", part):
                if token.lower() not in {"where", "keep", "drop", "rename", "in", "by"}:
                    datasets.add(token)

    macros = sorted(
        {
            value
            for value in re.findall(r"%([A-Za-z_]\w*)\s*(?:\(|;)", program_text)
            if value.lower()
            not in {"do", "end", "if", "then", "else", "let", "put", "sysfunc", "scan"}
        },
        key=str.lower,
    )
    library_datasets = extract_library_dataset_refs(program_text)

    return {
        "titles": titles,
        "footnotes": footnotes,
        "datasets": sorted(datasets, key=str.lower),
        "library_datasets": library_datasets,
        "macros": macros,
    }


def extract_library_dataset_refs(program_text: str) -> list[str]:
    refs: set[str] = set()
    for libref, dataset in re.findall(
        r"(?i)\b(sdtm|adam|adm)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b",
        program_text,
    ):
        refs.add(f"{libref.lower()}.{dataset.lower()}")
    return sorted(refs)


def parse_shell_text(text: str, filename: str = "") -> dict[str, Any]:
    normalized = normalize_text(text)
    lines = [line for line in normalized.split("\n") if line.strip()]
    joined = "\n".join(lines)
    lower = joined.lower()

    tlf_type = "table"
    if re.search(r"\blisting\b|\blist of\b", lower):
        tlf_type = "listing"
    if re.search(r"\bfigure\b|\bplot\b|\bgraph\b", lower):
        tlf_type = "figure"

    tlf_number = ""
    number_match = re.search(
        r"\b(?:table|listing|figure)?\s*(\d{1,2}(?:\.\d+){1,5})\b",
        joined,
        flags=re.IGNORECASE,
    )
    if number_match:
        tlf_number = number_match.group(1)

    title = ""
    tlf_id_pattern = re.compile(r"(?i)^(table|listing|figure)\s+\d{1,2}(?:\.\d+){1,5}\s*$")
    for line in lines[:15]:
        if tlf_id_pattern.match(line):
            continue
        candidate = re.sub(r"(?i)^title\d*\s*[:.-]?\s*", "", line).strip()
        if len(candidate) > 8 and (
            re.search(r"(?i)\b(table|listing|figure|summary|analysis|characteristics|events|subjects)\b", candidate)
            or not title
        ):
            title = candidate
            if re.search(r"(?i)\b(summary|listing|figure|table)\b", candidate):
                break
    if not title and lines:
        title = lines[0]

    population = ""
    population_match = re.search(
        r"(?i)\b((?:safety|intent[- ]to[- ]treat|itt|full analysis|fas|efficacy|per protocol|pk|pharmacokinetic|all treated)[A-Za-z -]*population)\b",
        joined,
    )
    if population_match:
        population = population_match.group(1)

    footnotes: list[str] = []
    for line in lines:
        if re.match(r"(?i)^(footnote|note)\s*\d*\s*[:.-]", line):
            footnotes.append(re.sub(r"(?i)^(footnote|note)\s*\d*\s*[:.-]\s*", "", line).strip())

    treatment_terms = re.findall(
        r"(?i)\b(placebo|total|overall|active|control|drug\s*[A-Z0-9-]*|[A-Z0-9-]+\s*mg|dose\s*\d+|low dose|high dose)\b",
        joined,
    )
    columns = unique_preserve([clean_label(term) for term in treatment_terms])

    for line in lines:
        if "|" in line and len(line.split("|")) >= 2:
            parts = [clean_label(part) for part in line.split("|") if clean_label(part)]
            if len(parts) >= 2:
                columns = unique_preserve(parts[1:] if len(parts) > 2 else parts)
                break

    statistics_catalog = [
        ("n", r"\bn\b"),
        ("mean", r"\bmean\b"),
        ("sd", r"\bsd\b|standard deviation"),
        ("median", r"\bmedian\b"),
        ("min", r"\bmin(?:imum)?\b"),
        ("max", r"\bmax(?:imum)?\b"),
        ("q1", r"\bq1\b|first quartile"),
        ("q3", r"\bq3\b|third quartile"),
        ("percent", r"\bpercent(?:age)?\b|%"),
        ("count", r"\bcount\b|frequency"),
    ]
    statistics = [name for name, pattern in statistics_catalog if re.search(pattern, lower)]

    rows: list[str] = []
    skip_patterns = re.compile(
        r"(?i)^(table|listing|figure|title|footnote|note|population|columns?|treatment|source|adam|dataset|page|program|output)\b"
    )
    for line in lines:
        if "|" in line:
            continue
        label = clean_label(re.sub(r"^\d+[\).]\s*", "", line))
        if not label or skip_patterns.search(label):
            continue
        if 2 <= len(label) <= 80 and not re.search(r"https?://|\.sas|\.rtf|\.pdf", label, re.I):
            rows.append(label)

    rows = [
        row
        for row in unique_preserve(rows)
        if row.lower() not in {title.lower(), population.lower(), "n", "mean", "sd"}
    ][:50]

    return {
        "filename": filename,
        "tlf_type": tlf_type,
        "tlf_number": tlf_number,
        "title": title,
        "population": population,
        "columns": columns[:12],
        "rows": rows,
        "statistics": statistics,
        "footnotes": footnotes[:12],
    }


HEADER_ID_PATTERN = r"(?P<header_id>\d+[A-Za-z]?|[A-Za-z])"
HEADER_DEF_RE = re.compile(
    rf"^\s*(?:treatment\s*)?(?:columns?\s*)?header\s*[:#.\-]?\s*{HEADER_ID_PATTERN}\b\s*[:.)\-]?\s*(?P<body>.*)$",
    re.IGNORECASE,
)
HEADER_REF_RE = re.compile(
    rf"\b(?:use\s+)?(?:treatment\s+)?(?:columns?\s+)?header\s*[:#.\-]?\s*{HEADER_ID_PATTERN}\b",
    re.IGNORECASE,
)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ElementTree.register_namespace("w", W_NS)


def parse_shell_agent(payload: dict[str, Any]) -> dict[str, Any]:
    shell_document_path = str(payload.get("shell_document_path") or "").strip()
    output_dir_text = str(payload.get("output_dir") or "").strip()
    top_k = max(1, min(int(payload.get("top_k") or 5), 10))
    use_llm = as_bool(payload.get("use_llm", True))
    uploaded_name, uploaded_raw, _ = decode_uploaded_file(payload.get("shell_file"))
    source_path: Path | None = None
    source_name = uploaded_name
    source_mode = "uploaded" if uploaded_raw else "path"
    raw = uploaded_raw

    if shell_document_path:
        source_path = resolve_user_path(shell_document_path)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError(f"Shell document does not exist or is not a file: {source_path}")
        source_name = source_path.name
        raw = source_path.read_bytes()
        source_mode = "path"
    elif not uploaded_raw:
        raise ValueError("Upload a shell document or provide the original shell document path.")

    suffix = Path(source_name).suffix.lower()
    if suffix not in {".docx", ".txt", ".rtf"}:
        raise ValueError("Clean Shell Agent supports .docx, .txt, and .rtf files in this version.")

    lines = read_shell_document_lines_from_raw(source_name, raw)
    shell_text = "\n".join(lines)
    heuristic_plan = build_clean_shell_plan(lines)
    retrieved = retrieve_examples(shell_text, "", top_k=top_k)
    output_shape_examples = load_output_shape_examples()
    llm_plan = generate_clean_shell_with_llm(
        source_name,
        shell_text,
        heuristic_plan,
        retrieved,
        use_llm=use_llm,
    )
    plan = finalize_clean_shell_plan(lines, heuristic_plan, llm_plan, output_shape_examples)
    clean_path = clean_shell_output_path(source_path, source_name, output_dir_text)
    clean_path.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".docx":
        write_docx_from_lines(clean_path, plan["cleaned_lines"])
    elif suffix == ".rtf":
        clean_path.write_text(lines_to_rtf(plan["cleaned_lines"]), encoding="utf-8")
    else:
        clean_path.write_text("\n".join(plan["cleaned_lines"]) + "\n", encoding="utf-8")

    if source_mode == "uploaded" and not output_dir_text:
        plan["warnings"].append(
            "Uploaded files do not include their original folder path; clean shell was saved under runs/clean_shells."
        )

    return {
        "source_path": str(source_path) if source_path else "",
        "source_name": source_name,
        "source_mode": source_mode,
        "clean_path": str(clean_path),
        "line_count": len(lines),
        "header_count": len(plan["headers"]),
        "applied_count": len(plan["applications"]),
        "output_count": len(plan.get("outputs", [])),
        "retrieved_count": len(retrieved),
        "method": plan.get("method", ""),
        "treatment_section": {
            "start_line": line_number(plan.get("treatment_start")),
            "end_line": line_number(plan.get("tfl_start")),
        },
        "headers": [
            {
                "id": header_id,
                "line_count": len(header["lines"]),
                "preview": " | ".join(header["lines"][:4]),
            }
            for header_id, header in plan["headers"].items()
        ],
        "applications": [
            {
                "line": line_number(item["line_index"]),
                "header_id": item["header_id"],
                "context": item["context"],
            }
            for item in plan["applications"]
        ],
        "outputs": plan.get("outputs", []),
        "retrieved": [public_example(item) for item in retrieved],
        "warnings": plan["warnings"],
        "preview": "\n".join(plan["cleaned_lines"][:80]),
    }


def refine_clean_shell(payload: dict[str, Any]) -> dict[str, Any]:
    instruction = str(payload.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("Enter a refinement instruction for the clean shell.")

    clean_path_text = str(payload.get("clean_path") or "").strip()
    clean_text = str(payload.get("clean_text") or "").strip()
    conversation = normalize_chat_history(payload.get("conversation") or [])
    clean_path: Path | None = None
    source_name = str(payload.get("source_name") or "").strip() or "clean_shell.txt"

    if clean_path_text:
        clean_path = resolve_user_path(clean_path_text)
        source_name = clean_path.name
        if clean_path.exists() and clean_path.is_file():
            clean_lines = read_shell_document_lines(clean_path)
        elif clean_text:
            clean_lines = normalize_clean_lines(clean_text.splitlines())
        else:
            raise ValueError(f"Clean shell file does not exist: {clean_path}")
    elif clean_text:
        clean_lines = normalize_clean_lines(clean_text.splitlines())
    else:
        raise ValueError("Create a clean shell before using the refinement chat.")

    current_text = "\n".join(clean_lines)
    llm_result = refine_clean_shell_locally(clean_lines, instruction)
    if not llm_result:
        retrieved = retrieve_examples(f"{current_text}\n{instruction}", "", top_k=5)
        try:
            llm_result = generate_refined_clean_shell_with_llm(
                source_name,
                current_text,
                instruction,
                conversation,
                retrieved,
            )
        except ValueError as exc:
            message = str(exc)
            if is_openai_quota_or_rate_limit_message(message):
                llm_result = clean_shell_no_change_result(clean_lines, message)
            else:
                raise
    else:
        retrieved = []
    refined_lines = normalize_clean_lines(llm_result.get("cleaned_lines") or [])
    if not refined_lines:
        raise ValueError("LLM did not return clean shell lines.")

    if clean_path:
        write_clean_shell_lines(clean_path, refined_lines)

    assistant_message = str(llm_result.get("message") or "Applied the requested clean-shell refinement.").strip()
    updated_conversation = normalize_chat_history(
        conversation
        + [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": assistant_message},
        ]
    )

    return {
        "clean_path": str(clean_path) if clean_path else "",
        "line_count": len(refined_lines),
        "method": llm_result.get("method", "llm"),
        "message": assistant_message,
        "warnings": llm_result.get("warnings") or [],
        "conversation": updated_conversation,
        "retrieved": [public_example(item) for item in retrieved],
        "preview": "\n".join(refined_lines[:120]),
    }


def is_openai_quota_or_rate_limit_message(message: str) -> bool:
    key = message.lower()
    return any(
        phrase in key
        for phrase in (
            "429",
            "too many requests",
            "rate limit",
            "quota",
            "billing",
            "insufficient_quota",
            "exceeded your current quota",
        )
    )


def clean_shell_no_change_result(clean_lines: list[str], error_message: str) -> dict[str, Any]:
    return {
        "method": "llm_refine_unavailable_no_change",
        "cleaned_lines": clean_lines,
        "message": (
            "OpenAI quota or rate limit prevented this LLM edit, so no clean-shell changes were made. "
            "You can still use local prompts like 'keep only Table 14-11.32.2', or update OpenAI billing/quota and try again."
        ),
        "warnings": [error_message],
    }


def normalize_chat_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value[-12:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "").strip()
        if content:
            history.append({"role": role, "content": content[:3000]})
    return history


def refine_clean_shell_locally(clean_lines: list[str], instruction: str) -> dict[str, Any] | None:
    instruction_key = normalize_heading(instruction)
    keep_words = {"keep", "only", "single", "one", "first", "second", "third", "last", "except"}
    if not any(word in instruction_key.split() for word in keep_words):
        return None
    if not any(word in instruction_key.split() for word in ("table", "listing", "figure", "tfl", "output")):
        return None

    sections = split_clean_shell_sections(clean_lines)
    if not sections:
        return None

    target_sections = select_sections_for_keep_instruction(sections, instruction)
    if target_sections is None:
        return None
    if not target_sections:
        return {
            "method": "local_refine:keep_only_needs_target",
            "cleaned_lines": clean_lines,
            "message": (
                "I could not tell which output to keep. Please include the table, listing, or figure number "
                "such as 'keep only Table 14-11.32.2'."
            ),
            "warnings": ["No clean shell changes were made because the keep-only instruction was ambiguous."],
        }

    refined_lines: list[str] = []
    for section in target_sections:
        if refined_lines:
            refined_lines.append("")
        refined_lines.extend(section["lines"])
    label = ", ".join(section.get("label", "output") for section in target_sections)
    return {
        "method": "local_refine:keep_only",
        "cleaned_lines": clean_output_lines(refined_lines),
        "message": f"Kept only {label}.",
        "warnings": [],
    }


def split_clean_shell_sections(clean_lines: list[str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in clean_lines:
        stripped = line.strip()
        if is_output_start_line(stripped):
            if current:
                sections.append(current)
            parsed = parse_output_start_line(stripped)
            current = {
                "tfl_number": parsed.get("tfl_number", ""),
                "tfl_type": parsed.get("tfl_type", ""),
                "title": parsed.get("title", ""),
                "label": f"{parsed.get('tfl_type', 'output')} {parsed.get('tfl_number', '')}".strip(),
                "lines": [line],
            }
            continue
        if current:
            current["lines"].append(line)
    if current:
        sections.append(current)
    return sections


def select_sections_for_keep_instruction(
    sections: list[dict[str, Any]],
    instruction: str,
) -> list[dict[str, Any]] | None:
    instruction_key = normalize_heading(instruction)
    instruction_tokens = set(instruction_key.split())
    requested_numbers = [
        normalize_match_key(match)
        for match in re.findall(r"\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?", instruction)
    ]
    only_like = any(
        phrase in instruction_key
        for phrase in (
            "keep only",
            "only keep",
            "keep one",
            "keep single",
            "all except",
            "remove all except",
            "delete all except",
        )
    ) or ("keep" in instruction_tokens and "only" in instruction_tokens) or (
        "keep" in instruction_tokens and bool(requested_numbers)
    )
    if not only_like:
        return None

    requested_types = {
        word
        for word in ("table", "listing", "figure")
        if re.search(rf"(?i)\b{word}s?\b", instruction)
    }
    candidate_sections = [
        section
        for section in sections
        if not requested_types or section.get("tfl_type", "").lower() in requested_types
    ]
    if not candidate_sections:
        return []

    if requested_numbers:
        return [
            section
            for section in candidate_sections
            if normalize_match_key(section.get("tfl_number", "")) in requested_numbers
        ]

    ordinal = ordinal_requested(instruction_key)
    if ordinal is not None:
        if ordinal == -1:
            return [candidate_sections[-1]]
        if 0 <= ordinal < len(candidate_sections):
            return [candidate_sections[ordinal]]
        return []

    if ("one" in instruction_tokens or "single" in instruction_tokens) and len(candidate_sections) > 1:
        return []
    if len(candidate_sections) == 1:
        return candidate_sections
    if re.search(r"(?i)\ball\s+tables?\s+only\b|\bonly\s+(the\s+)?tables\b|\btables\s+only\b", instruction):
        return candidate_sections if requested_types == {"table"} else []
    return []


def ordinal_requested(instruction_key: str) -> int | None:
    if "first" in instruction_key:
        return 0
    if "second" in instruction_key:
        return 1
    if "third" in instruction_key:
        return 2
    if "last" in instruction_key:
        return -1
    return None


def generate_refined_clean_shell_with_llm(
    source_name: str,
    clean_shell_text: str,
    instruction: str,
    conversation: list[dict[str, str]],
    retrieved: list[dict[str, Any]],
) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured; configure an LLM before using clean-shell chat refinement.")

    prompt = build_clean_shell_refinement_prompt(source_name, clean_shell_text, instruction, retrieved)
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior clinical SAS TFL shell editor. "
                "Refine clean shell documents according to user instructions. "
                "Return valid JSON only."
            ),
        }
    ]
    for item in conversation[-8:]:
        messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": prompt})
    request_body = {
        "model": model,
        "temperature": 0.1,
        "messages": messages,
    }
    try:
        data = openai_chat_completion(base_url, api_key, request_body, timeout=120)
        content = data["choices"][0]["message"]["content"]
        parsed = extract_json_response(content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM did not return a JSON object.")
        clean_lines = normalize_clean_lines(parsed.get("clean_lines") or parsed.get("lines") or [])
        if not clean_lines:
            raise ValueError("LLM returned no clean shell lines.")
        return {
            "method": f"llm_refine:{model}",
            "cleaned_lines": clean_lines,
            "message": str(parsed.get("message") or parsed.get("summary") or "Applied the requested refinement."),
            "warnings": [str(item) for item in parsed.get("warnings") or parsed.get("notes") or []],
        }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"LLM clean-shell refinement failed: {type(exc).__name__}: {exc}") from exc


def openai_chat_completion(
    base_url: str,
    api_key: str,
    request_body: dict[str, Any],
    timeout: int = 120,
    max_attempts: int = 3,
) -> dict[str, Any]:
    last_error = ""
    for attempt in range(max(1, max_attempts)):
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = openai_http_error_detail(exc)
            if exc.code == 429:
                last_error = detail or "OpenAI returned HTTP 429 Too Many Requests."
                if attempt < max_attempts - 1:
                    time.sleep(openai_retry_delay(exc, attempt))
                    continue
                raise ValueError(
                    "OpenAI rate limit or quota was reached after retrying. "
                    "Please wait a minute and try again, or use a local keep-only instruction with a TFL number. "
                    f"Details: {last_error}"
                ) from exc
            raise ValueError(f"OpenAI request failed with HTTP {exc.code}. {detail}".strip()) from exc
    raise ValueError(last_error or "OpenAI request failed.")


def openai_retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else ""
    try:
        if retry_after:
            return min(20.0, max(1.0, float(retry_after)))
    except ValueError:
        pass
    return min(20.0, 2.0 * (attempt + 1))


def openai_http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    try:
        parsed = json.loads(raw)
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else ""
        if message:
            return str(message)
    except json.JSONDecodeError:
        pass
    return raw[:600] if raw else str(exc)


def build_clean_shell_refinement_prompt(
    source_name: str,
    clean_shell_text: str,
    instruction: str,
    retrieved: list[dict[str, Any]],
) -> str:
    examples = []
    for index, item in enumerate(retrieved[:4], start=1):
        examples.append(
            textwrap.dedent(
                f"""
                === Knowledge Base Example {index} ===
                TFL: {item.get('tlf_number', '')} ({item.get('tlf_type', '')})
                Title: {item.get('title', '')}
                Historical shell excerpt:
                {excerpt(item.get('shell_text', ''), 900)}
                Historical output excerpt:
                {excerpt(item.get('output_text', ''), 900)}
                """
            ).strip()
        )

    return textwrap.dedent(
        f"""
        Apply the user's latest refinement instruction to the current clean shell.

        Rules:
        - Keep the result as clean shell content only.
        - Preserve Table/Listing/Figure numbers, titles, ordering, headers, footnotes, and row labels unless
          the user explicitly asks to change them.
        - Keep x/xx/xx.x placeholders for expected result values.
        - Never introduce actual historical output result values, counts, percentages, confidence intervals, or N values.
        - Use knowledge-base examples only to understand structure and formatting.
        - Return valid JSON only with:
          {{
            "clean_lines": ["line 1", "line 2"],
            "message": "short summary of what changed",
            "warnings": []
          }}

        Clean shell file: {source_name}

        User refinement instruction:
        {instruction}

        Current clean shell:
        {excerpt(clean_shell_text, 24000)}

        Knowledge-base examples:
        {chr(10).join(examples) if examples else 'No knowledge-base examples are available.'}
        """
    ).strip()


def write_clean_shell_lines(clean_path: Path, lines: list[str]) -> None:
    suffix = clean_path.suffix.lower()
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".docx":
        write_docx_from_lines(clean_path, lines)
    elif suffix == ".rtf":
        clean_path.write_text(lines_to_rtf(lines), encoding="utf-8")
    else:
        clean_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_shell_document_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    return read_shell_document_lines_from_raw(path.name, raw)


def read_shell_document_lines_from_raw(name: str, raw: bytes) -> list[str]:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        return extract_docx_document_lines(raw)
    if suffix == ".rtf":
        return strip_rtf(decode_bytes(raw)).splitlines()
    return decode_bytes(raw).splitlines()


def extract_docx_document_lines(raw: bytes) -> list[str]:
    root = read_docx_document_root(raw)
    return collect_docx_block_lines(root)


def read_docx_document_root(raw: bytes) -> ElementTree.Element:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise ValueError("DOCX file does not contain word/document.xml.") from exc
    return ElementTree.fromstring(xml)


def collect_docx_paragraphs(root: ElementTree.Element) -> list[ElementTree.Element]:
    paragraphs: list[ElementTree.Element] = []

    def walk(node: ElementTree.Element) -> None:
        for child in list(node):
            if local_xml_name(child.tag) == "p":
                paragraphs.append(child)
            walk(child)

    walk(root)
    return paragraphs


def collect_docx_block_lines(root: ElementTree.Element) -> list[str]:
    body = next((node for node in root.iter() if local_xml_name(node.tag) == "body"), root)
    lines: list[str] = []
    for child in list(body):
        name = local_xml_name(child.tag)
        if name == "p":
            lines.append(docx_paragraph_text(child))
        elif name == "tbl":
            lines.extend(docx_table_lines(child))
    return lines


def docx_table_lines(table: ElementTree.Element) -> list[str]:
    lines: list[str] = []
    for row in [child for child in list(table) if local_xml_name(child.tag) == "tr"]:
        cells: list[str] = []
        for cell in [child for child in list(row) if local_xml_name(child.tag) == "tc"]:
            text = docx_cell_text(cell)
            span = docx_grid_span(cell)
            cells.extend([text] * max(1, span))
        if any(cell.strip() for cell in cells):
            lines.append(" | ".join(cells))
    return lines


def docx_cell_text(cell: ElementTree.Element) -> str:
    paragraphs = [node for node in cell.iter() if local_xml_name(node.tag) == "p"]
    parts = [clean_header_line(docx_paragraph_text(paragraph)) for paragraph in paragraphs]
    return " ".join(part for part in parts if part)


def docx_grid_span(cell: ElementTree.Element) -> int:
    for node in cell.iter():
        if local_xml_name(node.tag) != "gridSpan":
            continue
        value = node.attrib.get(f"{{{W_NS}}}val") or node.attrib.get("val") or "1"
        try:
            return max(1, int(value))
        except ValueError:
            return 1
    return 1


def collect_docx_paragraph_entries(root: ElementTree.Element) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def walk(parent: ElementTree.Element) -> None:
        for child in list(parent):
            if local_xml_name(child.tag) == "p":
                entries.append(
                    {
                        "parent": parent,
                        "paragraph": child,
                        "text": docx_paragraph_text(child),
                    }
                )
            walk(child)

    walk(root)
    return entries


def docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    pieces: list[str] = []
    for node in paragraph.iter():
        name = local_xml_name(node.tag)
        if name == "t":
            pieces.append(node.text or "")
        elif name == "tab":
            pieces.append("\t")
        elif name in {"br", "cr"}:
            pieces.append(" ")
    return "".join(pieces)


def local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def build_clean_shell_plan(lines: list[str]) -> dict[str, Any]:
    warnings: list[str] = []
    treatment_start = find_treatment_columns_section(lines)
    if treatment_start is None:
        warnings.append("Treatment columns section was not found.")
        treatment_start = 0

    tfl_start = find_tfl_shells_section(lines, treatment_start + 1)
    if tfl_start is None:
        warnings.append("TFL Shells section was not found; searched after the treatment columns section.")
        tfl_start = len(lines)

    headers = parse_treatment_headers(lines, treatment_start + 1, tfl_start)
    if not headers:
        fallback = [
            line.strip()
            for line in lines[treatment_start + 1 : tfl_start]
            if line.strip() and not is_section_heading(line)
        ]
        if fallback:
            headers["1"] = {"id": "1", "lines": fallback}
            warnings.append("No explicit Header 1/Header 2 labels were found; used the treatment section as Header 1.")
        else:
            warnings.append("No treatment headers were extracted.")

    applications = find_header_applications(lines, tfl_start, headers)
    if headers and not applications:
        warnings.append("No Header references were found in the TFL Shells section.")

    return {
        "treatment_start": treatment_start,
        "tfl_start": tfl_start,
        "headers": headers,
        "applications": applications,
        "cleaned_lines": apply_header_applications_to_lines(lines, applications, headers),
        "warnings": warnings,
    }


def finalize_clean_shell_plan(
    lines: list[str],
    heuristic_plan: dict[str, Any],
    llm_plan: dict[str, Any],
    knowledge_examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings = list(heuristic_plan.get("warnings", []))
    clean_lines = normalize_clean_lines(llm_plan.get("cleaned_lines") or [])
    outputs = normalize_llm_outputs(llm_plan.get("outputs") or [])
    applications = list(heuristic_plan.get("applications", []))
    method = llm_plan.get("method", "")
    headers = heuristic_plan.get("headers") or {}

    if outputs:
        outputs = enrich_outputs_with_columns(outputs, headers)
    original_shell = build_original_shell_clean_output(lines, heuristic_plan, outputs)
    if original_shell["cleaned_lines"]:
        if llm_plan.get("warning"):
            warnings.append(llm_plan["warning"])
        warnings.extend(llm_plan.get("warnings") or [])
        method_label = method or "heuristic"
        if method_label.startswith("llm:"):
            method_label = f"{method_label}+original_rows"
        elif method_label and method_label not in {"heuristic", "llm_unavailable"}:
            method_label = f"{method_label}_original_rows_fallback"
        else:
            method_label = "heuristic"
        return {
            **heuristic_plan,
            "cleaned_lines": original_shell["cleaned_lines"],
            "outputs": original_shell["outputs"],
            "applications": original_shell["applications"] or applications,
            "warnings": unique_preserve(warnings),
            "method": method_label,
        }

    knowledge_shell = build_knowledge_based_clean_output(
        lines,
        heuristic_plan,
        knowledge_examples or [],
        outputs,
    )
    if knowledge_shell["cleaned_lines"] and knowledge_shell.get("knowledge_used_count", 0):
        if llm_plan.get("warning"):
            warnings.append(llm_plan["warning"])
        warnings.extend(llm_plan.get("warnings") or [])
        warnings.extend(knowledge_shell.get("warnings") or [])
        method_label = method or "knowledge_output_shape"
        if method_label.startswith("llm:"):
            method_label = f"{method_label}+knowledge_output_shape"
        else:
            method_label = "knowledge_output_shape"
        return {
            **heuristic_plan,
            "cleaned_lines": knowledge_shell["cleaned_lines"],
            "outputs": knowledge_shell["outputs"],
            "applications": knowledge_shell["applications"] or applications,
            "warnings": unique_preserve(warnings),
            "method": method_label,
        }

    if clean_lines:
        warnings.extend(llm_plan.get("warnings") or [])
        if not applications:
            applications = applications_from_outputs(outputs)
        if not outputs:
            outputs = infer_outputs_from_clean_lines(clean_lines)
        outputs = enrich_outputs_with_columns(outputs, headers)
        clean_lines = expand_clean_lines_with_output_columns(clean_lines, outputs, headers)
        return {
            **heuristic_plan,
            "cleaned_lines": clean_lines,
            "outputs": outputs,
            "applications": applications,
            "warnings": unique_preserve(warnings),
            "method": method or "llm",
        }

    fallback_lines = build_heuristic_clean_output_lines(lines, heuristic_plan)
    fallback_outputs = infer_outputs_from_clean_lines(fallback_lines)
    fallback_outputs = enrich_outputs_with_columns(fallback_outputs, headers)
    if llm_plan.get("warning"):
        warnings.append(llm_plan["warning"])
    return {
        **heuristic_plan,
        "cleaned_lines": fallback_lines,
        "outputs": fallback_outputs,
        "warnings": unique_preserve(warnings),
        "method": "heuristic_fallback" if method and method != "heuristic" else "heuristic",
    }


def generate_clean_shell_with_llm(
    source_name: str,
    shell_text: str,
    heuristic_plan: dict[str, Any],
    retrieved: list[dict[str, Any]],
    use_llm: bool = True,
) -> dict[str, Any]:
    if not use_llm:
        return {"method": "heuristic", "warning": "LLM parsing was disabled."}
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "method": "llm_unavailable",
            "warning": "OPENAI_API_KEY is not configured; used local clean-shell fallback.",
        }

    prompt = build_shell_cleaning_prompt(source_name, shell_text, heuristic_plan, retrieved)
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request_body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior clinical SAS TFL shell analyst. "
                    "Interpret clinical shell documents and return clean TFL shell content only. "
                    "Return valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        data = openai_chat_completion(base_url, api_key, request_body, timeout=120)
        content = data["choices"][0]["message"]["content"]
        parsed = extract_json_response(content)
        if not isinstance(parsed, dict):
            return {
                "method": "llm_invalid_response",
                "warning": "LLM did not return a JSON object; used local clean-shell fallback.",
            }
        clean_lines = normalize_clean_lines(parsed.get("clean_lines") or [])
        outputs = normalize_llm_outputs(parsed.get("outputs") or [])
        if not clean_lines and outputs:
            clean_lines = flatten_output_clean_lines(outputs)
        if not clean_lines:
            return {
                "method": "llm_empty_response",
                "warning": "LLM returned no clean shell lines; used local clean-shell fallback.",
            }
        return {
            "method": f"llm:{model}",
            "cleaned_lines": clean_lines,
            "outputs": outputs,
            "warnings": [str(item) for item in parsed.get("warnings") or parsed.get("notes") or []],
        }
    except Exception as exc:
        return {
            "method": "llm_error",
            "warning": f"LLM clean-shell creation failed: {type(exc).__name__}: {exc}",
        }


def build_shell_cleaning_prompt(
    source_name: str,
    shell_text: str,
    heuristic_plan: dict[str, Any],
    retrieved: list[dict[str, Any]],
) -> str:
    examples = []
    for index, item in enumerate(retrieved[:5], start=1):
        examples.append(
            textwrap.dedent(
                f"""
                === Knowledge Base Example {index} ===
                Study: {item.get('study_id', '')}
                TFL: {item.get('tlf_number', '')} ({item.get('tlf_type', '')})
                Title: {item.get('title', '')}
                Source datasets: {item.get('source_datasets', '')}
                Historical shell excerpt:
                {excerpt(item.get('shell_text', ''), 1800)}
                Historical output excerpt:
                {excerpt(item.get('output_text', ''), 1800)}
                """
            ).strip()
        )

    heuristic_summary = {
        "headers": {
            header_id: header.get("lines", [])
            for header_id, header in (heuristic_plan.get("headers") or {}).items()
        },
        "applications": heuristic_plan.get("applications") or [],
        "treatment_start": line_number(heuristic_plan.get("treatment_start")),
        "tfl_start": line_number(heuristic_plan.get("tfl_start")),
    }
    return textwrap.dedent(
        f"""
        Read the new clinical TFL shell document and produce a clean shell.

        Goal:
        - Use the knowledge-base examples to learn how shell sections map to final output shells.
        - Make the clean shell look like the corresponding historical output layout when the shell
          supports that structure, but never carry over historical result values.
        - Replace actual result values, counts, percentages, confidence intervals, and N values with
          shell placeholders using x/xx/xx.x-style symbols.
        - Interpret the treatment column/header section in the new shell.
        - For every Table, Listing, or Figure shell in the new document, apply the referenced header.
        - The assigned header is the shell column definition. Expand it into explicit columns in the clean shell.
        - Use this format whenever a header is assigned:
          Columns (Header 1):
           | Placebo | Drug A | Total
          Age (years)
            Mean (SD)
        - Keep only clean output shell content from the new shell document.
        - Remove support material such as treatment-header catalogs, table of contents, revision history,
          instructions, programming notes, examples copied from old studies, and document boilerplate.
        - Do not invent rows, titles, footnotes, or columns that are not supported by the new shell.
        - Do not copy actual knowledge-base result numbers into the clean result; use examples only
          to understand structure and replace values with x placeholders.

        Return valid JSON only with this shape:
        {{
          "clean_lines": ["line 1", "line 2"],
          "outputs": [
            {{
              "tfl_number": "14.1.1",
              "tfl_type": "table",
              "title": "Title text",
              "header_id": "1",
              "header_lines": ["Placebo", "Drug A", "Total"],
              "columns": ["Placebo", "Drug A", "Total"],
              "clean_lines": ["Table 14.1.1", "Columns (Header 1):", " | Placebo | Drug A | Total", "Age (years)"]
            }}
          ],
          "warnings": []
        }}

        Local shell analysis from the app:
        {json.dumps(heuristic_summary, indent=2)}

        New shell document name: {source_name}

        New shell document text:
        {excerpt(shell_text, 20000)}

        Knowledge-base examples:
        {chr(10).join(examples) if examples else 'No knowledge-base examples are available.'}
        """
    ).strip()


def extract_json_response(content: str) -> Any:
    stripped = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    return json.loads(stripped)


def normalize_clean_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return clean_output_lines(value.splitlines())
    if isinstance(value, list):
        return clean_output_lines([str(line) for line in value])
    return []


def normalize_llm_outputs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    outputs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        clean_lines = normalize_clean_lines(item.get("clean_lines") or [])
        outputs.append(
            {
                "tfl_number": str(item.get("tfl_number") or item.get("number") or "").strip(),
                "tfl_type": str(item.get("tfl_type") or item.get("type") or "").strip().lower(),
                "title": str(item.get("title") or "").strip(),
                "header_id": normalize_header_id(str(item.get("header_id") or "")),
                "header_lines": normalize_clean_lines(item.get("header_lines") or []),
                "columns": normalize_clean_lines(item.get("columns") or []),
                "clean_lines": clean_lines,
                "line_count": len(clean_lines),
            }
        )
    return outputs


def enrich_outputs_with_columns(
    outputs: list[dict[str, Any]],
    headers: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for output in outputs:
        item = dict(output)
        header_id = normalize_header_id(item.get("header_id", ""))
        header_lines = item.get("header_lines") or []
        if not header_lines and header_id in headers:
            header_lines = headers[header_id].get("lines", [])
        columns = item.get("columns") or expand_header_columns(header_lines)
        item["header_id"] = header_id
        item["header_lines"] = header_lines
        item["columns"] = columns
        item["column_count"] = len(columns)
        enriched.append(item)
    return enriched


def flatten_output_clean_lines(outputs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in outputs:
        output_lines = normalize_clean_lines(item.get("clean_lines") or [])
        if not output_lines:
            continue
        if lines:
            lines.append("")
        lines.extend(output_lines)
    return clean_output_lines(lines)


def expand_clean_lines_with_output_columns(
    clean_lines: list[str],
    outputs: list[dict[str, Any]],
    headers: dict[str, dict[str, Any]],
) -> list[str]:
    if not clean_lines:
        return []
    outputs_by_number = {
        normalize_match_key(output.get("tfl_number", "")): output
        for output in outputs
        if output.get("tfl_number")
    }
    result: list[str] = []
    output_index = -1
    inserted_for_current_output = False

    for index, line in enumerate(clean_lines):
        stripped = line.strip()
        if is_output_start_line(stripped):
            output_index += 1
            inserted_for_current_output = False
            result.append(line)
            output = output_for_line(stripped, outputs, outputs_by_number, output_index)
            expanded = expanded_column_lines_for_output(output, headers)
            if expanded and not has_expanded_columns_ahead(clean_lines, index + 1):
                result.extend(expanded)
                inserted_for_current_output = True
            continue

        header_match = HEADER_REF_RE.search(stripped)
        if header_match:
            header_id = normalize_header_id(header_match.group("header_id"))
            expanded = expanded_header_lines(header_id, headers.get(header_id, {}).get("lines", []))
            if expanded:
                if not inserted_for_current_output:
                    result.extend(expanded)
                    inserted_for_current_output = True
                continue

        result.append(line)
    return clean_output_lines(result)


def output_for_line(
    line: str,
    outputs: list[dict[str, Any]],
    outputs_by_number: dict[str, dict[str, Any]],
    output_index: int,
) -> dict[str, Any]:
    match = re.search(r"\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?", line)
    if match:
        key = normalize_match_key(match.group(0))
        if key in outputs_by_number:
            return outputs_by_number[key]
    if 0 <= output_index < len(outputs):
        return outputs[output_index]
    return {}


def expanded_column_lines_for_output(
    output: dict[str, Any],
    headers: dict[str, dict[str, Any]],
) -> list[str]:
    header_id = normalize_header_id(output.get("header_id", ""))
    header_lines = output.get("header_lines") or []
    if not header_lines and header_id in headers:
        header_lines = headers[header_id].get("lines", [])
    columns = output.get("columns") or expand_header_columns(header_lines)
    return expanded_header_lines(header_id, header_lines, columns=columns)


def has_expanded_columns_ahead(lines: list[str], start: int) -> bool:
    lookahead = "\n".join(lines[start : start + 8]).lower()
    return "columns (header" in lookahead


def applications_from_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applications: list[dict[str, Any]] = []
    for index, output in enumerate(outputs, start=1):
        header_id = normalize_header_id(output.get("header_id", ""))
        if not header_id:
            continue
        applications.append(
            {
                "line_index": None,
                "header_id": header_id,
                "context": output.get("title") or output.get("tfl_number") or f"Output {index}",
            }
        )
    return applications


def build_knowledge_based_clean_output(
    lines: list[str],
    plan: dict[str, Any],
    knowledge_examples: list[dict[str, Any]],
    llm_outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    headers = plan.get("headers") or {}
    sections = extract_original_output_sections(lines, plan)
    if not sections or not knowledge_examples:
        return {"cleaned_lines": [], "outputs": [], "applications": [], "knowledge_used_count": 0}

    llm_by_number = {
        normalize_match_key(output.get("tfl_number", "")): output
        for output in (llm_outputs or [])
        if output.get("tfl_number")
    }
    cleaned_lines: list[str] = []
    outputs: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    warnings: list[str] = []
    knowledge_used_count = 0

    for section_index, section in enumerate(sections, start=1):
        header_id = header_id_for_section(section, llm_by_number)
        header_lines = headers.get(header_id, {}).get("lines", []) if header_id else []
        llm_output = llm_by_number.get(normalize_match_key(section.get("tfl_number", "")), {})
        if not header_lines:
            header_lines = llm_output.get("header_lines") or []
        columns = expand_header_columns(header_lines) or llm_output.get("columns") or []
        expanded = expanded_header_lines(header_id, header_lines, columns=columns)
        example = best_knowledge_example_for_section(section, knowledge_examples)
        template_lines = output_example_to_x_template_lines(example, columns) if example else []
        inserted = False
        source = "original_shell"

        if template_lines:
            section_lines = [section["lines"][0]]
            if expanded:
                section_lines.extend(expanded)
                inserted = True
            section_lines.extend(template_lines)
            section_lines = clean_output_lines(section_lines)
            knowledge_used_count += 1
            source = "knowledge_output_shape"
        else:
            section_lines, inserted = clean_original_section_lines(section["lines"], header_id, expanded, columns)

        if not section_lines:
            continue
        if cleaned_lines:
            cleaned_lines.append("")
        cleaned_lines.extend(section_lines)
        applications.append(
            {
                "line_index": section.get("header_line_index"),
                "header_id": header_id,
                "context": section.get("title") or section.get("tfl_number") or f"Output {section_index}",
            }
        )
        outputs.append(
            {
                "tfl_number": section.get("tfl_number", ""),
                "tfl_type": section.get("tfl_type", ""),
                "title": section.get("title", ""),
                "header_id": header_id,
                "header_lines": header_lines,
                "columns": columns,
                "column_count": len(columns),
                "line_count": len(section_lines),
                "row_count": count_clean_shell_rows(section_lines, len(expanded)),
                "columns_inserted": bool(inserted),
                "template_source": source,
                "knowledge_example_id": example.get("id") if example else None,
                "knowledge_example_tfl_number": example.get("tlf_number", "") if example else "",
            }
        )

    if sections and not knowledge_used_count:
        warnings.append("No matching knowledge-base output layout was found; used original shell rows.")

    return {
        "cleaned_lines": clean_output_lines(cleaned_lines),
        "outputs": outputs,
        "applications": applications,
        "warnings": warnings,
        "knowledge_used_count": knowledge_used_count,
    }


def best_knowledge_example_for_section(
    section: dict[str, Any],
    examples: list[dict[str, Any]],
) -> dict[str, Any] | None:
    usable = [item for item in examples if str(item.get("output_text") or "").strip()]
    if not usable:
        return None
    section_number_key = normalize_match_key(section.get("tfl_number", ""))
    if section_number_key:
        for item in usable:
            if normalize_match_key(item.get("tlf_number", "")) == section_number_key:
                return item

    section_tokens = Counter(
        tokenize(
            " ".join(
                [
                    str(section.get("tfl_number") or ""),
                    str(section.get("tfl_type") or ""),
                    str(section.get("title") or ""),
                    str((section.get("lines") or [""])[0]),
                ]
            )
        )
    )
    section_norm = math.sqrt(sum(value * value for value in section_tokens.values())) or 1.0
    best_score = 0.0
    best_item: dict[str, Any] | None = None
    for item in usable:
        text = "\n".join(
            [
                str(item.get("tlf_number") or ""),
                str(item.get("tlf_type") or ""),
                str(item.get("title") or ""),
                str(item.get("output_name") or ""),
                str(item.get("shell_name") or ""),
            ]
        )
        item_tokens = Counter(tokenize(text))
        if not item_tokens:
            continue
        item_norm = math.sqrt(sum(value * value for value in item_tokens.values())) or 1.0
        overlap = set(section_tokens).intersection(item_tokens)
        score = sum(section_tokens[token] * item_tokens[token] for token in overlap)
        score = score / (section_norm * item_norm)
        if section.get("tfl_type") and str(item.get("tlf_type") or "").lower() == section.get("tfl_type"):
            score += 0.05
        if score > best_score:
            best_score = score
            best_item = item
    return best_item if best_score >= 0.18 else None


def output_example_to_x_template_lines(example: dict[str, Any], columns: list[str]) -> list[str]:
    output_text = str(example.get("output_text") or "")
    if not output_text.strip():
        return []
    output_lines = clean_output_lines(normalize_text(output_text).splitlines())
    if not output_lines:
        return []
    body_start = output_body_start_index(output_lines, columns)
    body_lines = output_lines[body_start:] if body_start < len(output_lines) else []
    return body_lines_to_x_template_rows(body_lines, len(columns))


def output_body_start_index(lines: list[str], columns: list[str]) -> int:
    start = first_output_line_index(lines)
    if start is None:
        start = 0
    candidates = lines[start + 1 :]
    header_terms = header_terms_for_template(columns)
    seen_header = False
    for offset, line in enumerate(candidates[:60]):
        stripped = line.strip()
        if not stripped or is_output_admin_line(stripped):
            continue
        if seen_header and (is_output_body_signal_line(stripped) or is_result_value_line(stripped)):
            return start + 1 + offset
        if is_likely_output_header_line(stripped, header_terms):
            seen_header = True
            continue
        if seen_header and not is_probable_title_continuation(stripped):
            return start + 1 + offset

    for offset, line in enumerate(candidates[:100]):
        stripped = line.strip()
        if is_output_body_signal_line(stripped) or is_result_value_line(stripped):
            return start + 1 + offset
    return min(start + 1, len(lines))


def header_terms_for_template(columns: list[str]) -> set[str]:
    terms: set[str] = {
        "cohort",
        "overall",
        "responder",
        "non",
        "combined",
        "infusion",
        "infusions",
        "placebo",
        "treatment",
        "dose",
        "group",
        "arm",
    }
    for column in columns:
        terms.update(token for token in tokenize(column) if len(token) >= 3)
    return terms


def is_likely_output_header_line(line: str, header_terms: set[str]) -> bool:
    key = normalize_heading(line)
    if not key:
        return False
    if re.search(r"(?i)\bN\s*=", line):
        return True
    if len(line) > 140:
        return False
    tokens = set(tokenize(line))
    if not tokens:
        return False
    if tokens.intersection(header_terms):
        return True
    return bool(re.fullmatch(r"(?i)n\s*\(%\)|n|%|mean\s*\(sd\)|median|q1\s*,?\s*q3", line.strip()))


def is_probable_title_continuation(line: str) -> bool:
    if len(line) > 100:
        return True
    key = normalize_heading(line)
    return any(
        phrase in key
        for phrase in (
            "analysis set",
            "safety analysis",
            "intent to treat",
            "all participants",
        )
    )


def is_output_body_signal_line(line: str) -> bool:
    return bool(
        re.match(
            r"(?i)^\s*(reporting period|number of|participants?|subjects?|patients?|total\b|grade\b|"
            r"mean\b|median\b|min\b|max\b|q1\b|q3\b|at week\b|at day\b|baseline\b|change\b|"
            r"adverse event|preferred term|system organ class|hearing impairment|risk difference|"
            r"km estimate|estimate\b|95%\s+ci\b)",
            line,
        )
    )


def body_lines_to_x_template_rows(lines: list[str], column_count: int) -> list[str]:
    result: list[str] = []
    label_parts: list[str] = []
    values: list[str] = []

    def flush_label() -> None:
        nonlocal label_parts
        if label_parts:
            result.append(mask_non_result_line(" ".join(label_parts)))
            label_parts = []

    def flush_values() -> None:
        nonlocal values
        if values:
            label = mask_non_result_line(" ".join(label_parts))
            row_values = values[: column_count or len(values)]
            result.append(format_template_row(label, row_values, column_count))
            del values[: len(row_values)]
            label_parts.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_values()
            flush_label()
            if result and result[-1] != "":
                result.append("")
            continue
        if is_output_admin_line(line):
            break
        pieces = split_result_value_pieces(line)
        if pieces:
            values.extend(mask_result_value(piece) for piece in pieces)
            while column_count and len(values) >= column_count:
                flush_values()
            continue
        if values:
            flush_values()
        if label_parts and should_flush_label_before(label_parts, line):
            flush_label()
        label_parts.append(line)
        if len(result) >= 180:
            break

    flush_values()
    flush_label()
    return clean_output_lines(result[:180])


def split_result_value_pieces(line: str) -> list[str]:
    if "|" in line:
        pieces = [piece.strip() for piece in line.split("|") if piece.strip()]
        if pieces and all(is_result_value_line(piece) for piece in pieces):
            return pieces
    if is_result_value_line(line):
        return [line]
    return []


def is_result_value_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if not re.search(r"[0-9xX]", stripped):
        return False
    without_x = re.sub(r"(?i)\bx+\b", "", stripped)
    letters = re.findall(r"[A-WYZa-wyz]", without_x)
    if letters:
        return False
    return bool(re.search(r"[0-9xX]", stripped))


def should_flush_label_before(label_parts: list[str], next_line: str) -> bool:
    current = " ".join(label_parts).strip()
    if re.match(r"(?i)^reporting period\b", current):
        return True
    if re.match(r"(?i)^(hearing impairment|adverse event|preferred term|system organ class|km estimate)\b", current):
        return bool(re.match(r"(?i)^(number of|grade\b|at week\b|at day\b|mean\b|median\b|95%\s+ci\b)", next_line))
    if re.match(r"(?i)^number of\b", current) and re.match(r"(?i)^grade\b", next_line):
        return True
    return False


def format_template_row(label: str, values: list[str], column_count: int) -> str:
    if column_count and len(values) < column_count:
        values = values + ["x"] * (column_count - len(values))
    return " | ".join([label] + values).rstrip()


def mask_result_value(value: str) -> str:
    return re.sub(r"\d", "x", value.strip())


def mask_non_result_line(line: str) -> str:
    return re.sub(
        r"(?i)\b(N\s*=\s*)\d+",
        lambda match: f"{match.group(1)}" + re.sub(r"\d", "x", match.group(0).split("=", 1)[1]),
        line.strip(),
    )


def is_output_admin_line(line: str) -> bool:
    return bool(
        re.match(
            r"(?i)^\s*(page\s+\d+\s+of\s+\d+|program\s*:|output\s*:|source\s*:|"
            r"footnotes?\s+defined|date generated|sas version|version\s+9\.4|normal;|arial;|courier;|"
            r"sps_processing)",
            line,
        )
    )


def build_original_shell_clean_output(
    lines: list[str],
    plan: dict[str, Any],
    llm_outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    headers = plan.get("headers") or {}
    sections = extract_original_output_sections(lines, plan)
    llm_by_number = {
        normalize_match_key(output.get("tfl_number", "")): output
        for output in (llm_outputs or [])
        if output.get("tfl_number")
    }
    cleaned_lines: list[str] = []
    outputs: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []

    for section_index, section in enumerate(sections, start=1):
        header_id = header_id_for_section(section, llm_by_number)
        header_lines = headers.get(header_id, {}).get("lines", []) if header_id else []
        llm_output = llm_by_number.get(normalize_match_key(section.get("tfl_number", "")), {})
        if not header_lines:
            header_lines = llm_output.get("header_lines") or []
        columns = expand_header_columns(header_lines) or llm_output.get("columns") or []
        expanded = expanded_header_lines(header_id, header_lines, columns=columns)
        section_lines, inserted = clean_original_section_lines(
            section["lines"],
            header_id,
            expanded,
            columns,
        )
        if not section_lines:
            continue
        if cleaned_lines:
            cleaned_lines.append("")
        cleaned_lines.extend(section_lines)
        applications.append(
            {
                "line_index": section.get("header_line_index"),
                "header_id": header_id,
                "context": section.get("title") or section.get("tfl_number") or f"Output {section_index}",
            }
        )
        outputs.append(
            {
                "tfl_number": section.get("tfl_number", ""),
                "tfl_type": section.get("tfl_type", ""),
                "title": section.get("title", ""),
                "header_id": header_id,
                "header_lines": header_lines,
                "columns": columns,
                "column_count": len(columns),
                "line_count": len(section_lines),
                "row_count": count_clean_shell_rows(section_lines, len(expanded)),
                "columns_inserted": bool(inserted),
            }
        )

    return {
        "cleaned_lines": clean_output_lines(cleaned_lines),
        "outputs": outputs,
        "applications": applications,
    }


def extract_original_output_sections(lines: list[str], plan: dict[str, Any]) -> list[dict[str, Any]]:
    start = plan.get("tfl_start")
    if not isinstance(start, int) or start >= len(lines):
        start = first_output_line_index(lines)
    if start is None:
        return []
    section_start = start + 1 if is_section_heading(lines[start]) else start
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for index in range(section_start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if is_noise_shell_line(stripped):
            continue
        if is_output_start_line(stripped):
            if current:
                sections.append(current)
            parsed = parse_output_start_line(stripped)
            current = {
                "start_index": index,
                "tfl_number": parsed.get("tfl_number", ""),
                "tfl_type": parsed.get("tfl_type", ""),
                "title": parsed.get("title", ""),
                "lines": [line],
                "header_line_index": None,
            }
            continue
        if not current:
            continue
        current["lines"].append(line)
        if current.get("header_line_index") is None and HEADER_REF_RE.search(stripped):
            current["header_line_index"] = index

    if current:
        sections.append(current)
    return sections


def clean_original_section_lines(
    section_lines: list[str],
    header_id: str,
    expanded_columns: list[str],
    columns: list[str] | None = None,
) -> tuple[list[str], bool]:
    cleaned: list[str] = []
    inserted = False
    column_count = len(columns or [])
    pending_stub_heading = ""
    seen_shell_body = False
    in_notes = False
    for offset, line in enumerate(section_lines):
        stripped = line.strip()
        if offset > 0 and is_output_start_line(stripped):
            break
        if is_noise_shell_line(stripped):
            continue
        if offset == 0:
            cleaned.append(line)
            if expanded_columns:
                cleaned.extend(expanded_columns)
                inserted = True
            continue
        if not stripped:
            if seen_shell_body and cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if is_shell_note_start_line(stripped):
            in_notes = True
            pending_stub_heading = ""
        if not in_notes and HEADER_REF_RE.search(stripped):
            pending_stub_heading = shell_stub_heading_from_header_row(stripped)
            continue
        if is_existing_column_block_line(stripped):
            continue
        if not in_notes and is_table_row_line(stripped):
            expanded_row = expand_original_shell_body_row(stripped, column_count, pending_stub_heading)
            pending_stub_heading = ""
            if expanded_row:
                cleaned.append(expanded_row)
                seen_shell_body = True
                continue
        pending_stub_heading = ""
        seen_shell_body = True
        cleaned.append(line)
    return clean_output_lines(cleaned), inserted


def shell_stub_heading_from_header_row(line: str) -> str:
    cells = split_header_columns(line, keep_empty=True)
    stub_parts: list[str] = []
    for cell in cells:
        if HEADER_REF_RE.search(cell):
            break
        cleaned = clean_header_line(cell)
        if cleaned:
            stub_parts.append(cleaned)
    return " ".join(stub_parts).strip()


def expand_original_shell_body_row(line: str, column_count: int, stub_heading: str = "") -> str:
    cells = split_header_columns(line, keep_empty=True)
    if not cells:
        return line
    label = cells[0] if cells else ""
    values = cells[1:]
    if stub_heading and not label:
        label = stub_heading
    expanded_values = expand_original_shell_values(values, column_count)
    if not expanded_values:
        return label
    return " | ".join([label] + expanded_values).rstrip()


def expand_original_shell_values(values: list[str], column_count: int) -> list[str]:
    cleaned = [clean_header_line(value) for value in values]
    if column_count <= 0:
        return cleaned
    if not cleaned:
        return [""] * column_count
    if len(cleaned) == column_count:
        return cleaned
    nonempty = [value for value in cleaned if value]
    if len(cleaned) == 1:
        return [cleaned[0]] * column_count
    if len(set(value.lower() for value in nonempty)) == 1 and nonempty:
        return [nonempty[0]] * column_count
    if len(cleaned) < column_count:
        repeats = math.ceil(column_count / len(cleaned))
        return (cleaned * repeats)[:column_count]
    return cleaned[:column_count]


def is_shell_note_start_line(line: str) -> bool:
    return bool(
        re.match(
            r"(?i)^\s*(footnotes?|abbreviations?|tfl shell notes?|programming notes?|notes?|source)\s*:",
            line,
        )
    )


def is_existing_column_block_line(line: str) -> bool:
    return bool(
        re.match(r"(?i)^columns\s*(?:\(\s*header\s+[0-9A-Za-z]+\s*\))?\s*:", line)
        or re.match(r"(?i)^column\s+\d+\s*:", line)
        or re.match(r"(?i)^treatment\s+columns?\s*:", line)
    )


def header_id_for_section(section: dict[str, Any], llm_by_number: dict[str, dict[str, Any]]) -> str:
    for line in section.get("lines", []):
        match = HEADER_REF_RE.search(line.strip())
        if match:
            return normalize_header_id(match.group("header_id"))
    llm_output = llm_by_number.get(normalize_match_key(section.get("tfl_number", "")), {})
    return normalize_header_id(llm_output.get("header_id", ""))


def parse_output_start_line(line: str) -> dict[str, str]:
    match = re.match(
        r"(?i)^\s*(?:[A-Za-z0-9]{1,3}(?:[-_.][A-Za-z0-9]{1,4}){1,4}\s*:\s*)?"
        r"(?P<type>table|listing|figure|t|l|f)\s*[:#.\-]?\s*"
        r"(?P<number>\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?)\b\s*[:.\-]?\s*(?P<title>.*)$",
        line,
    )
    if not match:
        return {"tfl_number": "", "tfl_type": "", "title": ""}
    marker = match.group("type").lower()
    return {
        "tfl_number": match.group("number"),
        "tfl_type": {"t": "table", "l": "listing", "f": "figure"}.get(marker, marker),
        "title": match.group("title").strip(),
    }


def count_clean_shell_rows(section_lines: list[str], expanded_header_line_count: int) -> int:
    start_index = 1 + max(0, expanded_header_line_count)
    count = 0
    for line in section_lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            continue
        if is_existing_column_block_line(stripped):
            continue
        if HEADER_REF_RE.search(stripped):
            continue
        count += 1
    return count


def build_heuristic_clean_output_lines(lines: list[str], plan: dict[str, Any]) -> list[str]:
    start = plan.get("tfl_start")
    if not isinstance(start, int) or start >= len(lines):
        start = first_output_line_index(lines)
    if start is None:
        start = 0

    headers = plan.get("headers") or {}
    applications = {item["line_index"]: item for item in plan.get("applications") or []}
    cleaned: list[str] = []
    seen_output = False
    for index in range(start + 1 if is_section_heading(lines[start]) else start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if is_noise_shell_line(stripped):
            continue
        if is_output_start_line(stripped):
            seen_output = True
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
        if not seen_output and not HEADER_REF_RE.search(stripped):
            continue
        item = applications.get(index)
        if item:
            header_lines = headers.get(item["header_id"], {}).get("lines", [])
            cleaned.extend(expanded_header_lines(item["header_id"], header_lines) + [""])
            continue
        if HEADER_REF_RE.search(stripped):
            header_id = normalize_header_id(HEADER_REF_RE.search(stripped).group("header_id"))
            header_lines = headers.get(header_id, {}).get("lines", [])
            if header_lines:
                cleaned.extend(expanded_header_lines(header_id, header_lines) + [""])
                continue
        cleaned.append(line)
    return clean_output_lines(cleaned)


def first_output_line_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if is_output_start_line(line.strip()):
            return index
    return None


def is_output_start_line(line: str) -> bool:
    return bool(
        re.match(
            r"(?i)^\s*(?:[A-Za-z0-9]{1,3}(?:[-_.][A-Za-z0-9]{1,4}){1,4}\s*:\s*)?"
            r"(table|listing|figure|t|l|f)\s*[:#.\-]?\s*\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?\b",
            line,
        )
    )


def is_noise_shell_line(line: str) -> bool:
    if not line:
        return False
    return bool(
        re.match(
            r"(?i)^(revision history|version history|table of contents|contents|instruction|instructions|programming note|"
            r"general note|template note|treatment columns? information|treatment headers?|mock data|example only)\b",
            line,
        )
    )


def clean_output_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    blank = False
    for line in lines:
        text = str(line).rstrip()
        if not text.strip():
            if cleaned and not blank:
                cleaned.append("")
            blank = True
            continue
        cleaned.append(text)
        blank = False
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return cleaned


def infer_outputs_from_clean_lines(lines: list[str]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_lines = 0
    for line in lines:
        match = re.match(
            r"(?i)^\s*(?P<type>table|listing|figure|t|l|f)\s*[:#.\-]?\s*"
            r"(?P<number>\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?)\b\s*[:.\-]?\s*(?P<title>.*)$",
            line,
        )
        if match:
            if current:
                current["line_count"] = current_lines
                outputs.append(current)
            marker = match.group("type").lower()
            current = {
                "tfl_number": match.group("number"),
                "tfl_type": {"t": "table", "l": "listing", "f": "figure"}.get(marker, marker),
                "title": match.group("title").strip(),
                "header_id": "",
                "header_lines": [],
                "columns": [],
                "column_count": 0,
                "line_count": 0,
            }
            current_lines = 1
        elif current:
            header_match = re.match(r"(?i)^\s*columns\s*\(\s*header\s+([0-9A-Za-z]+)\s*\)\s*:", line)
            if header_match:
                current["header_id"] = normalize_header_id(header_match.group(1))
            column_match = re.match(r"(?i)^\s*column\s+\d+\s*:\s*(.+)$", line)
            if column_match:
                current.setdefault("columns", []).append(clean_header_line(column_match.group(1)))
                current["column_count"] = len(current.get("columns", []))
            current_lines += 1
    if current:
        current["line_count"] = current_lines
        outputs.append(current)
    return outputs


def find_treatment_columns_section(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        key = normalize_heading(line)
        if "following column headers" in key or "column headers and specific footnotes" in key:
            if any(HEADER_DEF_RE.match(lines[lookahead].strip()) for lookahead in range(index + 1, min(len(lines), index + 8))):
                return index
    for index, line in enumerate(lines):
        if HEADER_DEF_RE.match(line.strip()) and any("|" in lines[lookahead] for lookahead in range(index + 1, min(len(lines), index + 8))):
            return max(0, index - 1)
    for index, line in enumerate(lines):
        key = normalize_heading(line)
        if "treatment" in key and ("column" in key or "header" in key or "arm" in key or "group" in key):
            return index
    return None


def find_tfl_shells_section(lines: list[str], start: int = 0) -> int | None:
    for index in range(max(0, start), len(lines)):
        key = normalize_heading(lines[index])
        if key in {"tfl shells", "tlf shells", "tfl shell", "tlf shell"}:
            return index
        if re.fullmatch(r"(table|listing|figure)s?\s+shells?", key):
            return index
    return None


def parse_treatment_headers(lines: list[str], start: int, end: int) -> dict[str, dict[str, Any]]:
    headers: dict[str, dict[str, Any]] = {}
    current_id = ""
    collecting_table = False
    for line in lines[start:end]:
        stripped = line.strip()
        match = HEADER_DEF_RE.match(stripped)
        if match:
            current_id = normalize_header_id(match.group("header_id"))
            headers[current_id] = {"id": current_id, "lines": []}
            collecting_table = False
            body = clean_header_line(match.group("body"))
            if body:
                headers[current_id]["lines"].append(body)
                collecting_table = True
            continue
        if not current_id or not stripped:
            continue
        if re.match(r"(?i)^(footnote|note|tfl shell notes|analysis\s*\||category using in header)\b", stripped):
            collecting_table = False
            current_id = ""
            continue
        if "|" in stripped or "\t" in stripped:
            headers[current_id]["lines"].append(clean_header_line(stripped))
            collecting_table = True
            continue
        if collecting_table:
            collecting_table = False
            current_id = ""

    return {
        header_id: {"id": header_id, "lines": unique_preserve([line for line in header["lines"] if line])}
        for header_id, header in headers.items()
        if any(line for line in header["lines"])
    }


def find_header_applications(
    lines: list[str],
    tfl_start: int,
    headers: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    applications: list[dict[str, Any]] = []
    if not headers:
        return applications
    for index in range(max(0, tfl_start), len(lines)):
        line = lines[index]
        match = HEADER_REF_RE.search(line)
        if not match:
            continue
        header_id = normalize_header_id(match.group("header_id"))
        if header_id not in headers:
            continue
        if already_has_applied_header(lines, index):
            continue
        applications.append(
            {
                "line_index": index,
                "header_id": header_id,
                "context": line.strip()[:200],
            }
        )
    return applications


def already_has_applied_header(lines: list[str], index: int) -> bool:
    lookahead = "\n".join(lines[index + 1 : index + 5]).lower()
    return "applied treatment header" in lookahead


def apply_header_applications_to_lines(
    lines: list[str],
    applications: list[dict[str, Any]],
    headers: dict[str, dict[str, Any]],
) -> list[str]:
    by_index = {item["line_index"]: item for item in applications}
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        cleaned.append(line)
        item = by_index.get(index)
        if not item:
            continue
        cleaned.extend(header_application_lines(item["header_id"], headers[item["header_id"]]["lines"]))
    return cleaned


def header_application_lines(header_id: str, header_lines: list[str]) -> list[str]:
    return [""] + expanded_header_lines(header_id, header_lines) + [""]


def expanded_header_lines(
    header_id: str,
    header_lines: list[str],
    columns: list[str] | None = None,
) -> list[str]:
    parsed_columns = columns if columns is not None else expand_header_columns(header_lines)
    if not parsed_columns:
        return []
    label = f"Columns (Header {header_id}):" if header_id else "Columns:"
    return [label] + format_horizontal_header_rows(header_lines, parsed_columns)


def format_horizontal_header_rows(header_lines: list[str], columns: list[str]) -> list[str]:
    rows = [split_header_columns(line, keep_empty=True) for line in header_lines if str(line).strip()]
    rows = [row for row in rows if row]
    if not rows:
        return [" | " + " | ".join(columns)] if columns else []
    if all(len(row) == 1 for row in rows):
        values = [row[0] for row in rows if row[0]]
        return [" | " + " | ".join(values)] if values else []
    rows = normalize_header_display_rows(rows, len(columns))
    return [" | ".join(row).rstrip() for row in rows if any(cell.strip() for cell in row)]


def normalize_header_display_rows(rows: list[list[str]], column_count: int) -> list[list[str]]:
    if not rows:
        return []
    max_width = max(len(row) for row in rows)
    padded = [row + [""] * (max_width - len(row)) for row in rows]
    target_width = (column_count + 1) if column_count else max_width
    normalized: list[list[str]] = []
    for row in padded:
        if column_count and len(row) == column_count and (not row or row[0].strip()):
            working = [""] + row
        else:
            working = row[:target_width]
        if len(working) < target_width:
            working.extend([""] * (target_width - len(working)))
        normalized.append(working)
    return normalized


def expand_header_columns(header_lines: list[str]) -> list[str]:
    rows = [split_header_columns(line) for line in header_lines if str(line).strip()]
    rows = [row for row in rows if row]
    if not rows:
        return []
    if all(len(row) == 1 for row in rows):
        return unique_preserve([row[0] for row in rows])

    column_count = max(len(row) for row in rows)
    columns: list[str] = []
    for column_index in range(column_count):
        pieces: list[str] = []
        for row in rows:
            if len(row) == column_count:
                piece = row[column_index]
            elif len(row) == 1:
                piece = row[0]
            elif column_index < len(row):
                piece = row[column_index]
            else:
                piece = ""
            piece = clean_header_line(piece)
            if piece and piece.lower() not in {item.lower() for item in pieces}:
                pieces.append(piece)
        if pieces:
            columns.append(" / ".join(pieces))
    return columns


def split_header_columns(line: str, keep_empty: bool = False) -> list[str]:
    text = clean_header_line(line)
    if not text:
        return []
    if "|" in text:
        parts = text.split("|")
    elif "\t" in text:
        parts = text.split("\t")
    elif re.search(r"\s{2,}", text):
        parts = re.split(r"\s{2,}", text)
    elif ";" in text:
        parts = text.split(";")
    elif "," in text and len(text.split(",")) <= 12 and not re.search(r"(?i)\bmin\s*,\s*max\b", text):
        parts = text.split(",")
    else:
        parts = [text]
    cleaned = [clean_header_line(part) for part in parts]
    if keep_empty:
        return cleaned
    return [part for part in cleaned if part]


def normalize_header_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value or "").upper()


def clean_header_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" :-\t")


def normalize_heading(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def is_section_heading(value: str) -> bool:
    key = normalize_heading(value)
    return any(
        phrase in key
        for phrase in (
            "treatment column",
            "treatment header",
            "tfl shell",
            "tlf shell",
            "table shell",
            "listing shell",
            "figure shell",
        )
    )


def clean_shell_output_path(source_path: Path | None, source_name: str, output_dir_text: str = "") -> Path:
    source_file = Path(source_name)
    stem = source_file.stem
    if stem.lower().endswith("_clean"):
        stem = f"{stem}_new"
    filename = f"{stem}_clean{source_file.suffix}"
    if output_dir_text:
        return resolve_user_path(output_dir_text) / filename
    if source_path:
        return source_path.with_name(filename)
    return RUNS_DIR / "clean_shells" / filename


def write_clean_docx(raw: bytes, clean_path: Path, plan: dict[str, Any]) -> None:
    with zipfile.ZipFile(io.BytesIO(raw)) as source_archive:
        document_xml = source_archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
        entries = collect_docx_paragraph_entries(root)
        applications_by_index = {item["line_index"]: item for item in plan["applications"]}

        for line_index in sorted(applications_by_index, reverse=True):
            if line_index >= len(entries):
                continue
            item = applications_by_index[line_index]
            entry = entries[line_index]
            parent = entry["parent"]
            paragraph = entry["paragraph"]
            try:
                paragraph_index = list(parent).index(paragraph)
            except ValueError:
                continue
            insert_lines = header_application_lines(
                item["header_id"],
                plan["headers"][item["header_id"]]["lines"],
            )
            for text in reversed(insert_lines):
                parent.insert(paragraph_index + 1, create_docx_paragraph(text))

        clean_xml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(clean_path, "w", zipfile.ZIP_DEFLATED) as clean_archive:
            for info in source_archive.infolist():
                data = clean_xml if info.filename == "word/document.xml" else source_archive.read(info.filename)
                clean_archive.writestr(info, data)


def write_docx_from_lines(clean_path: Path, lines: list[str]) -> None:
    body_parts: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if is_table_row_line(line):
            table_lines: list[str] = []
            while index < len(lines) and is_table_row_line(lines[index]):
                table_lines.append(lines[index])
                index += 1
            body_parts.append(ElementTree.tostring(create_docx_table(table_lines), encoding="unicode"))
            continue
        body_parts.append(ElementTree.tostring(create_docx_paragraph(line), encoding="unicode"))
        index += 1
    body = "".join(body_parts)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}<w:sectPr /></w:body></w:document>'
    )
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    with zipfile.ZipFile(clean_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document.encode("utf-8"))


def is_table_row_line(line: str) -> bool:
    return "|" in line and not line.strip().lower().startswith("http")


def create_docx_table(lines: list[str]) -> ElementTree.Element:
    table = ElementTree.Element(f"{{{W_NS}}}tbl")
    table_props = ElementTree.SubElement(table, f"{{{W_NS}}}tblPr")
    borders = ElementTree.SubElement(table_props, f"{{{W_NS}}}tblBorders")
    for border_name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = ElementTree.SubElement(borders, f"{{{W_NS}}}{border_name}")
        border.set(f"{{{W_NS}}}val", "single")
        border.set(f"{{{W_NS}}}sz", "4")
        border.set(f"{{{W_NS}}}space", "0")
        border.set(f"{{{W_NS}}}color", "auto")
    for line in lines:
        row = ElementTree.SubElement(table, f"{{{W_NS}}}tr")
        for cell_text in split_header_columns(line, keep_empty=True):
            cell = ElementTree.SubElement(row, f"{{{W_NS}}}tc")
            ElementTree.SubElement(cell, f"{{{W_NS}}}tcPr")
            cell.append(create_docx_paragraph(cell_text))
    return table


def create_docx_paragraph(text: str) -> ElementTree.Element:
    paragraph = ElementTree.Element(f"{{{W_NS}}}p")
    if not text:
        return paragraph
    run = ElementTree.SubElement(paragraph, f"{{{W_NS}}}r")
    parts = text.split("\t")
    for index, part in enumerate(parts):
        if index:
            ElementTree.SubElement(run, f"{{{W_NS}}}tab")
        text_node = ElementTree.SubElement(run, f"{{{W_NS}}}t")
        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_node.text = part
    return paragraph


def lines_to_rtf(lines: list[str]) -> str:
    escaped_lines = [escape_rtf(line) + r"\par" for line in lines]
    return "{\\rtf1\\ansi\\deff0\n" + "\n".join(escaped_lines) + "\n}"


def escape_rtf(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def line_number(index: Any) -> int | None:
    return index + 1 if isinstance(index, int) else None


def clean_label(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip(" :-\t")
    return value


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def searchable_text(example: dict[str, Any]) -> str:
    fields = [
        example.get("study_id", ""),
        example.get("tlf_number", ""),
        example.get("tlf_type", ""),
        example.get("title", ""),
        example.get("population", ""),
        example.get("endpoint", ""),
        example.get("treatment_structure", ""),
        example.get("source_datasets", ""),
        example.get("dataset_path", ""),
        example.get("macros", ""),
        example.get("shell_document_path", ""),
        example.get("mddt_path", ""),
        example.get("mddt_text", "")[:20000],
        example.get("shell_text", ""),
        example.get("output_text", ""),
        example.get("program_text", "")[:20000],
    ]
    return "\n".join(str(field or "") for field in fields)


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|\d+(?:\.\d+)*", text.lower())
    return [token for token in tokens if token not in STOPWORDS]


def retrieve_examples(query_text: str, tlf_type: str = "", top_k: int = 5) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = [row_to_dict(row) for row in conn.execute("select * from examples order by id desc")]
    if not rows:
        return []

    docs = [tokenize(searchable_text(row)) for row in rows]
    doc_freq: Counter[str] = Counter()
    for doc in docs:
        doc_freq.update(set(doc))

    query_tokens = tokenize(query_text)
    query_counts = Counter(query_tokens)
    doc_count = len(rows)

    def weighted_vector(counts: Counter[str]) -> dict[str, float]:
        vector: dict[str, float] = {}
        for token, count in counts.items():
            idf = math.log((doc_count + 1) / (doc_freq.get(token, 0) + 1)) + 1
            vector[token] = (1 + math.log(count)) * idf
        return vector

    query_vector = weighted_vector(query_counts)
    query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1.0

    scored: list[tuple[float, dict[str, Any]]] = []
    for row, doc_tokens in zip(rows, docs):
        if tlf_type and row.get("tlf_type") and row["tlf_type"].lower() != tlf_type.lower():
            type_bonus = 0.0
        else:
            type_bonus = 0.08 if tlf_type else 0.0
        doc_vector = weighted_vector(Counter(doc_tokens))
        doc_norm = math.sqrt(sum(value * value for value in doc_vector.values())) or 1.0
        overlap = set(query_vector).intersection(doc_vector)
        score = sum(query_vector[token] * doc_vector[token] for token in overlap)
        score = score / (query_norm * doc_norm)
        score += type_bonus
        scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    result = []
    for score, row in scored[: max(1, min(top_k, 10))]:
        item = dict(row)
        item["score"] = round(score, 4)
        item["program_excerpt"] = excerpt(row.get("program_text", ""), 900)
        item["output_excerpt"] = excerpt(row.get("output_text", ""), 700)
        item["shell_excerpt"] = excerpt(row.get("shell_text", ""), 700)
        result.append(item)
    return result


def load_output_shape_examples(limit: int = 300) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select
                    id, study_id, tlf_number, tlf_type, title,
                    output_name, output_text, shell_name,
                    substr(shell_text, 1, 30000) as shell_text
                from examples
                where coalesce(output_text, '') <> ''
                order by id desc
                limit ?
                """,
                (max(1, min(limit, 1000)),),
            )
        ]
    return rows


def excerpt(text: str, limit: int = 600) -> str:
    normalized = normalize_text(text or "")
    return normalized[:limit] + ("..." if len(normalized) > limit else "")


def create_example(payload: dict[str, Any]) -> dict[str, Any]:
    program_name, _, program_text = decode_uploaded_file(payload.get("program_file"))
    output_name, _, output_text = decode_uploaded_file(payload.get("output_file"))
    shell_name, shell_raw, shell_text = decode_uploaded_file(payload.get("shell_file"))
    metadata = payload.get("metadata") or {}
    metadata = enrich_metadata_with_context_files(metadata, payload)
    if not shell_text and metadata.get("shell_document_path"):
        _, shell_name_from_path, shell_text_from_path = read_text_from_path(metadata["shell_document_path"])
        shell_name = shell_name_from_path
        shell_text = shell_text_from_path
    return insert_example(
        metadata,
        program_name,
        program_text,
        output_name,
        output_text,
        shell_name,
        shell_text,
        shell_raw,
    )


def enrich_metadata_with_context_files(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    enriched = enrich_metadata_with_paths(metadata)
    shell_documents = decode_uploaded_file_list(
        payload.get("shell_document_files") or payload.get("shell_document_file")
    )
    if shell_documents:
        enriched["shell_documents"] = shell_documents
        if len(shell_documents) == 1:
            enriched["shell_name"] = shell_documents[0]["name"]
            enriched["shell_text"] = shell_documents[0]["text"]
            enriched["shell_blob"] = shell_documents[0]["raw"]
    mddt_name, mddt_raw, mddt_text = decode_uploaded_file(payload.get("mddt_file"))
    if mddt_raw:
        enriched["mddt_name"] = mddt_name
        enriched["mddt_text"] = mddt_text
        enriched["mddt_blob"] = mddt_raw
        enriched["mddt_path"] = ""
    return enriched


def enrich_metadata_with_paths(metadata: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(metadata or {})
    for key in ("dataset_path", "shell_document_path", "mddt_path"):
        if enriched.get(key):
            enriched[key] = str(resolve_user_path(str(enriched[key])))
    if enriched.get("mddt_path") and not enriched.get("mddt_text"):
        _, mddt_name, mddt_text = read_text_from_path(enriched["mddt_path"])
        enriched["mddt_name"] = mddt_name
        enriched["mddt_text"] = mddt_text
    return enriched


def insert_example(
    metadata: dict[str, Any],
    program_name: str,
    program_text: str,
    output_name: str,
    output_text: str,
    shell_name: str = "",
    shell_text: str = "",
    shell_blob: bytes = b"",
) -> dict[str, Any]:
    shell_name = shell_name or metadata.get("shell_name", "")
    shell_text = shell_text or metadata.get("shell_text", "")
    shell_blob = shell_blob or metadata.get("shell_blob", b"")
    sas_info = parse_sas_program(program_text)
    shell_info = parse_shell_text(shell_text, shell_name) if shell_text else {}
    title = metadata.get("title") or shell_info.get("title") or first_or_empty(sas_info["titles"])
    population = metadata.get("population") or shell_info.get("population") or ""
    tlf_type = (metadata.get("tlf_type") or shell_info.get("tlf_type") or "table").lower()

    source_datasets = metadata.get("source_datasets") or ", ".join(
        sas_info["library_datasets"] or sas_info["datasets"]
    )
    macros = metadata.get("macros") or ", ".join(sas_info["macros"])
    extracted = {
        "sas": sas_info,
        "shell": shell_info,
        "paths": {
            "dataset_path": metadata.get("dataset_path", ""),
            "shell_document_path": metadata.get("shell_document_path", ""),
            "mddt_path": metadata.get("mddt_path", ""),
        },
    }

    with connect() as conn:
        cursor = conn.execute(
            """
            insert into examples (
                study_id, tlf_number, tlf_type, title, population, endpoint,
                treatment_structure, source_datasets, dataset_path, macros, notes,
                program_name, program_text, output_name, output_text,
                shell_document_path, shell_name, shell_text, shell_blob,
                mddt_path, mddt_name, mddt_text, mddt_blob, extracted_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata.get("study_id", ""),
                metadata.get("tlf_number") or shell_info.get("tlf_number", ""),
                tlf_type,
                title,
                population,
                metadata.get("endpoint", ""),
                metadata.get("treatment_structure", ""),
                source_datasets,
                metadata.get("dataset_path", ""),
                macros,
                metadata.get("notes", ""),
                program_name,
                program_text,
                output_name,
                output_text,
                metadata.get("shell_document_path", ""),
                shell_name,
                shell_text,
                sqlite3.Binary(shell_blob) if shell_blob else None,
                metadata.get("mddt_path", ""),
                metadata.get("mddt_name", ""),
                metadata.get("mddt_text", ""),
                sqlite3.Binary(metadata.get("mddt_blob", b"")) if metadata.get("mddt_blob") else None,
                json.dumps(extracted, indent=2),
                utc_now(),
            ),
        )
        example_id = cursor.lastrowid
        row = conn.execute("select * from examples where id = ?", (example_id,)).fetchone()
    return public_example(row_to_dict(row), include_text=True)


def first_or_empty(values: list[str]) -> str:
    return values[0] if values else ""


def public_example(example: dict[str, Any], include_text: bool = False) -> dict[str, Any]:
    result = {
        "id": example.get("id"),
        "study_id": example.get("study_id") or "",
        "tlf_number": example.get("tlf_number") or "",
        "tlf_type": example.get("tlf_type") or "",
        "title": example.get("title") or "",
        "population": example.get("population") or "",
        "endpoint": example.get("endpoint") or "",
        "treatment_structure": example.get("treatment_structure") or "",
        "source_datasets": example.get("source_datasets") or "",
        "dataset_path": example.get("dataset_path") or "",
        "macros": example.get("macros") or "",
        "notes": example.get("notes") or "",
        "program_name": example.get("program_name") or "",
        "output_name": example.get("output_name") or "",
        "shell_document_path": example.get("shell_document_path") or "",
        "shell_name": example.get("shell_name") or "",
        "shell_file_stored": bool(example.get("shell_blob")),
        "mddt_path": example.get("mddt_path") or "",
        "mddt_name": example.get("mddt_name") or "",
        "mddt_file_stored": bool(example.get("mddt_blob")),
        "created_at": example.get("created_at") or "",
    }
    if "score" in example:
        result["score"] = example["score"]
    for key in ("program_excerpt", "output_excerpt", "shell_excerpt"):
        if key in example:
            result[key] = example[key]
    if include_text:
        result.update(
            {
                "program_text": example.get("program_text") or "",
                "output_text": example.get("output_text") or "",
                "shell_text": example.get("shell_text") or "",
                "mddt_text": example.get("mddt_text") or "",
                "extracted_json": example.get("extracted_json") or "{}",
            }
        )
    return result


def scan_output_directory(payload: dict[str, Any]) -> dict[str, Any]:
    output_dir_text = str(payload.get("output_dir") or "").strip()
    if not output_dir_text:
        raise ValueError("Output directory is required.")

    scan_id = str(payload.get("scan_id") or "").strip()
    output_dir = resolve_user_path(output_dir_text)
    if not output_dir.exists() or not output_dir.is_dir():
        raise ValueError(f"Output directory does not exist or is not a directory: {output_dir}")

    recursive = as_bool(payload.get("recursive", True))
    max_files = max(1, min(int(payload.get("max_files") or 1000), 10000))
    study_id = str(payload.get("study_id") or "").strip()
    scan_metadata = enrich_metadata_with_context_files(
        {
            "study_id": study_id,
            "dataset_path": str(payload.get("dataset_path") or "").strip(),
            "shell_document_path": str(payload.get("shell_document_path") or "").strip(),
            "mddt_path": str(payload.get("mddt_path") or "").strip(),
        },
        payload,
    )
    update_scan_progress(
        scan_id,
        status="discovering_outputs",
        current_file="",
        current_file_name="Finding outputs...",
        scanned_so_far=0,
        total_files=0,
        created_count=0,
        matched_count=0,
        skipped_count=0,
        unmatched_count=0,
        results=[],
    )
    output_paths = list(iter_output_files(output_dir, recursive=recursive))[:max_files]

    explicit_program_dirs = [
        path for path in (resolve_user_path(item) for item in split_path_list(payload.get("program_dirs"))) if path.exists()
    ]
    program_roots = unique_paths(explicit_program_dirs + default_program_roots(output_dir))
    update_scan_progress(
        scan_id,
        status="indexing_programs",
        current_file="",
        current_file_name="Indexing SAS program files...",
        scanned_so_far=0,
        total_files=len(output_paths),
        created_count=0,
        matched_count=0,
        skipped_count=0,
        unmatched_count=0,
        results=[],
    )
    program_index = build_program_index(program_roots)

    results: list[dict[str, Any]] = []
    created_count = 0
    skipped_count = 0
    matched_count = 0
    unmatched_count = 0

    for index, output_path in enumerate(output_paths, start=1):
        item: dict[str, Any] = {
            "output_path": str(output_path),
            "output_name": output_path.name,
            "status": "unmatched",
            "program_path": "",
            "candidates": [],
            "message": "",
        }
        update_scan_progress(
            scan_id,
            status="running",
            current_file=str(output_path),
            current_file_name=output_path.name,
            scanned_so_far=index - 1,
            total_files=len(output_paths),
            created_count=created_count,
            matched_count=matched_count,
            skipped_count=skipped_count,
            unmatched_count=unmatched_count,
            results=results[-100:],
        )
        try:
            output_raw = output_path.read_bytes()
            output_text = extract_first_page_text(str(output_path), output_raw)
            candidates = extract_program_paths_from_output(output_text)
            item["candidates"] = candidates[:8]
            program_path = resolve_program_path(candidates, output_path, program_roots, program_index)

            if not program_path:
                unmatched_count += 1
                item["message"] = "No Program: SAS path was resolved from the first page."
                results.append(item)
                update_scan_progress(
                    scan_id,
                    status="running",
                    current_file=str(output_path),
                    current_file_name=output_path.name,
                    scanned_so_far=index,
                    total_files=len(output_paths),
                    created_count=created_count,
                    matched_count=matched_count,
                    skipped_count=skipped_count,
                    unmatched_count=unmatched_count,
                    results=results[-100:],
                )
                continue

            matched_count += 1
            item["program_path"] = str(program_path)
            program_text = program_path.read_text(encoding="utf-8", errors="replace")
            metadata = infer_metadata_from_output(output_text, output_path, study_id)
            metadata.update(
                {
                    "dataset_path": scan_metadata.get("dataset_path", ""),
                    "shell_document_path": scan_metadata.get("shell_document_path", ""),
                    "mddt_path": scan_metadata.get("mddt_path", ""),
                    "mddt_name": scan_metadata.get("mddt_name", ""),
                    "mddt_text": scan_metadata.get("mddt_text", ""),
                    "mddt_blob": scan_metadata.get("mddt_blob", b""),
                }
            )
            shell_name, shell_text, shell_blob = select_shell_document_for_output(
                scan_metadata.get("shell_documents", []),
                output_path,
                metadata.get("tlf_number", ""),
            )
            if shell_name:
                item["shell_name"] = shell_name
                metadata["shell_name"] = shell_name
                metadata["shell_text"] = shell_text
                metadata["shell_blob"] = shell_blob
            else:
                shell_path, shell_name, shell_text = resolve_shell_document_for_output(
                    metadata.get("shell_document_path", ""),
                    output_path,
                    metadata.get("tlf_number", ""),
                )
                shell_blob = b""
                if shell_path:
                    item["shell_document_path"] = shell_path
                    metadata["shell_document_path"] = shell_path
            metadata["notes"] = "\n".join(
                value
                for value in [
                    "Auto-paired from output directory scan.",
                    "Program path was read from the first page of the output.",
                    f"Output path: {output_path}",
                    f"Dataset path: {metadata.get('dataset_path', '')}",
                    f"Shell file stored: {metadata.get('shell_name', shell_name)}",
                    f"MDDT file stored: {metadata.get('mddt_name', '')}",
                    f"Resolved program path: {program_path}",
                    f"Program candidates from first page: {', '.join(candidates[:5])}",
                ]
                if value
            )

            existing_id = find_existing_example(study_id, str(output_path), str(program_path))
            if existing_id:
                skipped_count += 1
                item["status"] = "skipped_existing"
                item["example_id"] = existing_id
                item["message"] = "Pair already exists in the knowledge base."
                results.append(item)
                update_scan_progress(
                    scan_id,
                    status="running",
                    current_file=str(output_path),
                    current_file_name=output_path.name,
                    scanned_so_far=index,
                    total_files=len(output_paths),
                    created_count=created_count,
                    matched_count=matched_count,
                    skipped_count=skipped_count,
                    unmatched_count=unmatched_count,
                    results=results[-100:],
                )
                continue

            example = insert_example(
                metadata,
                str(program_path),
                program_text,
                str(output_path),
                output_text,
                shell_name,
                shell_text,
                shell_blob,
            )
            created_count += 1
            item["status"] = "created"
            item["example_id"] = example["id"]
            item["tlf_number"] = example.get("tlf_number", "")
            item["title"] = example.get("title", "")
            item["source_datasets"] = example.get("source_datasets", "")
            results.append(item)
        except Exception as exc:
            unmatched_count += 1
            item["status"] = "error"
            item["message"] = f"{type(exc).__name__}: {exc}"
            results.append(item)
        update_scan_progress(
            scan_id,
            status="running",
            current_file=str(output_path),
            current_file_name=output_path.name,
            scanned_so_far=index,
            total_files=len(output_paths),
            created_count=created_count,
            matched_count=matched_count,
            skipped_count=skipped_count,
            unmatched_count=unmatched_count,
            results=results[-100:],
        )

    final_result = {
        "output_dir": str(output_dir),
        "recursive": recursive,
        "first_page_only": True,
        "program_roots": [str(path) for path in program_roots],
        "program_index_count": sum(len(value) for value in program_index.values()),
        "scanned_count": len(output_paths),
        "matched_count": matched_count,
        "created_count": created_count,
        "skipped_count": skipped_count,
        "unmatched_count": unmatched_count,
        "results": results,
    }
    update_scan_progress(
        scan_id,
        status="completed",
        current_file="",
        current_file_name="",
        scanned_so_far=len(output_paths),
        total_files=len(output_paths),
        created_count=created_count,
        matched_count=matched_count,
        skipped_count=skipped_count,
        unmatched_count=unmatched_count,
        results=results[-100:],
        final_result=final_result,
    )
    return final_result


def start_output_directory_scan(payload: dict[str, Any]) -> dict[str, Any]:
    scan_payload = dict(payload)
    scan_id = str(scan_payload.get("scan_id") or uuid.uuid4()).strip()
    scan_payload["scan_id"] = scan_id
    update_scan_progress(
        scan_id,
        status="queued",
        current_file="",
        current_file_name="",
        scanned_so_far=0,
        total_files=0,
        created_count=0,
        matched_count=0,
        skipped_count=0,
        unmatched_count=0,
        results=[],
    )
    thread = threading.Thread(target=run_output_directory_scan_job, args=(scan_payload,), daemon=True)
    thread.start()
    progress = get_scan_progress(scan_id)
    progress["started"] = True
    return progress


def run_output_directory_scan_job(payload: dict[str, Any]) -> None:
    scan_id = str(payload.get("scan_id") or "")
    try:
        scan_output_directory(payload)
    except Exception as exc:
        update_scan_progress(
            scan_id,
            status="failed",
            current_file="",
            current_file_name="",
            error=f"{type(exc).__name__}: {exc}",
        )


def update_scan_progress(scan_id: str, **updates: Any) -> None:
    if not scan_id:
        return
    with SCAN_PROGRESS_LOCK:
        current = SCAN_PROGRESS.get(scan_id, {"scan_id": scan_id, "updated_at": utc_now()})
        current.update(updates)
        current["scan_id"] = scan_id
        current["updated_at"] = utc_now()
        SCAN_PROGRESS[scan_id] = current


def get_scan_progress(scan_id: str) -> dict[str, Any]:
    with SCAN_PROGRESS_LOCK:
        return dict(SCAN_PROGRESS.get(scan_id, {"scan_id": scan_id, "status": "unknown"}))


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def resolve_user_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value).strip().strip('"')))).resolve()


def split_path_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key not in seen and resolved.exists() and resolved.is_dir():
            seen.add(key)
            result.append(resolved)
    return result


def default_program_roots(output_dir: Path) -> list[Path]:
    roots = [output_dir]
    parents = unique_paths([output_dir.parent, output_dir.parent.parent])
    roots.extend(parents)
    likely_names = ["programs", "program", "pgm", "pgms", "sas", "source", "programming"]
    for parent in parents:
        roots.extend(parent / name for name in likely_names)
    return roots


def iter_output_files(output_dir: Path, recursive: bool) -> list[Path]:
    iterator = output_dir.rglob("*") if recursive else output_dir.iterdir()
    return sorted(
        [
            path
            for path in iterator
            if path.is_file()
            and path.suffix.lower() in OUTPUT_EXTENSIONS
            and not path.name.startswith("~$")
        ],
        key=lambda path: str(path).lower(),
    )


def build_program_index(program_roots: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    scanned = 0
    for root in program_roots:
        try:
            candidates = root.rglob("*.sas")
            for path in candidates:
                if not path.is_file():
                    continue
                index.setdefault(path.name.lower(), []).append(path.resolve())
                scanned += 1
                if scanned >= 50000:
                    return index
        except OSError:
            continue
    return index


def extract_program_paths_from_output(output_text: str) -> list[str]:
    if not output_text:
        return []
    lines = output_text.splitlines()
    program_lines = [line for line in lines if re.search(r"(?i)\bprogram\s*:", line) and ".sas" in line.lower()]
    program_candidates: list[str] = []
    for line in program_lines:
        match = re.search(r"(?i)\bprogram\s*:\s*(.+?\.sas)\b", line.strip())
        if match:
            candidate = clean_program_candidate(match.group(1))
            if candidate:
                program_candidates.append(candidate)
    if program_candidates:
        return unique_preserve(program_candidates)

    focus_lines = lines
    patterns = [
        r"(?i)([A-Za-z]:[\\/][^\r\n\"<>|?*]*?\.sas)",
        r"(?i)(\\\\[^\r\n\"<>|?*]*?\.sas)",
        r"(?i)(/[^\r\n\"<>]*?\.sas)",
        r"(?i)((?:\.{1,2}[\\/])?(?:[A-Za-z0-9_. -]+[\\/])+[A-Za-z0-9_. -]+\.sas)",
        r"(?i)\b([A-Za-z0-9_. -]+\.sas)\b",
    ]
    candidates: list[str] = []
    for line in focus_lines:
        if ".sas" not in line.lower():
            continue
        cleaned_line = line.strip()
        for pattern in patterns:
            for match in re.finditer(pattern, cleaned_line):
                candidate = clean_program_candidate(match.group(1))
                if candidate:
                    candidates.append(candidate)
    return unique_preserve(candidates)


def clean_program_candidate(candidate: str) -> str:
    candidate = html.unescape(candidate)
    candidate = candidate.replace("/", os.sep).replace("\\", os.sep)
    candidate = re.sub(r"(?i)^.*?(program|source|path|file)\s*[:=]\s*", "", candidate).strip()
    candidate = candidate.strip().strip("'\"`,;)]}")
    candidate = candidate.rstrip(".")
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate if candidate.lower().endswith(".sas") else ""


def resolve_program_path(
    candidates: list[str],
    output_path: Path,
    program_roots: list[Path],
    program_index: dict[str, list[Path]],
) -> Path | None:
    for candidate in candidates:
        possible = candidate_to_paths(candidate, output_path, program_roots)
        for path in possible:
            if path.exists() and path.is_file() and path.suffix.lower() in PROGRAM_EXTENSIONS:
                return path.resolve()
        name = Path(candidate.replace("\\", "/")).name.lower()
        if name in program_index and program_index[name]:
            return program_index[name][0]
    return None


def select_shell_document_for_output(
    shell_documents: list[dict[str, Any]],
    output_path: Path,
    tlf_number: str,
) -> tuple[str, str, bytes]:
    if not shell_documents:
        return "", "", b""
    if len(shell_documents) == 1:
        item = shell_documents[0]
        return item["name"], item["text"], item["raw"]

    output_key = normalize_match_key(output_path.stem)
    tlf_key = normalize_match_key(tlf_number)
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in shell_documents:
        name_key = normalize_match_key(Path(item["name"]).stem)
        text_key = normalize_match_key((item.get("text") or "")[:2000])
        score = 0
        if output_key and (name_key == output_key or output_key in name_key or name_key in output_key):
            score += 10
        if tlf_key and (tlf_key in name_key or tlf_key in text_key):
            score += 6
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        return "", "", b""
    item = scored[0][1]
    return item["name"], item["text"], item["raw"]


def resolve_shell_document_for_output(
    shell_document_path: str,
    output_path: Path,
    tlf_number: str,
) -> tuple[str, str, str]:
    if not shell_document_path:
        return "", "", ""
    path = resolve_user_path(shell_document_path)
    if path.is_file():
        raw = path.read_bytes()
        return str(path), path.name, extract_text(str(path), raw)
    if not path.is_dir():
        return str(path), "", ""

    candidates = find_matching_context_files(path, output_path, tlf_number)
    if not candidates:
        return str(path), "", ""
    shell_path = candidates[0]
    raw = shell_path.read_bytes()
    return str(shell_path), shell_path.name, extract_text(str(shell_path), raw)


def find_matching_context_files(root: Path, output_path: Path, tlf_number: str) -> list[Path]:
    extensions = {".txt", ".rtf", ".docx", ".xlsx", ".pdf", ".html", ".htm"}
    output_stem = normalize_match_key(output_path.stem)
    tlf_key = normalize_match_key(tlf_number)
    matches: list[tuple[int, Path]] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            key = normalize_match_key(path.stem)
            score = 0
            if output_stem and (key == output_stem or output_stem in key or key in output_stem):
                score += 10
            if tlf_key and tlf_key in key:
                score += 6
            if score:
                matches.append((score, path.resolve()))
    except OSError:
        return []
    matches.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    return [path for _, path in matches]


def normalize_match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def candidate_to_paths(candidate: str, output_path: Path, program_roots: list[Path]) -> list[Path]:
    expanded = os.path.expandvars(os.path.expanduser(candidate.strip().strip('"')))
    candidate_path = Path(expanded)
    paths: list[Path] = []
    if candidate_path.is_absolute():
        paths.append(candidate_path)
    else:
        paths.append(output_path.parent / candidate_path)
        for root in program_roots:
            paths.append(root / candidate_path)
            paths.append(root / candidate_path.name)
    return paths


def infer_metadata_from_output(output_text: str, output_path: Path, study_id: str) -> dict[str, str]:
    shell_info = parse_shell_text(output_text[:30000], output_path.name)
    title_info = extract_tlf_title_from_first_page(output_text)
    tlf_number = title_info.get("tlf_number") or shell_info.get("tlf_number") or infer_tlf_number_from_name(output_path.name)
    tlf_type = title_info.get("tlf_type") or infer_tlf_type_from_name(output_path.name) or shell_info.get("tlf_type") or "table"
    title = title_info.get("title") or shell_info.get("title") or ""
    if tlf_number:
        title = re.sub(
            rf"(?i)^(table|listing|figure|t|l|f)?\s*[:.-]?\s*{re.escape(tlf_number)}\s*[:.-]?\s*",
            "",
            title,
        ).strip() or title
    return {
        "study_id": study_id,
        "tlf_number": tlf_number,
        "tlf_type": tlf_type,
        "title": title,
        "population": shell_info.get("population", ""),
    }


def extract_tlf_title_from_first_page(output_text: str) -> dict[str, str]:
    lines = [line.strip() for line in normalize_text(output_text).splitlines() if line.strip()]
    header_mark = re.compile(
        r"(?i)^\s*(?P<marker>table|listing|figure|t|l|f)\s*[:#.-]?\s*"
        r"(?P<number>\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?)\b"
        r"\s*[:.-]?\s*(?P<title>.*)$"
    )
    marker_only = re.compile(r"(?i)^\s*(?P<marker>table|listing|figure|t|l|f)\s*[:#.-]?\s*$")
    number_line = re.compile(
        r"(?i)^\s*(?P<number>\d{1,3}(?:[.-]\d+[A-Za-z]?){1,8}[A-Za-z]?)\b"
        r"\s*[:.-]?\s*(?P<title>.*)$"
    )
    stop_mark = re.compile(
        r"(?i)^(program|footnote|note|page|population|source|dataset|created|generated|run date|output)\s*:"
        r"|^(parameter|subject id|subject|visit|treatment|category|result|age|sex|race)\b"
    )
    for index, line in enumerate(lines):
        match = header_mark.match(line)
        start_index = index + 1
        if not match:
            marker_match = marker_only.match(line)
            if not marker_match or index + 1 >= len(lines):
                continue
            next_match = number_line.match(lines[index + 1])
            if not next_match:
                continue
            marker = marker_match.group("marker")
            number = next_match.group("number")
            first_title = next_match.group("title")
            start_index = index + 2
        else:
            marker = match.group("marker")
            number = match.group("number")
            first_title = match.group("title")

        tlf_type = marker_to_tlf_type(marker)
        title_lines = collect_tlf_title_lines(
            lines,
            start_index,
            first_title,
            stop_mark,
            header_mark,
            marker_only,
        )
        title = " ".join(title_lines).strip()
        return {"tlf_type": tlf_type, "tlf_number": number, "title": title}
    return {"tlf_type": "", "tlf_number": "", "title": ""}


def marker_to_tlf_type(marker: str) -> str:
    value = marker.lower()
    if value in {"listing", "l"}:
        return "listing"
    if value in {"figure", "f"}:
        return "figure"
    return "table"


def collect_tlf_title_lines(
    lines: list[str],
    start_index: int,
    first_title: str,
    stop_mark: re.Pattern[str],
    header_mark: re.Pattern[str],
    marker_only: re.Pattern[str],
) -> list[str]:
    title_lines: list[str] = []
    first_piece = clean_title_line(first_title)
    if first_piece:
        title_lines.append(first_piece)
    for next_line in lines[start_index : start_index + 5]:
        if len(title_lines) >= 5:
            break
        if (
            header_mark.match(next_line)
            or marker_only.match(next_line)
            or stop_mark.match(next_line)
            or re.search(r"(?i)\(N\s*=", next_line)
            or "|" in next_line
        ):
            break
        cleaned = clean_title_line(next_line)
        if cleaned:
            title_lines.append(cleaned)
    return title_lines


def clean_title_line(value: str) -> str:
    value = clean_label(value)
    value = re.sub(r"(?i)^title\d*\s*[:.-]?\s*", "", value).strip()
    return value


def infer_tlf_number_from_name(filename: str) -> str:
    stem = Path(filename).stem
    match = re.search(r"(\d{1,2}(?:[._-]\d+){1,5})", stem)
    return match.group(1).replace("_", ".").replace("-", ".") if match else ""


def infer_tlf_type_from_name(filename: str) -> str:
    stem = Path(filename).stem.lower()
    if re.match(r"^(l_|lst_|listing)", stem):
        return "listing"
    if re.match(r"^(f_|fig_|figure)", stem):
        return "figure"
    if re.match(r"^(t_|tbl_|table)", stem):
        return "table"
    return ""


def find_existing_example(study_id: str, output_name: str, program_name: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            """
            select id
            from examples
            where coalesce(study_id, '') = ?
              and output_name = ?
              and program_name = ?
            limit 1
            """,
            (study_id, output_name, program_name),
        ).fetchone()
    return int(row["id"]) if row else None


def clear_knowledge_base() -> dict[str, Any]:
    with connect() as conn:
        example_count = conn.execute("select count(*) as count from examples").fetchone()["count"]
        run_count = conn.execute("select count(*) as count from generation_runs").fetchone()["count"]
        conn.execute("delete from examples")
        conn.execute("delete from generation_runs")
        conn.execute("delete from sqlite_sequence where name in ('examples', 'generation_runs')")
    with SCAN_PROGRESS_LOCK:
        SCAN_PROGRESS.clear()
    return {
        "cleared": True,
        "examples_deleted": example_count,
        "runs_deleted": run_count,
    }


def generate_from_shell(payload: dict[str, Any]) -> dict[str, Any]:
    shell_name, _, shell_text = decode_uploaded_file(payload.get("shell_file"))
    metadata = payload.get("metadata") or {}
    metadata = enrich_metadata_with_context_files(metadata, payload)
    if metadata.get("program_output_dir"):
        metadata["program_output_dir"] = str(resolve_user_path(metadata["program_output_dir"]))
    if not shell_text and metadata.get("shell_document_path"):
        _, shell_name_from_path, shell_text_from_path = read_text_from_path(metadata["shell_document_path"])
        shell_name = shell_name_from_path
        shell_text = shell_text_from_path
    if not shell_text and metadata.get("shell_text"):
        shell_name = metadata.get("shell_name", shell_name)
        shell_text = metadata.get("shell_text", shell_text)
    shell_info = parse_shell_text(shell_text, shell_name)
    if metadata.get("tlf_type"):
        shell_info["tlf_type"] = metadata["tlf_type"]
    if metadata.get("tlf_number"):
        shell_info["tlf_number"] = metadata["tlf_number"]
    if metadata.get("title"):
        shell_info["title"] = metadata["title"]
    if metadata.get("population"):
        shell_info["population"] = metadata["population"]

    query = "\n".join(
        [
            shell_text,
            metadata.get("study_id", ""),
            metadata.get("tlf_number", ""),
            metadata.get("title", ""),
            metadata.get("population", ""),
            metadata.get("source_datasets", ""),
            metadata.get("dataset_path", ""),
            metadata.get("mddt_text", "")[:12000],
        ]
    )
    top_k = int(payload.get("top_k") or 5)
    retrieved = retrieve_examples(query, shell_info.get("tlf_type", ""), top_k=top_k)
    use_llm = as_bool(payload.get("use_llm", True))
    generation = generate_sas_program(shell_info, shell_text, retrieved, metadata, use_llm=use_llm)
    program = generation["program"]
    saved = save_generated_program(program, shell_info, metadata)
    validation = validate_program(program, shell_info=shell_info)
    if generation.get("warning"):
        validation.setdefault("findings", []).append(issue("info", "Generation note", generation["warning"]))
    if saved.get("error"):
        validation.setdefault("findings", []).append(issue("warning", "Program was not saved", saved["error"]))
        if validation.get("status") == "passed":
            validation["status"] = "passed_with_warnings"
    retrieved_public = [public_example(item) for item in retrieved]

    with connect() as conn:
        cursor = conn.execute(
            """
            insert into generation_runs (
                shell_name, shell_text, shell_json, generated_program,
                generated_program_path, generation_method,
                retrieval_json, validation_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shell_name,
                shell_text,
                json.dumps(shell_info, indent=2),
                program,
                saved.get("path", ""),
                generation.get("method", ""),
                json.dumps(retrieved_public, indent=2),
                json.dumps(validation, indent=2),
                utc_now(),
            ),
        )
        run_id = cursor.lastrowid

    return {
        "run_id": run_id,
        "shell": shell_info,
        "retrieved": retrieved_public,
        "program": program,
        "program_path": saved.get("path", ""),
        "program_saved": saved.get("saved", False),
        "generation_method": generation.get("method", ""),
        "generation_warning": generation.get("warning", ""),
        "validation": validation,
    }


def generate_sas_program(
    shell_info: dict[str, Any],
    shell_text: str,
    retrieved: list[dict[str, Any]],
    metadata: dict[str, Any],
    use_llm: bool = True,
) -> dict[str, Any]:
    fallback_program = generate_rule_based_sas_program(shell_info, shell_text, retrieved, metadata)
    if not use_llm:
        return {"program": fallback_program, "method": "rule_based", "warning": ""}
    llm_result = generate_sas_program_with_llm(shell_info, shell_text, retrieved, metadata)
    if llm_result.get("program"):
        return llm_result
    return {
        "program": fallback_program,
        "method": "rule_based_fallback",
        "warning": llm_result.get("warning") or "LLM generation was unavailable; used rule-based fallback.",
    }


def generate_rule_based_sas_program(
    shell_info: dict[str, Any],
    shell_text: str,
    retrieved: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> str:
    title = shell_info.get("title") or metadata.get("title") or "Generated TLF"
    tlf_type = shell_info.get("tlf_type") or metadata.get("tlf_type") or "table"
    tlf_number = shell_info.get("tlf_number") or metadata.get("tlf_number") or "x_x_x"
    outstem = sas_output_stem(tlf_type, tlf_number)
    source_datasets = metadata.get("source_datasets") or first_non_empty(
        [item.get("source_datasets", "") for item in retrieved]
    )
    source_macros = metadata.get("macros") or first_non_empty([item.get("macros", "") for item in retrieved])
    dataset_path = metadata.get("dataset_path") or first_non_empty([item.get("dataset_path", "") for item in retrieved])
    shell_document_path = metadata.get("shell_document_path") or first_non_empty(
        [item.get("shell_document_path", "") for item in retrieved]
    )
    shell_source = shell_document_path or metadata.get("shell_name") or first_non_empty(
        [item.get("shell_name", "") for item in retrieved]
    )
    mddt_path = metadata.get("mddt_path") or first_non_empty([item.get("mddt_path", "") for item in retrieved])
    mddt_source = mddt_path or metadata.get("mddt_name") or first_non_empty(
        [item.get("mddt_name", "") for item in retrieved]
    )

    header = [
        "/*****************************************************************************",
        f"* Program: {outstem}.sas",
        f"* Purpose: AI-assisted first draft for {tlf_type} {tlf_number}",
        f"* Generated: {utc_now()}",
        f"* Generator: SAS TLF Assistant MVP {APP_VERSION}",
        "*",
        "* Review checklist:",
        "* - Confirm libnames, input datasets, populations, variables, and treatment arms.",
        "* - Run the program in SAS and review the log before approving the output.",
        "* - Compare generated titles, footnotes, columns, and row labels against the shell.",
        "*****************************************************************************/",
        "",
        "options nodate nonumber missing=' ';",
        "%let outdir = .;",
        f"%let outname = {outstem}.rtf;",
        "%let outpath = &outdir./&outname;",
        "",
    ]
    if dataset_path:
        header.extend(
            [
                f"%let adam_path = {dataset_path.replace(';', '')};",
                'libname adam "&adam_path.";',
                "",
            ]
        )

    if source_datasets:
        header.append(f"/* Retrieved/source datasets to review: {source_datasets} */")
    if dataset_path:
        header.append(f"/* Dataset path: {dataset_path} */")
    if shell_source:
        header.append(f"/* Shell document: {shell_source} */")
    if mddt_source:
        header.append(f"/* MDDT file: {mddt_source} */")
    if source_macros:
        header.append(f"/* Retrieved/source macros to review: {source_macros} */")
    if retrieved:
        header.extend(
            [
                f"/* Closest prior example: {retrieved[0].get('study_id', '')} "
                f"{retrieved[0].get('tlf_number', '')} "
                f"({retrieved[0].get('score', 0):.3f}) - {retrieved[0].get('title', '')} */",
                "",
            ]
        )

    title_block = sas_title_block(title, shell_info)
    body = ""
    best_score = float(retrieved[0].get("score", 0)) if retrieved else 0.0
    if retrieved and retrieved[0].get("program_text") and best_score >= 0.45:
        body = adapt_prior_program(retrieved[0]["program_text"])
    else:
        body = shell_scaffold_program(shell_info)

    if "ods rtf" not in body.lower():
        body = "ods rtf file=\"&outpath\" style=journal bodytitle;\n\n" + body
        if "ods rtf close" not in body.lower():
            body += "\n\nods rtf close;\n"

    return "\n".join(header) + title_block + "\n\n" + body.strip() + "\n"


def generate_sas_program_with_llm(
    shell_info: dict[str, Any],
    shell_text: str,
    retrieved: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "program": "",
            "method": "llm_unavailable",
            "warning": "OPENAI_API_KEY is not configured; used rule-based fallback.",
        }

    prompt = build_llm_generation_prompt(shell_info, shell_text, retrieved, metadata)
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request_body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior clinical SAS statistical programmer. "
                    "Generate production-oriented SAS TLF programs using the provided shell, "
                    "MDDT metadata, ADaM dataset path, and retrieved historical examples. "
                    "Return SAS code only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        data = openai_chat_completion(base_url, api_key, request_body, timeout=90)
        content = data["choices"][0]["message"]["content"]
        program = extract_sas_code(content)
        if not program.strip():
            return {
                "program": "",
                "method": "llm_empty_response",
                "warning": "LLM returned no SAS code; used rule-based fallback.",
            }
        return {"program": program, "method": f"llm:{model}", "warning": ""}
    except Exception as exc:
        return {
            "program": "",
            "method": "llm_error",
            "warning": f"LLM generation failed: {type(exc).__name__}: {exc}",
        }


def build_llm_generation_prompt(
    shell_info: dict[str, Any],
    shell_text: str,
    retrieved: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> str:
    examples = []
    for index, item in enumerate(retrieved[:5], start=1):
        examples.append(
            textwrap.dedent(
                f"""
                === Retrieved Example {index} ===
                Study: {item.get('study_id', '')}
                TLF: {item.get('tlf_number', '')} ({item.get('tlf_type', '')})
                Title: {item.get('title', '')}
                Source datasets: {item.get('source_datasets', '')}
                Macros: {item.get('macros', '')}
                Shell excerpt:
                {excerpt(item.get('shell_text', ''), 1200)}
                Output excerpt:
                {excerpt(item.get('output_text', ''), 1200)}
                Program excerpt:
                {excerpt(item.get('program_text', ''), 3500)}
                """
            ).strip()
        )

    return textwrap.dedent(
        f"""
        Generate a runnable SAS program for a new clinical study TLF.

        Requirements:
        - Use the shell and MDDT metadata as the primary specification.
        - Use retrieved historical programs as patterns, not as hard-coded truth.
        - Include libname/path setup placeholders based on the ADaM dataset path.
        - Prefer source datasets listed in the MDDT or retrieved examples when appropriate.
        - Include titles and footnotes from the shell.
        - Write an ODS RTF output.
        - Include clear comments for assumptions requiring programmer review.
        - Return only SAS code. Do not wrap the answer in markdown prose.

        New study metadata:
        Study ID: {metadata.get('study_id', '')}
        TLF type: {shell_info.get('tlf_type', '')}
        TLF number: {shell_info.get('tlf_number', '')}
        Title: {shell_info.get('title', '')}
        Population: {shell_info.get('population', '')}
        ADaM dataset path: {metadata.get('dataset_path', '')}
        Source datasets entered by user: {metadata.get('source_datasets', '')}
        Shell file: {metadata.get('shell_name', '')}
        MDDT file: {metadata.get('mddt_name', '')}

        Parsed shell JSON:
        {json.dumps(shell_info, indent=2)}

        Shell text:
        {excerpt(shell_text, 8000)}

        MDDT text:
        {excerpt(metadata.get('mddt_text', ''), 8000)}

        Retrieved knowledge base context:
        {chr(10).join(examples) if examples else 'No retrieved examples available.'}
        """
    ).strip()


def extract_sas_code(content: str) -> str:
    match = re.search(r"```(?:sas)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return content.strip() + "\n"


def save_generated_program(program_text: str, shell_info: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    output_dir = str(metadata.get("program_output_dir") or "").strip()
    if not output_dir:
        return {"saved": False, "path": "", "error": "No SAS program output folder was provided."}
    try:
        output_path = resolve_user_path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        filename = sas_output_stem(
            shell_info.get("tlf_type") or metadata.get("tlf_type") or "table",
            shell_info.get("tlf_number") or metadata.get("tlf_number") or "x_x_x",
        )
        program_path = unique_program_path(output_path / f"{filename}.sas")
        program_path.write_text(program_text, encoding="utf-8")
        return {"saved": True, "path": str(program_path), "error": ""}
    except Exception as exc:
        return {"saved": False, "path": "", "error": f"{type(exc).__name__}: {exc}"}


def unique_program_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_v{index}{suffix}")
        if not candidate.exists():
            return candidate
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{stem}_{stamp}{suffix}")


def first_non_empty(values: list[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def sas_output_stem(tlf_type: str, tlf_number: str) -> str:
    prefix = {"table": "t", "listing": "l", "figure": "f"}.get(tlf_type.lower(), "t")
    clean_number = re.sub(r"[^0-9A-Za-z]+", "_", tlf_number or "x_x_x").strip("_")
    return f"{prefix}_{clean_number or 'x_x_x'}"


def sas_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sas_title_block(title: str, shell_info: dict[str, Any]) -> str:
    lines = ["title; footnote;"]
    lines.append(f"title1 {sas_literal(title)};")
    if shell_info.get("population"):
        lines.append(f"title2 {sas_literal(shell_info['population'])};")
    for index, footnote in enumerate(shell_info.get("footnotes") or [], start=1):
        lines.append(f"footnote{index} {sas_literal(footnote)};")
    return "\n".join(lines) + "\n"


def adapt_prior_program(program_text: str) -> str:
    lines = program_text.splitlines()
    filtered: list[str] = []
    for line in lines:
        if re.match(r"^\s*(title|footnote)\d*\b", line, flags=re.I):
            continue
        filtered.append(line)
    body = "\n".join(filtered)
    body = re.sub(
        r"(?is)ods\s+rtf\s+file\s*=\s*(\"[^\"]*\"|'[^']*'|[^ ;]+)([^;]*);",
        "ods rtf file=\"&outpath\"\\2;",
        body,
        count=1,
    )
    if "ods rtf" in body.lower():
        body = re.sub(r"(?i)ods\s+rtf\s+file=", "ods rtf file=", body, count=1)
    return (
        "/* Body adapted from the closest prior program. Review study-specific "
        "libnames, filters, variables, and macros before production use. */\n"
        + body.strip()
    )


def shell_scaffold_program(shell_info: dict[str, Any]) -> str:
    rows = shell_info.get("rows") or default_rows(shell_info)
    columns = shell_info.get("columns") or ["Treatment A", "Treatment B", "Total"]
    columns = columns[:8]

    lines = [
        "/* No high-similarity prior program was selected. This runnable scaffold",
        "   reproduces the shell structure and gives programmers a starting point",
        "   for derivations. */",
        "data tlf_shell;",
        "  length row_order 8 row_label $200 " + " ".join(f"col{i + 1} $80" for i in range(len(columns))) + ";",
    ]
    for index, row in enumerate(rows[:60], start=1):
        assignments = [
            f"row_order={index}",
            f"row_label={sas_literal(row)}",
        ]
        assignments.extend(f"col{i + 1}=''" for i in range(len(columns)))
        lines.append("  " + "; ".join(assignments) + "; output;")
    lines.extend(
        [
            "run;",
            "",
            "proc report data=tlf_shell nowd headline headskip split='|';",
            "  columns row_order row_label " + " ".join(f"col{i + 1}" for i in range(len(columns))) + ";",
            "  define row_order / order noprint;",
            "  define row_label / display 'Parameter';",
        ]
    )
    for index, column in enumerate(columns, start=1):
        lines.append(f"  define col{index} / display {sas_literal(column)} center;")
    lines.extend(["run;"])
    return "\n".join(lines)


def default_rows(shell_info: dict[str, Any]) -> list[str]:
    stats = shell_info.get("statistics") or []
    if shell_info.get("tlf_type") == "listing":
        return ["Subject ID", "Treatment", "Visit", "Result"]
    if shell_info.get("tlf_type") == "figure":
        return ["Figure data source", "X-axis", "Y-axis", "Grouping"]
    if stats:
        return [stat.upper() if stat == "n" else stat.title() for stat in stats]
    return ["N", "Mean", "SD", "Median", "Min, Max"]


def validate_program(
    program_text: str,
    shell_info: dict[str, Any] | None = None,
    log_text: str = "",
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    lower = program_text.lower()

    if "ods rtf" not in lower and "ods pdf" not in lower and "ods html" not in lower:
        findings.append(issue("warning", "No ODS destination found", "Program may not create a TLF output file."))
    if "ods rtf" in lower and "ods rtf close" not in lower:
        findings.append(issue("warning", "ODS RTF is not closed", "Add `ods rtf close;` after output generation."))
    if "title1" not in lower:
        findings.append(issue("warning", "Missing title1", "Generated output should include a shell title."))
    if program_text.count("'") % 2:
        findings.append(issue("error", "Unbalanced single quotes", "SAS string literals may be broken."))
    if program_text.count('"') % 2:
        findings.append(issue("error", "Unbalanced double quotes", "SAS string literals may be broken."))
    todo_count = len(re.findall(r"(?i)\b(todo|review|confirm)\b", program_text))
    if todo_count:
        findings.append(issue("info", "Review markers present", f"{todo_count} review marker(s) should be resolved."))

    if shell_info:
        title = (shell_info.get("title") or "").strip()
        if title and title.lower() not in lower:
            findings.append(issue("warning", "Shell title not found", "Generated SAS does not contain the shell title."))
        for footnote in shell_info.get("footnotes") or []:
            if footnote.lower() not in lower:
                findings.append(issue("warning", "Shell footnote not found", f"Missing footnote: {footnote}"))

    log_issues = parse_sas_log(log_text)
    findings.extend(log_issues)

    sas_executable = os.environ.get("SAS_EXECUTABLE", "")
    run_capability = (
        "SAS_EXECUTABLE is configured; server-side SAS execution can be added."
        if sas_executable
        else "SAS_EXECUTABLE is not configured; validation is static/log-based."
    )
    status = "passed"
    if any(item["severity"] == "error" for item in findings):
        status = "failed"
    elif any(item["severity"] == "warning" for item in findings):
        status = "passed_with_warnings"

    return {
        "status": status,
        "findings": findings,
        "run_capability": run_capability,
    }


def issue(severity: str, title: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "title": title, "detail": detail}


def parse_sas_log(log_text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not log_text.strip():
        return findings
    for line in log_text.splitlines():
        stripped = line.strip()
        if re.search(r"\bERROR[: ]", stripped):
            findings.append(issue("error", "SAS log error", stripped[:500]))
        elif re.search(r"\bWARNING[: ]", stripped):
            findings.append(issue("warning", "SAS log warning", stripped[:500]))
        elif re.search(r"(?i)uninitialized|not found|invalid data|merge statement has more than one data set", stripped):
            findings.append(issue("warning", "SAS log risk", stripped[:500]))
    return findings[:200]


def run_sas_if_configured(program_text: str) -> dict[str, Any]:
    sas_executable = os.environ.get("SAS_EXECUTABLE")
    if not sas_executable:
        return {"ran": False, "message": "Set SAS_EXECUTABLE to enable server-side SAS runs."}
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    program_path = RUNS_DIR / f"generated_{stamp}.sas"
    log_path = RUNS_DIR / f"generated_{stamp}.log"
    program_path.write_text(program_text, encoding="utf-8")
    command = [sas_executable, "-sysin", str(program_path), "-log", str(log_path)]
    completed = subprocess.run(command, cwd=RUNS_DIR, capture_output=True, text=True, timeout=120)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else completed.stderr
    return {
        "ran": True,
        "returncode": completed.returncode,
        "program_path": str(program_path),
        "log_path": str(log_path),
        "log_text": log_text,
    }


def seed_samples() -> dict[str, Any]:
    prior_dir = SAMPLES_DIR / "prior"
    files = {
        "program_file": prior_dir / "t_14_1_1_demographics.sas",
        "output_file": prior_dir / "t_14_1_1_demographics.rtf",
        "shell_file": prior_dir / "t_14_1_1_demographics_shell.txt",
    }
    missing = [str(path) for path in files.values() if not path.exists()]
    if missing:
        return {"created": False, "message": "Sample files are missing.", "missing": missing}

    with connect() as conn:
        exists = conn.execute(
            "select id from examples where study_id = ? and tlf_number = ?",
            ("DEMO001", "14.1.1"),
        ).fetchone()
    if exists:
        return {"created": False, "message": "Sample knowledge-base example already exists.", "id": exists["id"]}

    payload = {
        "metadata": {
            "study_id": "DEMO001",
            "tlf_number": "14.1.1",
            "tlf_type": "table",
            "title": "Summary of Demographic Characteristics",
            "population": "Safety Population",
            "endpoint": "Demographics",
            "source_datasets": "ADSL",
            "macros": "none",
        }
    }
    for key, path in files.items():
        payload[key] = {
            "name": path.name,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    created = create_example(payload)
    return {"created": True, "message": "Sample knowledge-base example loaded.", "example": created}


def list_examples() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("select * from examples order by created_at desc, id desc").fetchall()
    return [public_example(row_to_dict(row)) for row in rows]


def list_runs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select id, shell_name, shell_json, retrieval_json, validation_json,
                   generated_program_path, generation_method, created_at
            from generation_runs
            order by id desc
            limit 50
            """
        ).fetchall()
    runs = []
    for row in rows:
        shell_json = safe_json(row["shell_json"], {})
        validation = safe_json(row["validation_json"], {})
        retrieval = safe_json(row["retrieval_json"], [])
        runs.append(
            {
                "id": row["id"],
                "shell_name": row["shell_name"],
                "title": shell_json.get("title", ""),
                "tlf_number": shell_json.get("tlf_number", ""),
                "status": validation.get("status", ""),
                "retrieved_count": len(retrieval),
                "program_path": row["generated_program_path"] or "",
                "generation_method": row["generation_method"] or "",
                "created_at": row["created_at"],
            }
        )
    return runs


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("select * from generation_runs where id = ?", (run_id,)).fetchone()
    if not row:
        return None
    data = row_to_dict(row)
    return {
        "id": data["id"],
        "shell_name": data["shell_name"],
        "shell_text": data["shell_text"],
        "shell": safe_json(data["shell_json"], {}),
        "program": data["generated_program"],
        "program_path": data.get("generated_program_path", ""),
        "generation_method": data.get("generation_method", ""),
        "retrieved": safe_json(data["retrieval_json"], []),
        "validation": safe_json(data["validation_json"], {}),
        "created_at": data["created_at"],
    }


def safe_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def safe_print(*args: Any, **kwargs: Any) -> None:
    try:
        print(*args, **kwargs)
    except (OSError, ValueError):
        pass


class AppHandler(BaseHTTPRequestHandler):
    server_version = f"SASTLFAssistant/{APP_VERSION}"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(STATIC_DIR / "index.html")
        elif parsed.path.startswith("/static/"):
            self.serve_file(STATIC_DIR / parsed.path.removeprefix("/static/"))
        elif parsed.path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "version": APP_VERSION,
                    "llm_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                    "openai_model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                    "local_env_loaded": LOCAL_ENV_PATH.exists(),
                }
            )
        elif parsed.path == "/api/examples":
            self.send_json({"examples": list_examples()})
        elif parsed.path == "/api/runs":
            self.send_json({"runs": list_runs()})
        elif parsed.path.startswith("/api/runs/"):
            run_id = int(parsed.path.rsplit("/", 1)[-1])
            run = get_run(run_id)
            if not run:
                self.send_error(HTTPStatus.NOT_FOUND, "Run not found")
            else:
                self.send_json(run)
        elif parsed.path.startswith("/api/scan-progress/"):
            scan_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            self.send_json(get_scan_progress(scan_id))
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/examples":
                self.send_json({"example": create_example(payload)}, status=HTTPStatus.CREATED)
            elif parsed.path == "/api/clear-knowledge-base":
                self.send_json(clear_knowledge_base())
            elif parsed.path == "/api/parse-shell":
                self.send_json(parse_shell_agent(payload), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/refine-clean-shell":
                self.send_json(refine_clean_shell(payload), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/start-output-scan":
                self.send_json(start_output_directory_scan(payload), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/scan-output-directory":
                self.send_json(scan_output_directory(payload), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/generate":
                self.send_json(generate_from_shell(payload), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/validate":
                program_text = payload.get("program", "")
                log_text = payload.get("log", "")
                shell_info = payload.get("shell") or {}
                self.send_json(validate_program(program_text, shell_info=shell_info, log_text=log_text))
            elif parsed.path == "/api/run-sas":
                result = run_sas_if_configured(payload.get("program", ""))
                if result.get("ran"):
                    result["validation"] = validate_program(payload.get("program", ""), log_text=result.get("log_text", ""))
                self.send_json(result)
            elif parsed.path == "/api/seed":
                self.send_json(seed_samples())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.send_json(
                {"error": type(exc).__name__, "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: Path) -> None:
        path = path.resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        safe_print(f"{self.address_string()} - {format % args}")


def main() -> None:
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AppHandler)
    safe_print(f"SAS TLF Assistant running at http://{host}:{port}")
    safe_print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
