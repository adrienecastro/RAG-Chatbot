import os
import logging
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()
logger = logging.getLogger(__name__)

SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]

# Caches service account token so credentials are only read once
gdrive_token = None

def get_gdrive_token():
    global gdrive_token
    if gdrive_token is None:
        creds = service_account.Credentials.from_service_account_file(os.getenv("GOOGLE_SERVICE_ACCOUNT"), scopes=SCOPE)
        gdrive_token = build("drive", "v3", credentials=creds)
    return gdrive_token


def get_folder_ids() -> dict:
    gdrive_ids = os.getenv("GOOGLE_DRIVE_FOLDER_IDS")
    folders = {}
    for entry in gdrive_ids.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        category, folder_id = entry.split(":", 1)
        folders[category.strip()] = folder_id.strip()
    return folders


def get_subfolder_ids(service, folder_id: str) -> list:
    all_ids = [folder_id]
    page_token = None

    while True:
        response = service.files().list(
            q=(f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"),
            fields="nextPageToken, files(id)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        for subfolder in response.get("files", []):
            all_ids.extend(get_subfolder_ids(service, subfolder["id"]))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return all_ids


def get_gdrive_files(service, folder_id: str) -> list:
    all_files = []
    page_token = None

    while True:
        response = service.files().list(
            q=(f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"),
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        all_files.extend(response.get("files", []))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return all_files


def get_gdrive_urls() -> dict:
    service = get_gdrive_token()
    folder_map = get_folder_ids()
    all_links = {}

    for category, root_folder_id in folder_map.items():
        all_folder_ids = get_subfolder_ids(service, root_folder_id)
        category_count = 0

        for folder_id in all_folder_ids:
            files = get_gdrive_files(service, folder_id)
            for file in files:
                all_links[file["name"]] = (f"https://drive.google.com/file/d/{file['id']}/view")
            category_count += len(files)

        print(f"Found {category_count} PDFs across {len(all_folder_ids)} folders in '{category}'.")
        logger.info(f"Found {category_count} PDFs across {len(all_folder_ids)} folders in '{category}'.")

    return all_links
