"""
Диагностика: что реально видит бот в листе «Мерчанты» после парсинга.

Запуск из корня проекта (там же, где .env):
    python debug_merchants.py
"""

import asyncio
import base64
import json

from dotenv import dotenv_values

from services.sheets import SheetsClient, get_available_categories

env = dotenv_values(".env")


async def main() -> None:
    service_account_info = json.loads(base64.b64decode(env["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    client = SheetsClient.from_service_account_info(
        service_account_info=service_account_info,
        menu_spreadsheet_id=env["SHEETS_MENU_ID"],
        orders_spreadsheet_id=env["SHEETS_ORDERS_ID"],
    )

    merchants = await client.get_merchants(force_refresh=True)
    print(f"Всего строк распознано как мерчанты: {len(merchants)}\n")

    for m in merchants:
        print(f"  merchant_id={m.merchant_id!r}")
        print(f"    category={m.category!r}")
        print(f"    name={m.name!r}")
        print(f"    is_active={m.is_active!r}")
        print(f"    today_confirmed={m.today_confirmed!r}")
        print(f"    is_visible (оба TRUE)={m.is_visible!r}")
        print()

    codes = get_available_categories(merchants)
    print(f"Доступные категории (после фильтрации): {codes}")

    if not codes:
        print("\n-> Категорий нет. Смотрите на значения выше:")
        print("   - is_active / today_confirmed должны быть True (не False)")
        print("   - category должен быть ОДНИМ ИЗ английских кодов:")
        print("     restaurant, grocery, pharmacy, vet, water, hardware")
        print("     (не русское название типа 'Рестораны')")


asyncio.run(main())
