"""
Диагностика: что реально видит бот в листе «Позиции» после парсинга.
Запуск из корня проекта: python debug_items.py
"""
import asyncio
import base64
import json

from dotenv import dotenv_values

from services.sheets import SheetsClient, items_for_merchant

env = dotenv_values(".env")


async def main() -> None:
    service_account_info = json.loads(base64.b64decode(env["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    client = SheetsClient.from_service_account_info(
        service_account_info=service_account_info,
        menu_spreadsheet_id=env["SHEETS_MENU_ID"],
        orders_spreadsheet_id=env["SHEETS_ORDERS_ID"],
    )

    items = await client.get_items(force_refresh=True)
    print(f"Всего строк распознано как позиции: {len(items)}\n")

    for i in items:
        print(f"  item_id={i.item_id!r}")
        print(f"    merchant_id={i.merchant_id!r}")
        print(f"    name={i.name!r}")
        print(f"    price_try={i.price_try!r}")
        print(f"    photo_url={i.photo_url!r}")
        print(f"    is_active={i.is_active!r}")
        print(f"    is_available={i.is_available!r}")
        print()

    matched = items_for_merchant(items, "rest_001")
    print(f"Позиции для merchant_id='rest_001': {len(matched)}")

    if not matched:
        print("\n-> Смотрите на значения выше:")
        print("   - merchant_id в 'Позиции' должен быть ТОЧНО 'rest_001'")
        print("     (без пробелов, той же регистрозависимости)")
        print("   - is_active должен быть True")


asyncio.run(main())
