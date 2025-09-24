import os
import json
from flask import Flask, request, jsonify
import openai
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)

# ?? OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# ?? Google API
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

# Google Sheets
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

# Google Drive
drive_service = build("drive", "v3", credentials=creds)
TO_ANALYZE_FOLDER = os.getenv("TO_ANALYZE_FOLDER_ID")
ANALYZED_FOLDER = os.getenv("ANALYZED_FOLDER_ID")


def analyze_image(image_url: str) -> dict:
    """������ ���� � OpenAI Vision � JSON-�������"""
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "�� ������� �� ��������� ������������ �������. "
                    "�������� ���������� �����, �������� ������, ������� � ��������. "
                    "������� ������ � ������� JSON � �������: "
                    "{'part_number': str, 'name': str, 'machine': str, 'description': str}."
                )
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "��������� ������ �� ����:"},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ]
    )

    text = response["choices"][0]["message"]["content"].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {
            "part_number": "UNKNOWN",
            "name": "�� ����������",
            "machine": "",
            "description": text
        }
    return data


def move_file(file_id: str, to_folder: str):
    """���������� ���� � ������ ����� Google Drive"""
    file = drive_service.files().get(fileId=file_id, fields="parents").execute()
    prev_parents = ",".join(file.get("parents"))

    drive_service.files().update(
        fileId=file_id,
        addParents=to_folder,
        removeParents=prev_parents,
        fields="id, parents"
    ).execute()


@app.route("/analyze", methods=["POST"])
def analyze():
    results = []
    query = f"'{TO_ANALYZE_FOLDER}' in parents and mimeType contains 'image/'"
    files = drive_service.files().list(q=query, fields="files(id, name, webViewLink)").execute().get("files", [])

    if not files:
        return jsonify({"result": "��� ���� ��� �������"})

    for file in files:
        file_id = file["id"]
        name = file["name"]
        url = f"https://drive.google.com/uc?id={file_id}"

        # 1. ����������� ����
        data = analyze_image(url)

        # 2. ���������� ���� � ����� analyzed
        move_file(file_id, ANALYZED_FOLDER)

        # 3. ���������� � Google Sheets
        sheet.append_row([
            data.get("part_number"),
            data.get("name"),
            data.get("machine"),
            data.get("description"),
            file["webViewLink"]
        ])

        results.append(
            f"{name} > {data.get('part_number')} | {data.get('name')} | {data.get('machine')}"
        )

    return jsonify({
        "result": f"? ���������� {len(results)} ����",
        "details": results
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
