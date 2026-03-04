# Tester Issue Logger

Internal web app for software testers to submit and update issues.  
Each submit/update writes files under `quick-deal/Issues` and performs a git commit + push, then returns commit SHA and message.

## Features

- Create issues with type, description, notes, and attachments
- Update issue status/notes/attachments
- List/filter all issues
- View issue detail and update timeline
- No tester login required (name fields only)
- Direct push to branch `project-meituan`

## Tech stack

- FastAPI
- Jinja2 templates
- Git-backed file storage (GitLab)

## Environment variables

Required for production push:

```bash
GIT_REPO_URL=http://oauth2:<TOKEN>@gitlab.goldenstand.com/qd-team/quick-deal.git
GIT_BRANCH=project-meituan
ISSUES_ROOT=quick-deal/Issues
LOCAL_REPO_PATH=/opt/tester-issues/repo
GIT_USER_NAME=Tester Issues Bot
GIT_USER_EMAIL=tester-issues-bot@goldenstand.local
MAX_FILE_MB=10
MAX_FILES_PER_SUBMIT=10
```

## Local run

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8010
```

Open `http://127.0.0.1:8010/issues`.

## CentOS deployment runbook

Target VM: `10.0.0.182`  
GitLab: `http://gitlab.goldenstand.com/qd-team/quick-deal`  
Branch: `project-meituan`  
Issue folder in branch: `quick-deal/Issues`

### 1) Install OS packages

```bash
sudo dnf install -y python3 python3-pip git nginx
```

### 2) Create service user and directories

```bash
sudo useradd --system --create-home --home-dir /opt/tester-issues testerissues || true
sudo mkdir -p /opt/tester-issues/app /opt/tester-issues/repo /var/log/tester-issues
sudo chown -R testerissues:testerissues /opt/tester-issues /var/log/tester-issues
```

### 3) Copy application code

Copy this repository's files into `/opt/tester-issues/app`, then:

```bash
cd /opt/tester-issues/app
sudo -u testerissues python3 -m venv .venv
sudo -u testerissues /opt/tester-issues/app/.venv/bin/pip install --upgrade pip
sudo -u testerissues /opt/tester-issues/app/.venv/bin/pip install -r requirements.txt
```

### 4) Configure environment and GitLab token

Create env file with strict permissions:

```bash
sudo install -m 600 /dev/null /etc/tester-issues.env
```

Edit `/etc/tester-issues.env`:

```bash
APP_ENV=prod
HOST=127.0.0.1
PORT=8010
GIT_REPO_URL=http://oauth2:<YOUR_GITLAB_TOKEN>@gitlab.goldenstand.com/qd-team/quick-deal.git
GIT_BRANCH=project-meituan
ISSUES_ROOT=quick-deal/Issues
LOCAL_REPO_PATH=/opt/tester-issues/repo
GIT_USER_NAME=Tester Issues Bot
GIT_USER_EMAIL=tester-issues-bot@goldenstand.local
MAX_FILE_MB=10
MAX_FILES_PER_SUBMIT=10
```

Validate token can read repo:

```bash
source /etc/tester-issues.env
git ls-remote "$GIT_REPO_URL"
```

### 5) Install systemd service

```bash
sudo cp deploy/tester-issues.service /etc/systemd/system/tester-issues.service
sudo systemctl daemon-reload
sudo systemctl enable --now tester-issues
sudo systemctl status tester-issues --no-pager
```

### 6) Install nginx config

```bash
sudo cp deploy/nginx-tester-issues.conf /etc/nginx/conf.d/tester-issues.conf
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

### 7) Smoke tests

From VM:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -I http://127.0.0.1/
```

From your network:

```bash
curl -I http://10.0.0.182/
```

### 8) Common operations

Restart app:

```bash
sudo systemctl restart tester-issues
```

View logs:

```bash
sudo journalctl -u tester-issues -n 200 --no-pager
```

If token is rotated:

1. Update `/etc/tester-issues.env`.
2. Run `sudo systemctl restart tester-issues`.
