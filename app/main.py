from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

LOGGER = logging.getLogger("tester_issues")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ISSUE_ID_PATTERN = re.compile(r"^ISS-(\d{6})$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
ISSUE_TYPES = ("bug", "feature_request", "issue")
STATUSES = ("new", "triaged", "in_progress", "fixed", "needs_input", "closed")
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".txt", ".log"}


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


SETTINGS = Settings()
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
REPO_LOCK = threading.Lock()

app = FastAPI(title="Tester Issue Logger")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def ensure_non_empty(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return value


def sanitize_filename(filename: str) -> str:
    raw = Path(filename or "").name
    cleaned = SAFE_FILENAME_PATTERN.sub("_", raw).strip("._")
    return cleaned[:128] or "file"


def repo_path() -> Path:
    return Path(SETTINGS.local_repo_path).resolve()


def issues_root_path(repo: Path) -> Path:
    return repo / Path(SETTINGS.issues_root)


def run_git(args: list[str], cwd: Path, check: bool = True) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
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
        yaml.safe_dump(content, handle, sort_keys=False, allow_unicode=False)


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


def validate_files(files: list[UploadFile]) -> None:
    real_files = [f for f in files if f and f.filename]
    if len(real_files) > SETTINGS.max_files_per_submit:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Max allowed is {SETTINGS.max_files_per_submit}",
        )


def save_attachments(
    files: list[UploadFile],
    issue_dir: Path,
    timestamp: str,
) -> list[str]:
    validate_files(files)
    saved: list[str] = []
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
                handle.write(chunk)
        saved.append(f"attachments/{final_name}")

    return saved


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
        run_git(["push", "origin", SETTINGS.git_branch], cwd=repo)
    sha = run_git(["rev-parse", "HEAD"], cwd=repo)
    return sha, message


def create_issue_record(
    *,
    reporter: str,
    title: str,
    issue_type: str,
    description: str,
    status: str,
    notes: str,
    files: list[UploadFile],
) -> dict[str, str]:
    reporter = ensure_non_empty(reporter, "reported_by")
    title = ensure_non_empty(title, "title")
    description = ensure_non_empty(description, "description")
    issue_type = ensure_non_empty(issue_type, "type")

    if issue_type not in ISSUE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid issue type")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail="Invalid issue status")
    if not status:
        status = "new"

    with REPO_LOCK:
        repo = ensure_repo_ready()
        sync_repo(repo)
        root = issues_root_path(repo)
        issue_id = next_issue_id(root)
        issue_dir = root / issue_id
        issue_dir.mkdir(parents=True, exist_ok=True)

        created_at = now_utc_iso()
        file_stamp = now_utc_file_stamp()
        attachments = save_attachments(files, issue_dir, file_stamp)

        issue_doc = {
            "id": issue_id,
            "title": title,
            "type": issue_type,
            "status": status,
            "reported_by": reporter,
            "created_at": created_at,
            "updated_at": created_at,
            "description": description,
        }
        write_yaml(issue_dir / "issue.yaml", issue_doc)

        if notes.strip() or attachments:
            update_doc = {
                "issue_id": issue_id,
                "updated_at": created_at,
                "updated_by": reporter,
                "note": notes.strip(),
                "status_from": status,
                "status_to": status,
                "attachments": attachments,
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

    with REPO_LOCK:
        repo = ensure_repo_ready()
        sync_repo(repo)
        issue, _ = load_issue(repo, issue_id)
        issue_dir = issues_root_path(repo) / issue_id
        old_status = issue.get("status", "new")
        new_status = status or old_status

        file_stamp = now_utc_file_stamp()
        attachments = save_attachments(files, issue_dir, file_stamp)
        if not note and not attachments and new_status == old_status:
            raise HTTPException(
                status_code=400,
                detail="No changes detected. Add a note, attachment, or status change.",
            )

        changed_at = now_utc_iso()
        update_doc = {
            "issue_id": issue_id,
            "updated_at": changed_at,
            "updated_by": updater,
            "note": note,
            "status_from": old_status,
            "status_to": new_status,
            "attachments": attachments,
        }
        write_yaml(issue_dir / "updates" / f"{file_stamp}.yaml", update_doc)

        issue["status"] = new_status
        issue["updated_at"] = changed_at
        write_yaml(issue_dir / "issue.yaml", issue)
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


@app.on_event("startup")
def startup() -> None:
    try:
        repo = ensure_repo_ready()
        LOGGER.info("Repository ready at %s", repo)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Repository initialization failed at startup: %s", exc)


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
        if query and query not in json.dumps(issue).lower():
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
    description: str = Form(...),
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
    candidate = (issue_dir / attachment_path).resolve()
    if not str(candidate).startswith(str(issue_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(path=candidate)


@app.post("/api/issues")
def api_create_issue(
    reported_by: str = Form(...),
    title: str = Form(...),
    issue_type: str = Form(...),
    description: str = Form(...),
    status: str = Form(default="new"),
    notes: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, str]:
    return create_issue_record(
        reporter=reported_by,
        title=title,
        issue_type=issue_type,
        description=description,
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
        if query and query not in json.dumps(issue).lower():
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
