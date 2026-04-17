# Internal RAG Chatbot

A simple Slack-based internal support chatbot that helps answer IT support questions
by searching a local knowledge base of PDF and TXT documents using
Retrieval-Augmented Generation (RAG). Powered by Google Gemini, LlamaIndex,
ChromaDB, and sentence-transformers. Runs entirely on-premises with no
inbound firewall ports required.

---

## Architecture overview

```
Slack user
    │  @mention
    ▼
Slack platform (Socket Mode — outbound WebSocket, no open ports)
    │
    ▼
bot.py (Slack Bolt) → rag.py (LlamaIndex + ChromaDB + Gemini API)
                    → ingest.py (pdf_reader + gdrive_sync + ChromaDB)
                    → pdf_images.py (pymupdf image extraction)
                    → chat_history.py (per-user conversation memory)
                    → feedback.py (survey results + admin DM)
                    → error_logging.py (logs errors and rotates logs)
                    → slack_survey.py (prompts slack survey)

Google Drive API    ← gdrive_sync.py (PDF link discovery, Team Drive)
Gemini API          ← rag.py (primary: gemini-3.1-lite-preview, secondary: gemini-2.5-flash)
```

---

## Prerequisites

- Arch Linux server with bash (if using different distro/shell substitute command equivalents)
- Python 3.11 (not 3.14 — C-extension compatibility)
- Internet access from the server (outbound only)
- A Google cloud/drive account (Gemini API + Google Drive)
- A Slack workspace with admin rights
- 16 GB RAM minimum recommended
- Optional: The bot domain must have a DNS A record if you ever expose the API externally

---

## Step 1 — Server setup

```bash
# Update the system
sudo pacman -Syu

# Install Arch dependencies
sudo pacman -S python311 python-pip git tesseract tesseract-data-eng poppler uv pkgconf base-devel libjpeg-turbo libpng

# Create a system account (no login shell, no home directory)
sudo useradd --system -m -d <project directory> --shell /usr/sbin/nologin supportbot

# Create project directory
sudo mkdir <project directory>
sudo chown <chatbot name>:<chatbot name> <project directory>

# Create document directories (adjust names to match your structure)
sudo mkdir -p "<project directory>/docs/IT Knowledge Base"
sudo mkdir  "<project directory>/docs/User Manuals"
sudo mkdir  <project directory>/docs/Miscellaneous
sudo chown -R <chatbot name>:<chatbot name> <project directory>/docs/
```

---

## Step 2 — Google Cloud setup

### Enable the Drive API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project named `<chatbot name>`
3. Go to **APIs & Services → Library** → search **Google Drive API** → Enable

### Create a service account

1. Go to **APIs & Services → Credentials → Create Credentials → Service Account**
2. Name: `<google service account name>`
3. Once created: click the account → **Keys** tab → **Add Key → JSON**
4. Download the JSON file and copy it to the server:

```bash
sudo chown <chatbot name>:<chatbot name> <project directory>/.gdrive-token.json
sudo chmod 600 <project directory>/.gdrive-token.json
```

### Share your Drive folders with the service account

1. Open the JSON file and copy the `client_email` value
2. In Google Drive, open your Team Drive → right-click the root → **Manage members**
3. Add the `client_email` as a **Viewer**

### Create your Gemini API key

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create an API key under your Google Cloud account
3. Save it — you will add it to `.env` in Phase 4

---

## Step 3 — Slack app setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From Scratch**
2. Name: <chatbot name>, select your workspace

### Enable Socket Mode

Go to **Socket Mode** → enable → generate an app-level token with `connections:write` scope.
Copy the token (starts with `xapp-`) → this is `SLACK_APP_TOKEN`.

### Add OAuth scopes

Go to **OAuth & Permissions → Bot Token Scopes** and add:

| Scope | Purpose |
|---|---|
| `app_mentions:read` | Receive @mention events |
| `chat:write` | Post messages |
| `channels:history` | Read channel messages |
| `commands` | Handle slash commands |
| `files:write` | Upload images |
| `im:write` | Open DMs for admin notifications |

### Enable Interactivity

Go to **Interactivity & Shortcuts** → toggle on. No URL needed with Socket Mode.

### Create slash commands

Go to **Slash Commands** and create each of the following (leave Request URL blank):

| Command | Description |
|---|---|
| `/reload` | Reload the support knowledge base (admin only) |
| `/chat-clear` | Clear your conversation history |
| `/chat-stats` | View feedback statistics (admin only) |

### Install the app

Go to **OAuth & Permissions → Install to Workspace**. Copy the **Bot User OAuth Token** (starts with `xoxb-`) → this is `SLACK_BOT_TOKEN`.

### Get admin user IDs

In Slack, click each admin user's profile → **More → Copy member ID**.

---

## Step 4 — Project files

### Clone or copy files to the server

```bash
sudo cp -r /path/to/project/* <project directory>/
sudo chown -R <chatbot name>:<chatbot name> <project directory>/
```

### Set permissions

```bash
# Secrets — only chatbot can read
sudo chmod 600 <project directory>/.env
sudo chmod 600 <project directory>/.gdrive-token.json

# Code — owned by root, readable by chatbot
sudo chown root:root <project directory>/*.py
sudo chmod 644 <project directory>/*.py

# Writable directories — chatbot needs write access
sudo chown -R <chatbot name>:<chatbot name> <project directory>/docs/
sudo chown -R <chatbot name>:<chatbot name> <project directory>/.venv/ 

# Create log file
sudo mkdir <project directory>/logs
sudo chmod 4744 <project directory>/logs
sudo touch <project directory>/logs/error.log
sudo chown -R <chatbot name>:<chatbot name> <project directory>/logs
sudo chmod 644 <project directory>/logs/error.log

# Create feedback file
sudo touch <project directory>/.feedback.json
sudo chown <chatbot name>:<chatbot name> <project directory>/.feedback.json
sudo chmod 640 <project directory>/.feedback.json
```

### Create `.env`

```bash
# Gemini API
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite-preview
GEMINI_SECONDARY_MODEL=gemini-2.5-flash

# Slack
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
ADMIN_SLACK_USER_IDS=U012AB3CD,U098ZY7WX

# Google Drive
GOOGLE_SERVICE_ACCOUNT_PATH=<project directory>/gdrive_token.json
GOOGLE_DRIVE_FOLDER_IDS=IT Knowledge Base:FOLDER_ID_HERE|User Manuals:FOLDER_ID_HERE|Miscellaneous:FOLDER_ID_HERE

# Local paths (use absolute paths)
CHROMA_PATH=<project directory>/chroma_db
DOCS_PATHS=”<project directory>/docs/IT Knowledge Base,<project directory>/docs/User Manuals,<project directory>/docs/Miscellaneous”
LLAMA_INDEX_CACHE_DIR=<project directory>/.llama-cache
HF_HOME=<project directory>/.hf-cache
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# Security
HASH_CACHE_SECRET=long-random-string
```

Find your Drive folder IDs from the URL when the folder is open in your browser:
`https://drive.google.com/drive/folders/THIS_PART_IS_THE_ID`

---

## Step 5 — Create uv pip helper and install dependencies

```bash
# Create pip cache directory
sudo mkdir  <project directory>/.pip-cache
sudo chown <chatbot name>:<chatbot name> <project directory>/.pip-cache

# Create uv pip helper scripts
pipInstall.sh:
#!/bin/bash
sudo -u <chatbot name> uv pip install --cache-dir <project directory>/.pip-cache --python <project directory>/.venv/bin/python "$@"
EOF

pipUninstall.sh:
#!/bin/bash
sudo -u <chatbot name> uv pip uninstall --cache-dir <project directory>/.pip-cache --python <project directory>/.venv/bin/python "$@"
EOF

sudo chmod +x /opt/KeyWatchBot/pipInstall.sh
sudo chmod +x /opt/KeyWatchBot/pipUninstall.sh

# Create virtualenv using Python 3.11
sudo -u <chatbot name> python3.11 -m venv <project directory>/.venv
sudo chown -R <chatbot name>:<chatbot name> <project directory>/.venv

# Install python dependencies
<project directory>/pipinstall.sh -r <project directory>/dependencies.lock

# Optional: If dependencies get updated, pin new versions for reproducibility
sudo -u <chatbot name> <project directory>/.venv/bin/pip freeze > <project directory>/new-dependencies.lock

```

---

## Step 6 — Add documents and run initial ingest

```bash
# Copy PDFs into the appropriate category/product folders
sudo cp /path/to/pdfs/*.pdf "<project directory>/docs/IT Knowledge Base/"
sudo chown -R <chatbot name>:<chatbot name> <project directory>/docs/

# Create cache directories
sudo -u <chatbot name> mkdir <project directory>/.llama-cache
sudo -u <chatbot name> mkdir <project directory>/.hf-cache

# Run ingest (first run downloads the embedding model — takes a few minutes)
cd <project directory>
sudo -u <chatbot name> .venv/bin/python ingest.py
```

### Test before installing as a service

```bash
sudo -u <chatbot name> .venv/bin/python bot.py
```

Go to Slack, @mention the bot with a test question. Press `Ctrl+C` when done.

---

## Step 7 — Install as systemd service

Create `/etc/systemd/system/<chatbot service name>.service`:

```ini
[Unit]
Description=<chatbot name> Support RAG Bot
After=network.target

[Service]
Type=simple
User=<chatbot name>
WorkingDirectory=<project directory>
ExecStart=<project directory>/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now <chatbot service name>
sudo systemctl status <chatbot service name>
```

---

## Ongoing operations

### Adding or updating documents

```
1. Upload PDF to Google Drive in the correct folder
2. Copy new PDFs to <project directory>/docs/<category>/
3. Run /reload in Slack (admin only)
```

### Monitoring Examples

```bash
# Live logs
journalctl -u <chatbot service name> -f

# Error log file
tail -f <project directory>/logs/error.log

# Search for specific errors
grep "Gemini" <project directory>/logs/error.log

# Service status
sudo systemctl status <chatbot service name>
```

### Restarting after a code change

```bash
sudo systemctl restart <chatbot service name>
```

### Reloading the vector index completely

```bash
rm <project directory>/.hash-cache.json
rm -rf <project directory>/chroma_db
sudo systemctl restart <chatbot service name>
sudo -u <chatbot name> <project directory>/.venv/bin/python ingest.py
```

---

## Slash command reference

| Command | Who can use | What it does |
|---|---|---|
| `/reload` | Admins only | Re-indexes all documents from scratch |
| `/chat-clear` | All users | Clears the user's conversation history |
| `/chat-stats` | Admins only | Shows positive/negative feedback counts |

---

## TXT file format

Custom `.txt` files can be added to any docs folder to provide answers for topics
not covered by existing PDFs. Make sure txt files only cover 1 topic. Format:

```
TOPIC: Brief description of the topic
PRODUCT: Product name (optional — omit for general knowledge)
CATEGORY: IT Knowledge Base
KEYWORDS: keyword1, keyword2, keyword3, keyword4, keyword5
SOURCE_PDF: Optional PDF filename to credit as source
SOURCE_URL: URL for listed SOURCE_PDF file

ISSUE:
- Issue 1
- Issue 2

Cause:
1. Issue 1 can be caused by ...
2. Issue 1 and 2 can be caused by ...

Steps to resolve:
1. First step
2. Second step
3. Third step
```

The bot requires at least 3 keyword matches (can be adjusted in rag.py) between the user's question and a
TXT file's KEYWORDS header before including that file's content in answers.
Include synonyms and common phrasings in the KEYWORDS line.

---

## Project file structure

```
<project directory>/
├── .env
├── .gitignore
├── dependencies.txt        ← human-readable dependencies
├── dependencies.lock       ← pinned versions for reproducibility
├── pipInstall.sh           ← pip helper script
├── pipUninstall.sh         ← pip helper script
├── bot.py                  ← Slack bot and event handlers
├── rag.py                  ← retrieval and generation pipeline
├── ingest.py               ← document ingestion pipeline
├── gdrive_sync.py          ← Google Drive API integration
├── pdf_reader.py           ← PDF text extraction and chunking
├── extract_images.py       ← PDF image extraction
├── chat_history.py         ← per-user conversation memory
├── feedback.py             ← survey result storage
├── gdrive_token.json       ← service account credentials
├── .feedback.json          ← stored user feedback
├── .hash-cache.json        ← HMAC-signed file hash cache
├── .pip-cache/             ← pip download cache
├── .llama-cache/           ← LlamaIndex model cache
├── .hf-cache/              ← HuggingFace model cache
├── .venv/                  ← Python 3.11 virtual environment
├── chroma_db/              ← ChromaDB vector store
└── logs/
    └── error.log           ← rotating error log (5MB x 4 files)
└── docs/
    ├── IT Knowledge Base/
    │   └── *.pdf, *.txt
    ├── User Manuals/
    │   ├── Product A/
    │   │   └── *.pdf
    │   └── Product B/
    │       └── *.pdf
    └── Miscellaneous/
        ├── Product A/
        │   └── *.pdf
        │   └── *.txt
        ├── Product B/
        │   └── *.pdf
        │   └── *.txt
