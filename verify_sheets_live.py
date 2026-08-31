"""
Реальная проверка доступности Google Sheets по вашему .env.
Требует: pip install google-api-python-client google-auth (уже в requirements.txt)
Запуск из корня проекта: python verify_sheets_live.py
"""
import base64, json
from dotenv import dotenv_values
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

env = dotenv_values(".env")

info = json.loads(base64.b64decode(env["GOOGLE_SERVICE_ACCOUNT_JSON"]))
creds = Credentials.from_service_account_info(
    info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
service = build("sheets", "v4", credentials=creds, cache_discovery=False)

for label, sheet_id in [("Меню", env["SHEETS_MENU_ID"]), ("Заказы", env["SHEETS_ORDERS_ID"])]:
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=sheet_id, fields="properties.title"
        ).execute()
        title = meta["properties"]["title"]
        print(f'OK: документ "{label}" доступен, реальное название: "{title}"')
    except Exception as e:
        print(f'FAIL: документ "{label}" (id={sheet_id}) недоступен: {e}')
