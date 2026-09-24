from pathlib import Path
from datetime import datetime, timedelta
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from config import (
    DRIVE_MAIN_FOLDER_ID,
    DRIVE_EPISODES_FOLDER_ID,
    MP3_RETENTION_DAYS,
)


PROJECT_DIR = Path(__file__).resolve().parent

CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TOKEN_FILE = PROJECT_DIR / "token.json"

DRIVE_FOLDER_NAME = "Market Intelligence Podcast"
EPISODES_FOLDER_NAME = "Episodes"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


def authenticate():
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES,
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES,
            )

            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return creds


def get_or_create_folder(
    drive_service,
    folder_name,
    parent_id=None,
):
    query_parts = [
        f"name = '{folder_name}'",
        "mimeType = 'application/vnd.google-apps.folder'",
        "trashed = false",
    ]

    if parent_id:
        query_parts.append(
            f"'{parent_id}' in parents"
        )

    query = " and ".join(query_parts)

    results = (
        drive_service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id,name)",
        )
        .execute()
    )

    folders = results.get("files", [])

    if folders:
        return folders[0]["id"]

    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }

    if parent_id:
        folder_metadata["parents"] = [parent_id]

    folder = (
        drive_service.files()
        .create(
            body=folder_metadata,
            fields="id",
        )
        .execute()
    )

    return folder["id"]


def get_episode_date(episode_path):
    match = re.search(
        r"\d{4}-\d{2}-\d{2}",
        episode_path.name,
    )

    if not match:
        raise ValueError(
            "Could not determine episode date from filename: "
            f"{episode_path.name}"
        )

    return datetime.strptime(
        match.group(),
        "%Y-%m-%d",
    ).date()


def get_week_folder_name(episode_date):
    monday = (
        episode_date
        - timedelta(days=episode_date.weekday())
    )

    return (
        f"Week of "
        f"{monday.strftime('%B')} "
        f"{monday.day}, "
        f"{monday.year}"
    )


def cleanup_old_drive_episodes():
    cutoff_date = (
        datetime.now().date()
        - timedelta(days=MP3_RETENTION_DAYS)
    )

    creds = authenticate()

    drive_service = build(
        "drive",
        "v3",
        credentials=creds,
    )

    episodes_folder_id = DRIVE_EPISODES_FOLDER_ID

    # Find weekly folders inside the Episodes folder
    weekly_results = (
        drive_service.files()
        .list(
            q=(
                f"'{episodes_folder_id}' in parents "
                "and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            ),
            spaces="drive",
            fields="files(id,name)",
        )
        .execute()
    )

    weekly_folders = weekly_results.get(
        "files",
        [],
    )

    deleted_count = 0

    for week_folder in weekly_folders:
        week_folder_id = week_folder["id"]

        file_results = (
            drive_service.files()
            .list(
                q=(
                    f"'{week_folder_id}' in parents "
                    "and trashed = false"
                ),
                spaces="drive",
                fields="files(id,name)",
            )
            .execute()
        )

        files = file_results.get("files", [])

        for file in files:
            file_name = file["name"]

            if not file_name.lower().endswith(".mp3"):
                continue

            match = re.search(
                r"\d{4}-\d{2}-\d{2}",
                file_name,
            )

            if not match:
                continue

            episode_date = datetime.strptime(
                match.group(),
                "%Y-%m-%d",
            ).date()

            if episode_date < cutoff_date:
                drive_service.files().delete(
                    fileId=file["id"]
                ).execute()

                deleted_count += 1

                print(
                    f"Deleted old Drive MP3: "
                    f"{file_name}"
                )

        # Check whether this weekly folder is now empty
        remaining_results = (
            drive_service.files()
            .list(
                q=(
                    f"'{week_folder_id}' in parents "
                    "and trashed = false"
                ),
                spaces="drive",
                fields="files(id,name)",
            )
            .execute()
        )

        remaining_files = remaining_results.get(
            "files",
            [],
        )

        if not remaining_files:
            drive_service.files().delete(
                fileId=week_folder_id
            ).execute()

            print(
                f"Deleted empty Drive folder: "
                f"{week_folder['name']}"
            )

    if deleted_count == 0:
        print("No old Drive MP3s to delete.")


def upload_episode(episode_path):
    episode_path = Path(episode_path)

    if not episode_path.exists():
        raise FileNotFoundError(
            f"Episode file not found: {episode_path}"
        )

    episode_date = get_episode_date(
        episode_path
    )

    week_folder_name = get_week_folder_name(
        episode_date
    )

    creds = authenticate()

    drive_service = build(
        "drive",
        "v3",
        credentials=creds,
    )

    # Permanent Drive folders use stable IDs.
    episodes_folder_id = DRIVE_EPISODES_FOLDER_ID

    # Weekly folder:
    # My Drive / Market Intelligence Podcast / Episodes / Week of ...
    week_folder_id = get_or_create_folder(
        drive_service,
        week_folder_name,
        parent_id=episodes_folder_id,
    )

    file_metadata = {
        "name": f"MIP_{episode_date}.mp3",
        "parents": [week_folder_id],
    }

    media = MediaFileUpload(
        str(episode_path),
        mimetype="audio/mpeg",
        resumable=True,
    )

    uploaded_file = (
        drive_service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id,name,webViewLink",
        )
        .execute()
    )

    return uploaded_file