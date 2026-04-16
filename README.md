# KeyWatchBot — Internal RAG Support Chatbot
A Slack-based internal support chatbot that answers tier 1 support questions by searching a local knowledge base of PDF and TXT documents using Retrieval-Augmented Generation (RAG). Powered by Google Gemini, LlamaIndex, ChromaDB, and sentence-transformers. Runs entirely on-premises with no inbound firewall ports required.
________________________________________

# Architecture overview
Slack user
    │  @mention
    ▼
Slack platform (Socket Mode — outbound WebSocket, no open ports)
    │
    ▼
bot.py (Slack Bolt) 
    → rag.py (LlamaIndex + ChromaDB + Gemini API)
    → ingest.py (pdf_reader + gdrive_sync + ChromaDB)
    → pdf_images.py (pymupdf image extraction)
    → chat_history.py (per-user conversation memory)
    → feedback.py (survey results + admin DM)
    → error_logging.py (logs errors and rotates logs)
    → slack_surve.py (prompts slack survey)

Google Drive API    ← gdrive_sync.py (PDF link discovery from Team Drive)
Gemini API      ← rag.py (primary: gemini-3.1-lite-preview, fallback: gemini-2.5-flash)
________________________________________

# Prerequisites
•   Arch Linux server
•   Python 3.11 (not 3.14 — C-extension compatibility)
•   Internet access from the server (outbound only)
•   A Google Cloud/Drive account (Gemini API + Google Drive)
•   A Slack workspace with admin rights
•   16 GB RAM minimum recommended
•   Optional: The bot domain must have a DNS A record if you ever expose the API externally
________________________________________

# Step 1 — Server setup
Update the system
sudo pacman -Syu

Install Arch dependencies
sudo pacman -S python311 python-pip git tesseract tesseract-data-eng poppler uv pkgconf base-devel libjpeg-turbo libpng

Create a system account (no login shell, project directory as home directory)
sudo useradd --system -m -d /opt/KeyWatchBot --shell /usr/sbin/nologin KeyWatchBot

Create document directories (adjust names to match your structure)
sudo mkdir -p "/opt/KeyWatchBot/docs/IT Knowledge Base"
sudo mkdir  "/opt/KeyWatchBot/docs/User Manuals"
sudo mkdir  /opt/KeyWatchBot/docs/Miscellaneous
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/docs/
________________________________________

# Step 2 — Google Cloud setup
Enable the Drive API
1.  Go to console.cloud.google.com
2.  Create a new project named KeyWatchBot
3.  Go to APIs & Services → Library → search Google Drive API → Enable

Create a service account
1.  Go to APIs & Services → Credentials → Create Credentials → Service Account
2.  Name: keywatchbot-reader
3.  Once created: click the account → Keys tab → Add Key → JSON
4.  Download the JSON file and copy it to the server:
        sudo chown KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/.gdrive-token.json
        sudo chmod 600 /opt/KeyWatchBot/.gdrive-token.json

Share your Drive folders with the service account
1.  Open the JSON file and copy the client_email value
2.  In Google Drive, open your Team Drive → right-click the root → Manage members
3.  Add the client_email as a Viewer

Get your Gemini API key
1.  Go to aistudio.google.com/apikey
2.  Create an API key under your Google Business account
3.  Save it — you will add it to .env in Step 4
________________________________________

# Step 3 — Slack app setup
1.  Go to api.slack.com/apps → Create New App → From Scratch
2.  Name: KeyWatchBot, select your workspace

Enable Socket Mode
Go to Socket Mode → enable → generate an app-level token with connections:write scope. Copy the token (starts with xapp-) → this is SLACK_APP_TOKEN.

Add OAuth scopes
Go to OAuth & Permissions → Bot Token Scopes and add:
Scope               Purpose
app_mentions:read   Receive @mention events
chat:write          Post messages
channels:history    Read channel messages
commands            Handle slash commands
files:write         Upload images
im:write            Open DMs for admin notifications
    
Enable Interactivity
Go to Interactivity & Shortcuts → toggle on. No URL needed with Socket Mode.
Create slash commands
Go to Slash Commands and create each of the following (leave Request URL blank):

Command         Description
/reload         Reload the support knowledge base (admin only)
/chat-clear     Clear your conversation history
/chat-stats     View feedback statistics (admin only)

Install the app
Go to OAuth & Permissions → Install to Workspace. Copy the Bot User OAuth Token (starts with xoxb-) → this is SLACK_BOT_TOKEN.

Get admin user IDs
In Slack, click each admin user's profile → More → Copy member ID.
________________________________________

# Step 4 — Project files
Clone or copy files to the server
sudo cp -r /path/to/project/* /opt/KeyWatchBot/
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/

Set permissions:
sudo chmod 600 /opt/KeyWatchBot/.env
sudo chmod 600 /opt/KeyWatchBot/.gdrive-token.json

sudo chown root:root /opt/KeyWatchBot/*.py
sudo chmod 644 /opt/KeyWatchBot/*.py

sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/docs/
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/.venv/ 

sudo mkdir /opt/KeyWatchBot/logs
sudo chmod 4744 /opt/KeyWatchBot/logs
sudo touch /opt/KeyWatchBot/error.log
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/logs
sudo chmod 644 /opt/KeyWatchBot/error.log

Create feedback file:
sudo touch /opt/KeyWatchBot/.feedback.json
sudo chown KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/.feedback.json
sudo chmod 640 /opt/KeyWatchBot/.feedback.json

Create .env:
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite-preview
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
GOOGLE_SERVICE_ACCOUNT_PATH=/opt/KeyWatchBot/.gdrive-token.json
GOOGLE_DRIVE_FOLDER_IDS="IT Knowledge Base:1FOLDER_ID_HERE,User Manuals:1FOLDER_ID_HERE,Miscellaneous:FOLDER_ID_HERE"

SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
ADMIN_SLACK_USER_IDS=your-slack-id(s)

CHROMA_PATH=/opt/KeyWatchBot/chroma_db
DOCS_PATHS=”/opt/KeyWatchBot/docs/IT Knowledge Base,/opt/KeyWatchBot/docs/User Manuals,/opt/KeyWatchBot/docs/Miscellaneous”
LLAMA_INDEX_CACHE_DIR=/opt/KeyWatchBot/.llama-cache
HF_HOME=/opt/KeyWatchBot/.hf-cache

*Optional: embedding model path if running fully offline
EMBEDDING_MODEL_PATH=/opt/KeyWatchBot/models/all-MiniLM-L6-v2

HASH_CACHE_SECRET=your-secret-token

Find your Drive folder IDs from the URL when the folder is open in your browser: https://drive.google.com/drive/folders/THIS_PART_IS_THE_ID
________________________________________

# Step 5 — Create pip helper and install dependencies
Create pip cache directory:
sudo mkdir  /opt/KeyWatchBot/.pip-cache
sudo chown KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/.pip-cache

Create uv pip helper scripts:
sudo touch pipInstall.sh pipUninstall.sh

pipInstall.sh:
#!/bin/bash
sudo -u KeyWatchBot uv pip install --cache-dir /opt/KeyWatchBot/.pip-cache --python /opt/KeyWatchBot/.venv/bin/python "$@"
EOF

pipUninstall.sh:
#!/bin/bash
sudo -u KeyWatchBot uv pip uninstall --cache-dir /opt/KeyWatchBot/.pip-cache --python /opt/KeyWatchBot/.venv/bin/python "$@"
EOF

sudo chmod +x /opt/KeyWatchBot/pipInstall.sh
sudo chmod +x /opt/KeyWatchBot/pipUninstall.sh

Create virtualenv using Python 3.11:
sudo -u KeyWatchBot python3.11 -m venv /opt/KeyWatchBot/.venv
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/.venv

Install python dependencies:
/opt/KeyWatchBot/pipinstall.sh -r /opt/KeyWatchBot/requirements.txt

Pin installed versions for reproducibility:
sudo -u KeyWatchBot /opt/KeyWatchBot/.venv/bin/pip freeze > /opt/KeyWatchBot/requirements.lock
________________________________________

# Step 6 — Add documents and run initial ingest
Copy PDFs into the appropriate category/product folders:
sudo cp /path/to/pdfs/*.pdf "/opt/KeyWatchBot/docs/IT Knowledge Base/"
sudo chown -R KeyWatchBot:KeyWatchBot /opt/KeyWatchBot/docs/

Create cache directories:
sudo -u KeyWatchBot mkdir -p /opt/KeyWatchBot/.llama-cache
sudo -u KeyWatchBot mkdir -p /opt/KeyWatchBot/.hf-cache

Run ingest (first run downloads the embedding model — takes a few minutes):
cd /opt/KeyWatchBot
sudo -u KeyWatchBot .venv/bin/python ingest.py

Test before installing as a service:
sudo -u KeyWatchBot /opt/KeyWatchBot/.venv/bin/python /opt/KeyWatchBot/bot.py
Go to Slack, @mention the bot with a test question. Press Ctrl+C when done.
________________________________________

# Step 7 — Install as systemd service
sudo vim /etc/systemd/system/keywatchbot.service

[Unit]
Description=KeyWatchBot Support RAG Slack Bot
After=network.target

[Service]
Type=simple
User=KeyWatchBot
WorkingDirectory=/opt/KeyWatchBot
ExecStart=/opt/KeyWatchBot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
EnvironmentFile=/opt/KeyWatchBot/.env
Environment=LLAMA_INDEX_CACHE_DIR=/opt/KeyWatchBot/.llama-cache
Environment=HF_HOME=/opt/KeyWatchBot/.hf-cache
Environment=HOME=/opt/KeyWatchBot

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now keywatchbot
sudo systemctl status keywatchbot
________________________________________

Ongoing operations
Adding or updating documents
1. Upload PDF to Google Drive in the correct folder
2. Copy PDF to /opt/KeyWatchBot/docs/<category>/
3. Run /reload in Slack (admin only)

Monitoring examples:
journalctl -u keywatchbot -f
tail -f /opt/KeyWatchBot/logs/error.log
grep "Gemini" /opt/KeyWatchBot/logs/error.log

Restarting after a code change:
sudo systemctl restart keywatchbot

Reloading the vector index completely:
rm /opt/KeyWatchBot/.hash-cache.json
rm -rf /opt/KeyWatchBot/chroma_db
sudo systemctl restart keywatchbot
Then run /reload in Slack
________________________________________

# Slash command reference
Command         Who can use     What it does
/reload         Admins only     Re-indexes all documents from scratch
/clear-chat     All users       Clears the caller's conversation history
/chat-stats     Admins only     Shows positive/negative feedback counts
________________________________________

# TXT knowledge base file format
Custom .txt files can be added to any docs folder to provide answers for topics not covered by existing PDFs. Format:
TOPIC: Brief description of the topic
PRODUCT: Product name (optional — omit for general knowledge)
CATEGORY: IT Knowledge Base
KEYWORDS: keyword1, keyword2, keyword3, keyword4, keyword5
SOURCE_PDF: Optional PDF filename to credit as source
SOURCE_URL: Optional PDF URL 

Body text goes here. Write clearly and concisely. Include both the cause of
a problem and the steps to resolve it in the same file.

Steps to resolve:
1. First step
2. Second step
3. Third step
The bot requires at least 3 keyword matches between the user's question and a TXT file's KEYWORDS header before including that file's content in answers. Include synonyms and common phrasings in the KEYWORDS line.
________________________________________

# Project file structure
/opt/KeyWatchBot/
├── .env
├── .gitignore
├── requirements.txt        ← human-readable dependencies
├── requirements.lock       ← pinned versions for reproducibility
├── pipInstall.sh           ← pip helper script
├── pipUninstall.sh         ← pip helper script
├── bot.py                  ← Slack bot and event handlers
├── rag.py                  ← retrieval and generation pipeline
├── ingest.py               ← document ingestion pipeline
├── gdrive_sync.py          ← Google Drive API integration
├── pdf_reader.py           ← PDF text extraction and chunking
├── pdf_images.py           ← PDF image extraction
├── chat_history.py         ← per-user conversation memory
├── feedback.py             ← survey result storage
├── slack_survey.py         ← prompts slack survey for feedback
├── error_logging.py        ← rotating error log (5MB x 4 files)
├── .gdrive-token.json      ← service account credentials (chmod 600)
├── .feedback.json          ← stored user feedback
├── .hash-cache.json        ← HMAC-signed file hash cache
├── .pip-cache/             ← pip download cache
├── .llama-cache/           ← LlamaIndex model cache
├── .hf-cache/              ← HuggingFace model cache
├── .venv/                  ← Python 3.11 virtual environment
├── chroma_db/              ← ChromaDB vector store (rebuilt by /reload)
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