from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional dependency
    BeautifulSoup = None

try:
    from markdownify import markdownify as html_to_markdown_lib
except ImportError:  # pragma: no cover - optional dependency
    html_to_markdown_lib = None

LOGGER = logging.getLogger("tester_issues")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ISSUE_ID_PATTERN = re.compile(r"^ISS-(\d{6})$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
ISSUE_TYPES = ("bug", "feature_request", "issue")
STATUSES = (
    "new",
    "needs_info",
    "ready_for_fix",
    "in_progress",
    "fixed_pending_verify",
    "verified_closed",
    "triaged",
    "fixed",
    "needs_input",
    "closed",
)
ALLOWED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".svg",
    ".txt",
    ".log",
    ".pdf",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".zip",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
}
MIME_TO_EXTENSION = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


@dataclass(frozen=True)
class Settings:
    git_repo_url: str = os.getenv("GIT_REPO_URL", "").strip()
    git_branch: str = os.getenv("GIT_BRANCH", "project-meituan").strip()
    issues_root: str = os.getenv("ISSUES_ROOT", "quick-deal/Issues").strip()
    local_repo_path: str = os.getenv("LOCAL_REPO_PATH", "./data/repo").strip()
    git_user_name: str = os.getenv("GIT_USER_NAME", "Tester Issues Bot").strip()
    git_user_email: str = os.getenv("GIT_USER_EMAIL", "tester-issues-bot@local").strip()
    max_file_mb: int = int(os.getenv("MAX_FILE_MB", "10"))
    max_files_per_submit: int = int(os.getenv("MAX_FILES_PER_SUBMIT", "10"))
    repo_lock_timeout_sec: int = int(os.getenv("REPO_LOCK_TIMEOUT_SEC", "30"))


SETTINGS = Settings()
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
REPO_LOCK = threading.Lock()


class CreateIssueJsonRequest(BaseModel):
    reported_by: str
    title: str
    issue_type: str
    description: str = ""
    description_html: str = ""
    problem_summary: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    expected_result: str = ""
    actual_result: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    environment: str = ""
    impact: str = ""
    suspected_component: str = ""
    related_paths: list[str] = Field(default_factory=list)
    related_commits: list[str] = Field(default_factory=list)
    related_issue_ids: list[str] = Field(default_factory=list)
    status: str = "new"
    notes: str = ""


class UpdateIssueJsonRequest(BaseModel):
    updated_by: str
    note: str = ""
    status: str = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        repo = ensure_repo_ready()
        LOGGER.info("Repository ready at %s", repo)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Repository initialization failed at startup: %s", exc)
    yield


app = FastAPI(title="Tester Issue Logger", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def ensure_non_empty(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return value


def normalize_text(value: str) -> str:
    return (value or "").strip()


def parse_multiline_list(value: str) -> list[str]:
    lines = [line.strip() for line in (value or "").splitlines()]
    return [line for line in lines if line]


def parse_csv_list(value: str) -> list[str]:
    parts = [part.strip() for part in (value or "").split(",")]
    return [part for part in parts if part]


def sanitize_filename(filename: str) -> str:
    raw = Path(filename or "").name
    cleaned = SAFE_FILENAME_PATTERN.sub("_", raw).strip("._")
    return cleaned[:128] or "file"


def repo_path() -> Path:
    return Path(SETTINGS.local_repo_path).resolve()


def repo_lock_dir_path() -> Path:
    return repo_path().parent / ".tester-issues.repo-lock"


@contextmanager
def repo_write_lock():
    deadline = time.monotonic() + SETTINGS.repo_lock_timeout_sec
    lock_dir = repo_lock_dir_path()
    lock_dir.parent.mkdir(parents=True, exist_ok=True)

    with REPO_LOCK:
        while True:
            try:
                lock_dir.mkdir(parents=False, exist_ok=False)
                (lock_dir / "owner").write_text(
                    f"pid={os.getpid()}\nacquired_at={now_utc_iso()}",
                    encoding="utf-8",
                )
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise HTTPException(
                        status_code=503,
                        detail="Issue repository is busy. Please retry.",
                    )
                time.sleep(0.1)

    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def issues_root_path(repo: Path) -> Path:
    return repo / Path(SETTINGS.issues_root)


def run_git_process(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def run_git(args: list[str], cwd: Path, check: bool = True) -> str:
    process = run_git_process(args=args, cwd=cwd)
    if check and process.returncode != 0:
        stderr = (process.stderr or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return (process.stdout or "").strip()


def set_git_identity(repo: Path) -> None:
    run_git(["config", "user.name", SETTINGS.git_user_name], cwd=repo)
    run_git(["config", "user.email", SETTINGS.git_user_email], cwd=repo)


def initialize_repo() -> Path:
    repo = repo_path()
    repo_parent = repo.parent
    repo_parent.mkdir(parents=True, exist_ok=True)

    if (repo / ".git").exists():
        set_git_identity(repo)
        return repo

    if SETTINGS.git_repo_url:
        if repo.exists() and any(repo.iterdir()):
            raise RuntimeError(
                f"Repository path {repo} exists and is not empty, cannot clone"
            )
        run_git(
            ["clone", "--branch", SETTINGS.git_branch, SETTINGS.git_repo_url, str(repo)],
            cwd=repo_parent,
        )
        set_git_identity(repo)
        return repo

    repo.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", SETTINGS.git_branch], cwd=repo)
    set_git_identity(repo)
    root = issues_root_path(repo)
    root.mkdir(parents=True, exist_ok=True)
    keep_file = root / ".gitkeep"
    keep_file.write_text("", encoding="utf-8")
    run_git(["add", str((root / ".gitkeep").relative_to(repo).as_posix())], cwd=repo)
    run_git(["commit", "-m", "chore: initialize issues repository"], cwd=repo)
    return repo


def sync_repo(repo: Path) -> None:
    if not SETTINGS.git_repo_url:
        return
    run_git(["fetch", "origin"], cwd=repo)
    run_git(["checkout", SETTINGS.git_branch], cwd=repo)
    run_git(["reset", "--hard", f"origin/{SETTINGS.git_branch}"], cwd=repo)


def ensure_repo_ready() -> Path:
    repo = initialize_repo()
    root = issues_root_path(repo)
    root.mkdir(parents=True, exist_ok=True)
    return repo


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected YAML structure in {path}")
        return data


def write_yaml(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(content, handle, sort_keys=False, allow_unicode=True)


def list_issue_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for item in root.iterdir():
        if item.is_dir() and ISSUE_ID_PATTERN.match(item.name):
            result.append(item)
    return sorted(result, key=lambda p: p.name)


def next_issue_id(root: Path) -> str:
    current = 0
    for issue_dir in list_issue_dirs(root):
        match = ISSUE_ID_PATTERN.match(issue_dir.name)
        if not match:
            continue
        current = max(current, int(match.group(1)))
    return f"ISS-{current + 1:06d}"


def list_issues(repo: Path) -> list[dict[str, Any]]:
    root = issues_root_path(repo)
    issues: list[dict[str, Any]] = []
    for issue_dir in list_issue_dirs(root):
        issue_file = issue_dir / "issue.yaml"
        if not issue_file.exists():
            continue
        issue = read_yaml(issue_file)
        issue.setdefault("id", issue_dir.name)
        issues.append(issue)
    issues.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return issues


def load_issue(repo: Path, issue_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not ISSUE_ID_PATTERN.match(issue_id):
        raise HTTPException(status_code=404, detail="Issue not found")
    issue_dir = issues_root_path(repo) / issue_id
    issue_file = issue_dir / "issue.yaml"
    if not issue_file.exists():
        raise HTTPException(status_code=404, detail="Issue not found")

    issue = read_yaml(issue_file)
    updates_dir = issue_dir / "updates"
    updates: list[dict[str, Any]] = []
    if updates_dir.exists():
        for update_file in sorted(updates_dir.glob("*.yaml")):
            update = read_yaml(update_file)
            updates.append(update)
    return issue, updates


def persist_issue_index(repo: Path) -> Path:
    issues = list_issues(repo)
    index = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("type"),
            "status": item.get("status"),
            "updated_at": item.get("updated_at"),
            "reported_by": item.get("reported_by"),
        }
        for item in issues
    ]
    index_path = issues_root_path(repo) / "issue-index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index_path


def issue_matches_query(issue: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    fields = (
        "id",
        "title",
        "type",
        "status",
        "reported_by",
        "description",
        "description_markdown",
        "updated_at",
        "created_at",
    )
    haystack = " ".join(str(issue.get(field, "")) for field in fields).lower()
    return query in haystack


def validate_files(files: list[UploadFile]) -> None:
    real_files = [f for f in files if f and f.filename]
    if len(real_files) > SETTINGS.max_files_per_submit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Max allowed is {SETTINGS.max_files_per_submit}",
        )


def count_upload_files(files: list[UploadFile]) -> int:
    return len([f for f in files if f and f.filename])


def count_embedded_data_images(description_html: str) -> int:
    html_input = (description_html or "").strip()
    if not html_input:
        return 0
    if BeautifulSoup:
        soup = BeautifulSoup(html_input, "html.parser")
        return sum(
            1
            for img_tag in soup.find_all("img")
            if (img_tag.get("src") or "").strip().startswith("data:image/")
        )
    return len(
        re.findall(
            r'(?is)<img\b[^>]*src=["\']data:image/[^"\']+["\'][^>]*>',
            html_input,
        )
    )


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def attachment_manifest_path(issue_dir: Path) -> Path:
    return issue_dir / "attachments" / "manifest.json"


def load_attachment_manifest(issue_dir: Path) -> list[dict[str, Any]]:
    manifest = read_json(attachment_manifest_path(issue_dir), [])
    if isinstance(manifest, list):
        return manifest
    return []


def append_attachment_manifest(issue_dir: Path, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    manifest = load_attachment_manifest(issue_dir)
    manifest.extend(entries)
    write_json(attachment_manifest_path(issue_dir), manifest)


def save_attachments(
    files: list[UploadFile],
    issue_dir: Path,
    timestamp: str,
) -> list[dict[str, Any]]:
    validate_files(files)
    saved: list[dict[str, Any]] = []
    attachment_dir = issue_dir / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = SETTINGS.max_file_mb * 1024 * 1024

    for upload in files:
        if not upload or not upload.filename:
            continue
        ext = Path(upload.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type for {upload.filename}",
            )

        safe_name = sanitize_filename(upload.filename)
        final_name = f"{timestamp}_{safe_name}"
        dest = attachment_dir / final_name

        total = 0
        digest = hashlib.sha256()
        with dest.open("wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=400,
                        detail=f"{upload.filename} exceeds {SETTINGS.max_file_mb} MB",
                    )
                digest.update(chunk)
                handle.write(chunk)
        relative_path = f"attachments/{final_name}"
        mime_type = upload.content_type or mimetypes.guess_type(final_name)[0] or "application/octet-stream"
        saved.append(
            {
                "path": relative_path,
                "filename": final_name,
                "mime_type": mime_type,
                "size_bytes": total,
                "sha256": digest.hexdigest(),
                "source": "uploaded",
                "role": "attachment",
            }
        )

    return saved


def simple_html_to_markdown(html_value: str) -> str:
    text = html_value or ""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<\s*h1[^>]*>(.*?)<\s*/\s*h1\s*>", r"# \1\n\n", text)
    text = re.sub(r"(?is)<\s*h2[^>]*>(.*?)<\s*/\s*h2\s*>", r"## \1\n\n", text)
    text = re.sub(r"(?is)<\s*h3[^>]*>(.*?)<\s*/\s*h3\s*>", r"### \1\n\n", text)
    text = re.sub(r"(?is)<\s*strong[^>]*>(.*?)<\s*/\s*strong\s*>", r"**\1**", text)
    text = re.sub(r"(?is)<\s*b[^>]*>(.*?)<\s*/\s*b\s*>", r"**\1**", text)
    text = re.sub(r"(?is)<\s*em[^>]*>(.*?)<\s*/\s*em\s*>", r"*\1*", text)
    text = re.sub(r"(?is)<\s*i[^>]*>(.*?)<\s*/\s*i\s*>", r"*\1*", text)
    text = re.sub(r"(?is)<\s*li[^>]*>(.*?)<\s*/\s*li\s*>", r"- \1\n", text)
    text = re.sub(r"(?is)<\s*/\s*(ul|ol)\s*>", "\n", text)

    def img_replacer(match: re.Match[str]) -> str:
        src_match = re.search(r'src=["\']([^"\']+)["\']', match.group(0), flags=re.IGNORECASE)
        alt_match = re.search(r'alt=["\']([^"\']*)["\']', match.group(0), flags=re.IGNORECASE)
        src = src_match.group(1) if src_match else ""
        alt = alt_match.group(1) if alt_match else "image"
        return f"![{alt}]({src})"

    text = re.sub(r"(?is)<\s*img[^>]*>", img_replacer, text)
    text = re.sub(r"(?is)<\s*/\s*p\s*>", "\n\n", text)
    text = re.sub(r"(?is)<\s*p[^>]*>", "", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown_text(html_value: str) -> str:
    if html_to_markdown_lib:
        return html_to_markdown_lib(html_value, heading_style="ATX").strip()
    return simple_html_to_markdown(html_value)


def extract_text_from_html(html_value: str) -> str:
    return " ".join(re.sub(r"(?is)<[^>]+>", " ", html_value or "").split())


def has_img_tag(html_value: str) -> bool:
    return bool(re.search(r"(?is)<\s*img\b", html_value or ""))


def extract_embedded_images_and_markdown(
    description_html: str,
    issue_dir: Path,
    timestamp: str,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Extract base64-embedded images from HTML and return rewritten markdown/text."""
    html_input = (description_html or "").strip()
    if not html_input:
        return "", "", "", []

    attachment_dir = issue_dir / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)

    embedded_entries: list[dict[str, Any]] = []
    max_bytes = SETTINGS.max_file_mb * 1024 * 1024
    image_counter = 1

    if BeautifulSoup:
        soup = BeautifulSoup(html_input, "html.parser")
        for img_tag in soup.find_all("img"):
            src = (img_tag.get("src") or "").strip()
            if not src.startswith("data:image/"):
                continue
            if "," not in src:
                continue
            header, payload = src.split(",", 1)
            mime = header.split(";")[0].replace("data:", "").strip().lower()
            extension = MIME_TO_EXTENSION.get(mime, ".png")
            filename = f"{timestamp}_embedded_{image_counter}{extension}"
            destination = attachment_dir / filename

            try:
                decoded = base64.b64decode(payload, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid embedded image payload: {exc}",
                ) from exc

            if len(decoded) > max_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"Embedded image exceeds {SETTINGS.max_file_mb} MB",
                )
            destination.write_bytes(decoded)
            rel_path = f"attachments/{filename}"
            img_tag["src"] = rel_path
            embedded_entries.append(
                {
                    "path": rel_path,
                    "filename": filename,
                    "mime_type": mime,
                    "size_bytes": len(decoded),
                    "sha256": hashlib.sha256(decoded).hexdigest(),
                    "source": "embedded",
                    "role": "description_image",
                }
            )
            image_counter += 1

        rewritten_html = str(soup)
    else:
        img_tag_pattern = re.compile(r"(?is)<img\b[^>]*>")
        data_src_pattern = re.compile(r'src=["\'](data:image/[^"\']+)["\']', flags=re.IGNORECASE)

        def replace_img_tag(match: re.Match[str]) -> str:
            nonlocal image_counter
            tag = match.group(0)
            src_match = data_src_pattern.search(tag)
            if not src_match:
                return tag

            src = src_match.group(1)
            if "," not in src:
                return tag
            header, payload = src.split(",", 1)
            mime = header.split(";")[0].replace("data:", "").strip().lower()
            extension = MIME_TO_EXTENSION.get(mime, ".png")
            filename = f"{timestamp}_embedded_{image_counter}{extension}"
            destination = attachment_dir / filename

            try:
                decoded = base64.b64decode(payload, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid embedded image payload: {exc}",
                ) from exc

            if len(decoded) > max_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"Embedded image exceeds {SETTINGS.max_file_mb} MB",
                )
            destination.write_bytes(decoded)
            rel_path = f"attachments/{filename}"
            embedded_entries.append(
                {
                    "path": rel_path,
                    "filename": filename,
                    "mime_type": mime,
                    "size_bytes": len(decoded),
                    "sha256": hashlib.sha256(decoded).hexdigest(),
                    "source": "embedded",
                    "role": "description_image",
                }
            )
            image_counter += 1
            return data_src_pattern.sub(f'src="{rel_path}"', tag, count=1)

        rewritten_html = img_tag_pattern.sub(replace_img_tag, html_input)

    markdown = html_to_markdown_text(rewritten_html)
    text_only = extract_text_from_html(rewritten_html)
    return rewritten_html, markdown, text_only, embedded_entries


def build_issue_main_markdown(issue_main: dict[str, Any]) -> str:
    metadata = issue_main.get("metadata", {})
    report = issue_main.get("report", {})
    links = issue_main.get("links", {})
    attachments = issue_main.get("attachments", [])
    repro_steps = report.get("repro_steps", [])
    acceptance_criteria = report.get("acceptance_criteria", [])

    lines = [
        f"# {issue_main.get('issue_id', 'UNKNOWN')} - {metadata.get('title', '')}",
        "",
        "## Metadata",
        f"- Reporter Name: {metadata.get('reported_by', '')}",
        f"- Issue Type: {metadata.get('issue_type', '')}",
        f"- Status: {metadata.get('status', '')}",
        f"- Created At (UTC): {metadata.get('created_at', '')}",
        f"- Updated At (UTC): {metadata.get('updated_at', '')}",
        "",
        "## Problem Summary",
        "",
        report.get("problem_summary", "_No summary provided._"),
        "",
        "## Description",
        "",
    ]
    description_markdown = report.get("description_markdown", "")
    if description_markdown.strip():
        lines.append(description_markdown)
    else:
        lines.append("_No description provided._")

    lines.extend(["", "## Reproduction Steps", ""])
    if repro_steps:
        for idx, step in enumerate(repro_steps, start=1):
            lines.append(f"{idx}. {step}")
    else:
        lines.append("_No reproduction steps provided._")

    lines.extend(
        [
            "",
            "## Expected Result",
            "",
            report.get("expected_result", "_Not provided._"),
            "",
            "## Actual Result",
            "",
            report.get("actual_result", "_Not provided._"),
            "",
            "## Impact",
            "",
            report.get("impact", "_Not provided._"),
            "",
            "## Environment",
            "",
            report.get("environment", "_Not provided._"),
            "",
            "## Suspected Component",
            "",
            report.get("suspected_component", "_Not provided._"),
            "",
            "## Acceptance Criteria",
            "",
        ]
    )
    if acceptance_criteria:
        for idx, item in enumerate(acceptance_criteria, start=1):
            lines.append(f"{idx}. {item}")
    else:
        lines.append("_No acceptance criteria provided._")

    lines.extend(["", "## Initial Notes", ""])
    notes = issue_main.get("initial_notes", "")
    if notes:
        lines.append(notes)
    else:
        lines.append("_No notes provided._")

    lines.extend(["", "## Attachments", ""])
    if attachments:
        for entry in attachments:
            path_value = entry.get("path", "")
            name = entry.get("filename", Path(path_value).name)
            source = entry.get("source", "uploaded")
            lines.append(f"- [{name}]({path_value}) ({source})")
    else:
        lines.append("_No attachments._")

    lines.extend(["", "## Links", ""])
    if links.get("related_paths"):
        lines.append(f"- Related Paths: {', '.join(links['related_paths'])}")
    if links.get("related_commits"):
        lines.append(f"- Related Commits: {', '.join(links['related_commits'])}")
    if links.get("related_issue_ids"):
        lines.append(f"- Related Issues: {', '.join(links['related_issue_ids'])}")
    if not any(links.values()):
        lines.append("_No related links._")

    return "\n".join(lines).strip() + "\n"


def build_update_markdown(update_event: dict[str, Any]) -> str:
    attachment_entries = update_event.get("attachments", [])
    lines = [
        f"# Update {update_event.get('timestamp', '')}",
        "",
        f"- Issue: {update_event.get('issue_id', '')}",
        f"- Actor: {update_event.get('actor', '')}",
        f"- Status: {update_event.get('status_from', '')} -> {update_event.get('status_to', '')}",
        "",
        "## Note",
        "",
        update_event.get("note", "_No note provided._") or "_No note provided._",
        "",
        "## Attachments",
        "",
    ]
    if attachment_entries:
        for entry in attachment_entries:
            lines.append(f"- [{entry.get('filename', '')}]({entry.get('path', '')})")
    else:
        lines.append("_No attachments added in this update._")
    return "\n".join(lines).strip() + "\n"


def build_ai_brief_markdown(
    issue_main: dict[str, Any],
    updates: list[dict[str, Any]],
) -> str:
    metadata = issue_main.get("metadata", {})
    report = issue_main.get("report", {})
    latest = updates[-1] if updates else None
    lines = [
        f"# AI Brief - {issue_main.get('issue_id', '')}",
        "",
        f"- Title: {metadata.get('title', '')}",
        f"- Status: {metadata.get('status', '')}",
        f"- Type: {metadata.get('issue_type', '')}",
        f"- Updated At: {metadata.get('updated_at', '')}",
        "",
        "## Problem",
        "",
        report.get("problem_summary", "_Not provided._"),
        "",
        "## Repro Steps",
        "",
    ]
    repro_steps = report.get("repro_steps", [])
    if repro_steps:
        for idx, step in enumerate(repro_steps, start=1):
            lines.append(f"{idx}. {step}")
    else:
        lines.append("_No repro steps provided._")

    lines.extend(
        [
            "",
            "## Expected vs Actual",
            "",
            f"- Expected: {report.get('expected_result', '_Not provided._')}",
            f"- Actual: {report.get('actual_result', '_Not provided._')}",
            "",
            "## Acceptance Criteria",
            "",
        ]
    )
    criteria = report.get("acceptance_criteria", [])
    if criteria:
        for idx, item in enumerate(criteria, start=1):
            lines.append(f"{idx}. {item}")
    else:
        lines.append("_No acceptance criteria provided._")

    lines.extend(["", "## Latest Update", ""])
    if latest:
        lines.append(f"- Actor: {latest.get('actor', '')}")
        lines.append(f"- Time: {latest.get('timestamp', '')}")
        lines.append(f"- Status: {latest.get('status_from', '')} -> {latest.get('status_to', '')}")
        lines.append(f"- Note: {latest.get('note', '') or '_No note provided._'}")
    else:
        lines.append("_No updates yet._")

    return "\n".join(lines).strip() + "\n"


def update_events_dir(issue_dir: Path) -> Path:
    return issue_dir / "updates"


def read_update_events(issue_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for json_file in sorted(update_events_dir(issue_dir).glob("*.json")):
        payload = read_json(json_file, {})
        if isinstance(payload, dict):
            events.append(payload)
    return events


def write_update_artifacts(issue_dir: Path, timestamp: str, event: dict[str, Any]) -> None:
    updates_dir = update_events_dir(issue_dir)
    updates_dir.mkdir(parents=True, exist_ok=True)
    write_json(updates_dir / f"{timestamp}.json", event)
    (updates_dir / f"{timestamp}.md").write_text(
        build_update_markdown(event),
        encoding="utf-8",
    )


def enforce_ready_for_fix_requirements(
    *,
    status: str,
    repro_steps: list[str],
    expected_result: str,
    actual_result: str,
    acceptance_criteria: list[str],
) -> None:
    if status != "ready_for_fix":
        return
    missing: list[str] = []
    if not repro_steps:
        missing.append("repro_steps")
    if not expected_result.strip():
        missing.append("expected_result")
    if not actual_result.strip():
        missing.append("actual_result")
    if not acceptance_criteria:
        missing.append("acceptance_criteria")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"ready_for_fix requires: {', '.join(missing)}",
        )


def summarize_for_commit(title: str, max_len: int = 52) -> str:
    compact = " ".join(title.split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3].rstrip() + "..."


def commit_and_push(repo: Path, rel_paths: list[Path], message: str) -> tuple[str, str]:
    add_args = ["add", *[path.as_posix() for path in rel_paths]]
    run_git(add_args, cwd=repo)
    status = run_git(["status", "--porcelain"], cwd=repo)
    if not status:
        sha = run_git(["rev-parse", "HEAD"], cwd=repo)
        return sha, message

    run_git(["commit", "-m", message], cwd=repo)
    if SETTINGS.git_repo_url:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            push = run_git_process(["push", "origin", SETTINGS.git_branch], cwd=repo)
            if push.returncode == 0:
                break

            stderr = (push.stderr or "").lower()
            is_retryable = any(
                token in stderr
                for token in (
                    "non-fast-forward",
                    "fetch first",
                    "rejected",
                )
            )
            if attempt < max_attempts and is_retryable:
                run_git(["fetch", "origin"], cwd=repo)
                run_git(["rebase", f"origin/{SETTINGS.git_branch}"], cwd=repo)
                continue

            err_text = (push.stderr or "").strip()
            raise RuntimeError(f"git push failed: {err_text}")
    sha = run_git(["rev-parse", "HEAD"], cwd=repo)
    return sha, message


def create_issue_record(
    *,
    reporter: str,
    title: str,
    issue_type: str,
    description: str,
    description_html: str,
    problem_summary: str,
    repro_steps_text: str,
    expected_result: str,
    actual_result: str,
    acceptance_criteria_text: str,
    environment: str,
    impact: str,
    suspected_component: str,
    related_paths_text: str,
    related_commits_text: str,
    related_issue_ids_text: str,
    status: str,
    notes: str,
    files: list[UploadFile],
) -> dict[str, str]:
    reporter = ensure_non_empty(reporter, "reported_by")
    title = ensure_non_empty(title, "title")
    issue_type = ensure_non_empty(issue_type, "type")

    if issue_type not in ISSUE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid issue type")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail="Invalid issue status")
    if not status:
        status = "new"
    description = normalize_text(description)
    description_html = normalize_text(description_html)
    problem_summary = normalize_text(problem_summary) or title
    expected_result = normalize_text(expected_result)
    actual_result = normalize_text(actual_result)
    environment = normalize_text(environment)
    impact = normalize_text(impact)
    suspected_component = normalize_text(suspected_component)
    notes = normalize_text(notes)
    repro_steps = parse_multiline_list(repro_steps_text)
    acceptance_criteria = parse_multiline_list(acceptance_criteria_text)
    related_paths = parse_csv_list(related_paths_text)
    related_commits = parse_csv_list(related_commits_text)
    related_issue_ids = parse_csv_list(related_issue_ids_text)
    if not description_html and not description:
        raise HTTPException(status_code=400, detail="description is required")
    enforce_ready_for_fix_requirements(
        status=status,
        repro_steps=repro_steps,
        expected_result=expected_result,
        actual_result=actual_result,
        acceptance_criteria=acceptance_criteria,
    )

    uploaded_file_count = count_upload_files(files)
    embedded_image_count = count_embedded_data_images(description_html)
    total_file_count = uploaded_file_count + embedded_image_count
    if total_file_count > SETTINGS.max_files_per_submit:
        raise HTTPException(
            status_code=400,
            detail=(
                "Too many files. "
                f"Max allowed is {SETTINGS.max_files_per_submit} "
                "(uploaded + embedded images)."
            ),
        )

    with repo_write_lock():
        repo = ensure_repo_ready()
        sync_repo(repo)
        root = issues_root_path(repo)
        issue_id = next_issue_id(root)
        issue_dir = root / issue_id
        issue_dir.mkdir(parents=True, exist_ok=True)

        created_at = now_utc_iso()
        file_stamp = now_utc_file_stamp()
        uploaded_attachments = save_attachments(files, issue_dir, file_stamp)

        if description_html:
            normalized_html, description_markdown, description_text, embedded_images = (
                extract_embedded_images_and_markdown(
                    description_html=description_html,
                    issue_dir=issue_dir,
                    timestamp=file_stamp,
                )
            )
            if not description_text and not has_img_tag(normalized_html):
                if description:
                    escaped = html.escape(description)
                    normalized_html = f"<p>{escaped}</p>"
                    description_markdown = description
                    description_text = " ".join(description.split())
                else:
                    raise HTTPException(status_code=400, detail="description is required")
        else:
            escaped = html.escape(description)
            normalized_html = f"<p>{escaped}</p>"
            description_markdown = description
            description_text = " ".join(description.split())
            embedded_images: list[dict[str, Any]] = []

        all_attachments = [*uploaded_attachments, *embedded_images]
        if all_attachments:
            append_attachment_manifest(issue_dir, all_attachments)
        else:
            write_json(attachment_manifest_path(issue_dir), [])
        manifest = load_attachment_manifest(issue_dir)

        issue_main_json = {
            "schema_version": "1.0",
            "issue_id": issue_id,
            "metadata": {
                "title": title,
                "issue_type": issue_type,
                "status": status,
                "reported_by": reporter,
                "created_at": created_at,
                "updated_at": created_at,
            },
            "report": {
                "problem_summary": problem_summary,
                "description_markdown": description_markdown,
                "description_html": normalized_html,
                "description_text": description_text,
                "repro_steps": repro_steps,
                "expected_result": expected_result,
                "actual_result": actual_result,
                "environment": environment,
                "impact": impact,
                "suspected_component": suspected_component,
                "acceptance_criteria": acceptance_criteria,
            },
            "links": {
                "related_paths": related_paths,
                "related_commits": related_commits,
                "related_issue_ids": related_issue_ids,
            },
            "initial_notes": notes,
            "attachments_manifest_file": "attachments/manifest.json",
            "attachments": manifest,
            "artifacts": {
                "issue_main_md": "issue_main.md",
                "issue_main_json": "issue_main.json",
                "ai_brief_md": "ai_brief.md",
            },
        }
        write_json(issue_dir / "issue_main.json", issue_main_json)
        issue_main_md = build_issue_main_markdown(issue_main_json)
        (issue_dir / "issue_main.md").write_text(issue_main_md, encoding="utf-8")

        initial_event = {
            "schema_version": "1.0",
            "event_type": "create",
            "issue_id": issue_id,
            "timestamp": created_at,
            "actor": reporter,
            "status_from": status,
            "status_to": status,
            "note": notes,
            "attachments": all_attachments,
        }
        write_update_artifacts(
            issue_dir=issue_dir,
            timestamp=file_stamp,
            event=initial_event,
        )
        (issue_dir / "ai_brief.md").write_text(
            build_ai_brief_markdown(issue_main_json, [initial_event]),
            encoding="utf-8",
        )
        attachment_paths = [entry["path"] for entry in all_attachments]

        issue_doc = {
            "id": issue_id,
            "title": title,
            "type": issue_type,
            "status": status,
            "reported_by": reporter,
            "created_at": created_at,
            "updated_at": created_at,
            "description": description_text,
            "description_markdown": description_markdown,
            "description_html": normalized_html,
            "issue_main_file": "issue_main.md",
            "issue_main_json_file": "issue_main.json",
            "ai_brief_file": "ai_brief.md",
        }
        write_yaml(issue_dir / "issue.yaml", issue_doc)

        update_doc = {
            "issue_id": issue_id,
            "updated_at": created_at,
            "updated_by": reporter,
            "note": notes,
            "status_from": status,
            "status_to": status,
            "attachments": attachment_paths,
        }
        write_yaml(issue_dir / "updates" / f"{file_stamp}.yaml", update_doc)

        index_path = persist_issue_index(repo)
        commit_msg = f"issue({issue_id}): new {issue_type} - {summarize_for_commit(title)}"
        sha, message = commit_and_push(
            repo,
            [
                issue_dir.relative_to(repo),
                index_path.relative_to(repo),
            ],
            commit_msg,
        )

    return {
        "issue_id": issue_id,
        "commit_sha": sha,
        "commit_message": message,
        "issue_url": f"/issues/{issue_id}",
    }


def update_issue_record(
    *,
    issue_id: str,
    updater: str,
    note: str,
    status: str,
    files: list[UploadFile],
) -> dict[str, str]:
    updater = ensure_non_empty(updater, "updated_by")
    note = note.strip()
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")

    with repo_write_lock():
        repo = ensure_repo_ready()
        sync_repo(repo)
        issue, _ = load_issue(repo, issue_id)
        issue_dir = issues_root_path(repo) / issue_id
        old_status = issue.get("status", "new")
        new_status = status or old_status

        issue_main_path = issue_dir / "issue_main.json"
        issue_main = read_json(issue_main_path, {})
        if not isinstance(issue_main, dict) or not issue_main:
            issue_main = {
                "schema_version": "1.0",
                "issue_id": issue_id,
                "metadata": {
                    "title": issue.get("title", ""),
                    "issue_type": issue.get("type", ""),
                    "status": issue.get("status", new_status),
                    "reported_by": issue.get("reported_by", ""),
                    "created_at": issue.get("created_at", ""),
                    "updated_at": issue.get("updated_at", ""),
                },
                "report": {
                    "problem_summary": issue.get("title", ""),
                    "description_markdown": issue.get("description_markdown", ""),
                    "description_html": issue.get("description_html", ""),
                    "description_text": issue.get("description", ""),
                    "repro_steps": [],
                    "expected_result": "",
                    "actual_result": "",
                    "environment": "",
                    "impact": "",
                    "suspected_component": "",
                    "acceptance_criteria": [],
                },
                "links": {
                    "related_paths": [],
                    "related_commits": [],
                    "related_issue_ids": [],
                },
                "initial_notes": "",
                "attachments_manifest_file": "attachments/manifest.json",
                "attachments": [],
                "artifacts": {
                    "issue_main_md": "issue_main.md",
                    "issue_main_json": "issue_main.json",
                    "ai_brief_md": "ai_brief.md",
                },
            }
        report = issue_main.get("report", {})
        enforce_ready_for_fix_requirements(
            status=new_status,
            repro_steps=report.get("repro_steps", []),
            expected_result=report.get("expected_result", ""),
            actual_result=report.get("actual_result", ""),
            acceptance_criteria=report.get("acceptance_criteria", []),
        )

        file_stamp = now_utc_file_stamp()
        attachments = save_attachments(files, issue_dir, file_stamp)
        if not note and not attachments and new_status == old_status:
            raise HTTPException(
                status_code=400,
                detail="No changes detected. Add a note, attachment, or status change.",
            )
        append_attachment_manifest(issue_dir, attachments)

        changed_at = now_utc_iso()
        attachment_paths = [entry["path"] for entry in attachments]
        update_doc = {
            "issue_id": issue_id,
            "updated_at": changed_at,
            "updated_by": updater,
            "note": note,
            "status_from": old_status,
            "status_to": new_status,
            "attachments": attachment_paths,
        }
        write_yaml(issue_dir / "updates" / f"{file_stamp}.yaml", update_doc)

        update_event = {
            "schema_version": "1.0",
            "event_type": "update",
            "issue_id": issue_id,
            "timestamp": changed_at,
            "actor": updater,
            "status_from": old_status,
            "status_to": new_status,
            "note": note,
            "attachments": attachments,
        }
        write_update_artifacts(issue_dir=issue_dir, timestamp=file_stamp, event=update_event)

        issue["status"] = new_status
        issue["updated_at"] = changed_at
        write_yaml(issue_dir / "issue.yaml", issue)

        metadata = issue_main.setdefault("metadata", {})
        metadata["status"] = new_status
        metadata["updated_at"] = changed_at
        issue_main["attachments"] = load_attachment_manifest(issue_dir)
        write_json(issue_main_path, issue_main)
        (issue_dir / "issue_main.md").write_text(
            build_issue_main_markdown(issue_main),
            encoding="utf-8",
        )
        updates_for_brief = read_update_events(issue_dir)
        (issue_dir / "ai_brief.md").write_text(
            build_ai_brief_markdown(issue_main, updates_for_brief),
            encoding="utf-8",
        )
        index_path = persist_issue_index(repo)

        if new_status != old_status:
            commit_msg = f"issue({issue_id}): status {old_status} -> {new_status}"
        elif note and not attachments:
            commit_msg = f"issue({issue_id}): add tester note"
        elif attachments and not note:
            commit_msg = f"issue({issue_id}): add attachments"
        else:
            commit_msg = f"issue({issue_id}): add update"

        sha, message = commit_and_push(
            repo,
            [
                issue_dir.relative_to(repo),
                index_path.relative_to(repo),
            ],
            commit_msg,
        )

    return {
        "issue_id": issue_id,
        "commit_sha": sha,
        "commit_message": message,
        "issue_url": f"/issues/{issue_id}",
    }


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/issues", status_code=302)


@app.get("/issues", response_class=HTMLResponse, include_in_schema=False)
def issues_page(
    request: Request,
    status: str = Query(default=""),
    issue_type: str = Query(default="", alias="type"),
    q: str = Query(default=""),
) -> HTMLResponse:
    repo = ensure_repo_ready()
    issues = list_issues(repo)
    query = q.strip().lower()

    filtered = []
    for issue in issues:
        if status and issue.get("status") != status:
            continue
        if issue_type and issue.get("type") != issue_type:
            continue
        if not issue_matches_query(issue, query):
            continue
        filtered.append(issue)

    return TEMPLATES.TemplateResponse(
        request=request,
        name="issues_list.html",
        context={
            "issues": filtered,
            "statuses": STATUSES,
            "issue_types": ISSUE_TYPES,
            "filters": {"status": status, "type": issue_type, "q": q},
        },
    )


@app.get("/issues/new", response_class=HTMLResponse, include_in_schema=False)
def new_issue_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request,
        name="issue_new.html",
        context={
            "statuses": STATUSES,
            "issue_types": ISSUE_TYPES,
            "result": None,
            "error": None,
        },
    )


@app.post("/issues/new", response_class=HTMLResponse, include_in_schema=False)
def new_issue_submit(
    request: Request,
    reported_by: str = Form(...),
    title: str = Form(...),
    issue_type: str = Form(...),
    description: str = Form(default=""),
    description_html: str = Form(default=""),
    problem_summary: str = Form(default=""),
    repro_steps: str = Form(default=""),
    expected_result: str = Form(default=""),
    actual_result: str = Form(default=""),
    acceptance_criteria: str = Form(default=""),
    environment: str = Form(default=""),
    impact: str = Form(default=""),
    suspected_component: str = Form(default=""),
    related_paths: str = Form(default=""),
    related_commits: str = Form(default=""),
    related_issue_ids: str = Form(default=""),
    status: str = Form(default="new"),
    notes: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> HTMLResponse:
    try:
        result = create_issue_record(
            reporter=reported_by,
            title=title,
            issue_type=issue_type,
            description=description,
            description_html=description_html,
            problem_summary=problem_summary,
            repro_steps_text=repro_steps,
            expected_result=expected_result,
            actual_result=actual_result,
            acceptance_criteria_text=acceptance_criteria,
            environment=environment,
            impact=impact,
            suspected_component=suspected_component,
            related_paths_text=related_paths,
            related_commits_text=related_commits,
            related_issue_ids_text=related_issue_ids,
            status=status,
            notes=notes,
            files=files,
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_new.html",
            context={
                "statuses": STATUSES,
                "issue_types": ISSUE_TYPES,
                "result": result,
                "error": None,
            },
        )
    except HTTPException as exc:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_new.html",
            context={
                "statuses": STATUSES,
                "issue_types": ISSUE_TYPES,
                "result": None,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("New issue submit failed")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_new.html",
            context={
                "statuses": STATUSES,
                "issue_types": ISSUE_TYPES,
                "result": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.get("/issues/{issue_id}", response_class=HTMLResponse, include_in_schema=False)
def issue_detail_page(request: Request, issue_id: str) -> HTMLResponse:
    repo = ensure_repo_ready()
    issue, updates = load_issue(repo, issue_id)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="issue_detail.html",
        context={
            "issue": issue,
            "updates": updates,
            "statuses": STATUSES,
            "issue_main_url": f"/raw/{issue_id}/issue_main.md",
            "issue_main_json_url": f"/raw/{issue_id}/issue_main.json",
            "ai_brief_url": f"/raw/{issue_id}/ai_brief.md",
            "result": None,
            "error": None,
        },
    )


@app.post("/issues/{issue_id}/updates", response_class=HTMLResponse, include_in_schema=False)
def issue_update_submit(
    request: Request,
    issue_id: str,
    updated_by: str = Form(...),
    note: str = Form(default=""),
    status: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> HTMLResponse:
    repo = ensure_repo_ready()
    try:
        result = update_issue_record(
            issue_id=issue_id,
            updater=updated_by,
            note=note,
            status=status,
            files=files,
        )
        issue, updates = load_issue(repo, issue_id)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_detail.html",
            context={
                "issue": issue,
                "updates": updates,
                "statuses": STATUSES,
                "issue_main_url": f"/raw/{issue_id}/issue_main.md",
                "issue_main_json_url": f"/raw/{issue_id}/issue_main.json",
                "ai_brief_url": f"/raw/{issue_id}/ai_brief.md",
                "result": result,
                "error": None,
            },
        )
    except HTTPException as exc:
        issue, updates = load_issue(repo, issue_id)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_detail.html",
            context={
                "issue": issue,
                "updates": updates,
                "statuses": STATUSES,
                "issue_main_url": f"/raw/{issue_id}/issue_main.md",
                "issue_main_json_url": f"/raw/{issue_id}/issue_main.json",
                "ai_brief_url": f"/raw/{issue_id}/ai_brief.md",
                "result": None,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Issue update failed")
        issue, updates = load_issue(repo, issue_id)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="issue_detail.html",
            context={
                "issue": issue,
                "updates": updates,
                "statuses": STATUSES,
                "issue_main_url": f"/raw/{issue_id}/issue_main.md",
                "issue_main_json_url": f"/raw/{issue_id}/issue_main.json",
                "ai_brief_url": f"/raw/{issue_id}/ai_brief.md",
                "result": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.get("/raw/{issue_id}/{attachment_path:path}", include_in_schema=False)
def raw_attachment(issue_id: str, attachment_path: str):
    from fastapi.responses import FileResponse

    if not ISSUE_ID_PATTERN.match(issue_id):
        raise HTTPException(status_code=404, detail="Issue not found")
    repo = ensure_repo_ready()
    issue_dir = issues_root_path(repo) / issue_id
    issue_root = issue_dir.resolve()
    candidate = (issue_dir / attachment_path).resolve()
    try:
        candidate.relative_to(issue_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(path=candidate)


@app.post("/api/issues")
def api_create_issue(
    reported_by: str = Form(...),
    title: str = Form(...),
    issue_type: str = Form(...),
    description: str = Form(default=""),
    description_html: str = Form(default=""),
    problem_summary: str = Form(default=""),
    repro_steps: str = Form(default=""),
    expected_result: str = Form(default=""),
    actual_result: str = Form(default=""),
    acceptance_criteria: str = Form(default=""),
    environment: str = Form(default=""),
    impact: str = Form(default=""),
    suspected_component: str = Form(default=""),
    related_paths: str = Form(default=""),
    related_commits: str = Form(default=""),
    related_issue_ids: str = Form(default=""),
    status: str = Form(default="new"),
    notes: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, str]:
    return create_issue_record(
        reporter=reported_by,
        title=title,
        issue_type=issue_type,
        description=description,
        description_html=description_html,
        problem_summary=problem_summary,
        repro_steps_text=repro_steps,
        expected_result=expected_result,
        actual_result=actual_result,
        acceptance_criteria_text=acceptance_criteria,
        environment=environment,
        impact=impact,
        suspected_component=suspected_component,
        related_paths_text=related_paths,
        related_commits_text=related_commits,
        related_issue_ids_text=related_issue_ids,
        status=status,
        notes=notes,
        files=files,
    )


@app.post("/api/issues/{issue_id}/updates")
def api_update_issue(
    issue_id: str,
    updated_by: str = Form(...),
    note: str = Form(default=""),
    status: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, str]:
    return update_issue_record(
        issue_id=issue_id,
        updater=updated_by,
        note=note,
        status=status,
        files=files,
    )


@app.post("/api/issues/json")
def api_create_issue_json(payload: CreateIssueJsonRequest) -> dict[str, str]:
    return create_issue_record(
        reporter=payload.reported_by,
        title=payload.title,
        issue_type=payload.issue_type,
        description=payload.description,
        description_html=payload.description_html,
        problem_summary=payload.problem_summary,
        repro_steps_text="\n".join(payload.repro_steps),
        expected_result=payload.expected_result,
        actual_result=payload.actual_result,
        acceptance_criteria_text="\n".join(payload.acceptance_criteria),
        environment=payload.environment,
        impact=payload.impact,
        suspected_component=payload.suspected_component,
        related_paths_text=", ".join(payload.related_paths),
        related_commits_text=", ".join(payload.related_commits),
        related_issue_ids_text=", ".join(payload.related_issue_ids),
        status=payload.status,
        notes=payload.notes,
        files=[],
    )


@app.post("/api/issues/{issue_id}/updates/json")
def api_update_issue_json(issue_id: str, payload: UpdateIssueJsonRequest) -> dict[str, str]:
    return update_issue_record(
        issue_id=issue_id,
        updater=payload.updated_by,
        note=payload.note,
        status=payload.status,
        files=[],
    )


@app.get("/api/issues")
def api_list_issues(
    status: str = Query(default=""),
    issue_type: str = Query(default="", alias="type"),
    q: str = Query(default=""),
) -> list[dict[str, Any]]:
    repo = ensure_repo_ready()
    issues = list_issues(repo)
    query = q.strip().lower()

    filtered: list[dict[str, Any]] = []
    for issue in issues:
        if status and issue.get("status") != status:
            continue
        if issue_type and issue.get("type") != issue_type:
            continue
        if not issue_matches_query(issue, query):
            continue
        filtered.append(issue)
    return filtered


@app.get("/api/issues/{issue_id}")
def api_issue_detail(issue_id: str) -> dict[str, Any]:
    repo = ensure_repo_ready()
    issue, updates = load_issue(repo, issue_id)
    return {"issue": issue, "updates": updates}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    details: dict[str, Any] = {"ok": True}
    try:
        repo = ensure_repo_ready()
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
        details["repo"] = str(repo)
        details["branch"] = branch
        details["issues_root"] = SETTINGS.issues_root
    except Exception as exc:  # noqa: BLE001
        details["ok"] = False
        details["error"] = str(exc)
    return details


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
