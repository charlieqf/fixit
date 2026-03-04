# Tester Issue Logger - System Design

## 1) Scope and Decisions

- Runtime choice: **FastAPI (Python)**
- Deploy target: CentOS VM (`10.0.0.182`)
- Reverse proxy: `nginx`
- Process manager: `systemd`
- Git backend: self-hosted GitLab (`gitlab.goldenstand.com` / `10.0.0.118`)
- Push model: **direct push** to branch `project-meituan`
- Storage path in repo branch: `quick-deal/Issues/`
- End-user auth: **no login**
- Status permissions: anyone can change status
- Issue ID format: `ISS-000001` (incremental)

## 2) High-Level Architecture

1. Browser UI (single-page or server-rendered HTML)
2. FastAPI backend
3. Local working clone on VM (service account token)
4. GitLab remote repo/branch as source of truth
5. Optional local SQLite index for fast list/filter

Flow:
- User submits/updates issue -> backend writes files in local clone -> `git add/commit/push` -> returns commit SHA/message to UI.

## 3) Repository File Layout

All issue files live under branch `project-meituan` in:

`quick-deal/Issues/`

Structure:

```text
quick-deal/
  Issues/
    ISS-000001/
      issue.yaml
      updates/
        2026-03-04T01-22-10Z.yaml
      attachments/
        2026-03-04T01-22-10Z_login-page.png
    ISS-000002/
      ...
    issue-index.json
```

## 4) Data Model

`issue.yaml`

```yaml
id: ISS-000001
title: "SSO login fails for new users"
type: bug            # bug | feature_request | issue
status: new          # new | triaged | in_progress | fixed | needs_input | closed
reported_by: "Alice"
created_at: "2026-03-04T01:22:10Z"
updated_at: "2026-03-04T01:22:10Z"
description: "Steps to reproduce..."
tags:
  - "sso"
```

`updates/<timestamp>.yaml`

```yaml
issue_id: ISS-000001
updated_at: "2026-03-04T03:10:11Z"
updated_by: "Bob"
note: "Re-tested on staging."
status_from: "in_progress"
status_to: "fixed"
attachments:
  - "attachments/2026-03-04T03-10-11Z_fixed-proof.png"
```

## 5) API Design

### POST `/api/issues`
Creates new issue + optional attachments.

Input:
- `title` (required)
- `description` (required)
- `type` (required)
- `reported_by` (required; text field since no login)
- `status` (optional, default `new`)
- `notes` (optional)
- `files[]` (optional screenshots/logs)

Output:
- `issue_id`
- `commit_sha`
- `commit_message`
- `issue_url` (e.g. `/issues/ISS-000001`)

### POST `/api/issues/{issue_id}/updates`
Adds note/status/attachments.

Input:
- `updated_by` (required)
- `note` (optional)
- `status` (optional)
- `files[]` (optional)

Output:
- `issue_id`
- `commit_sha`
- `commit_message`

### GET `/api/issues`
List issues with filters:
- `status`
- `type`
- `q` (title/id text)
- `page`, `page_size`

### GET `/api/issues/{issue_id}`
Returns issue metadata + chronological updates.

### GET `/api/health`
Checks app, local repo availability, and Git remote reachability.

## 6) Commit and Push Rules

Commit examples:
- New: `issue(ISS-000001): new bug - SSO login fails`
- Update: `issue(ISS-000001): status in_progress -> fixed`
- Note only: `issue(ISS-000001): add tester note`

Implementation behavior:
1. Acquire repository lock (single writer).
2. `git fetch origin`
3. `git checkout project-meituan`
4. `git reset --hard origin/project-meituan` (only in dedicated local clone)
5. Write files
6. `git add quick-deal/Issues/...`
7. `git commit -m "..."`
8. `git push origin project-meituan`
9. Return commit SHA/message to UI

If push conflict:
- auto-retry flow (max 3): refresh branch, reapply write, commit, push.

## 7) UI Pages

### `/` - Issue List
- Table/cards with ID, title, type, status, updated_at
- Filters by status/type/search
- Row click to issue detail

### `/issues/new` - New Issue Form
- Fields: reporter name, title, type, description, optional note, attachments
- Submit button
- Success modal with commit SHA/message + copy buttons

### `/issues/{id}` - Issue Detail + Update
- Read issue summary and update timeline
- Add note
- Change status
- Upload attachments
- Submit update -> show commit SHA/message

## 8) No-Login UX Strategy

- No authentication gate
- Each submit/update requires a simple `Name` field
- Backend records IP + name + timestamp in update metadata
- Optional shared office kiosk mode supported

## 9) Security and Guardrails

- GitLab token stored in env file readable only by service user
- Upload allowlist: `.png, .jpg, .jpeg, .webp, .txt, .log`
- Max file size: 10 MB (default; configurable)
- Max files per request: 10 (default; configurable)
- Filename sanitization and path traversal prevention
- Request size limit at nginx and FastAPI

## 10) Deployment Layout on CentOS

Suggested paths:

```text
/opt/tester-issues/app            # FastAPI app
/opt/tester-issues/repo           # local clone of quick-deal
/opt/tester-issues/uploads-tmp    # temp upload staging
/var/log/tester-issues            # logs
```

Env (`/etc/tester-issues.env`):

```bash
APP_ENV=prod
HOST=127.0.0.1
PORT=8010
GIT_REPO_URL=http://oauth2:${GITLAB_TOKEN}@gitlab.goldenstand.com/qd-team/quick-deal.git
GIT_BRANCH=project-meituan
ISSUES_ROOT=quick-deal/Issues
LOCAL_REPO_PATH=/opt/tester-issues/repo
MAX_FILE_MB=10
MAX_FILES_PER_SUBMIT=10
```

## 11) systemd Service (example)

`/etc/systemd/system/tester-issues.service`

```ini
[Unit]
Description=Tester Issue Logger API
After=network.target

[Service]
User=testerissues
Group=testerissues
WorkingDirectory=/opt/tester-issues/app
EnvironmentFile=/etc/tester-issues.env
ExecStart=/opt/tester-issues/app/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8010 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 12) nginx Site (example)

```nginx
server {
    listen 80;
    server_name 10.0.0.182;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 13) Why FastAPI Here

- Strong request validation for mixed form + file uploads
- Easy async/background processing for commit/push tasks
- Simple deployment with `uvicorn` + `systemd`
- Python file/git tooling is straightforward for this workflow

## 14) MVP Delivery Plan

1. Implement FastAPI endpoints and git service module
2. Build minimal HTML UI (list/new/detail)
3. Add commit SHA/message success modal + copy action
4. Add index rebuild and list filters
5. Add deployment scripts for CentOS (`systemd` + `nginx`)
6. Add smoke tests for create/update/list flows
