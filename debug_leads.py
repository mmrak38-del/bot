"""
Диагностика: смотрим, что реально возвращает CRM на GET /leads,
чтобы понять точный формат ответа (список? обёртка? как называется
поле с ID? как называется поле с датой создания?).

Запуск:
    python3 debug_leads.py

Использует те же переменные, что и основной бот (BOT_TOKEN не нужен,
только CRM_BASE_URL и CRM_API_TOKEN — либо из .env, либо возьмутся
дефолты из cleanfloor_bot.py).
"""
import asyncio
import json
import os

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CRM_BASE_URL = os.getenv("CRM_BASE_URL", "https://nexus-crm-production-a018.up.railway.app")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "cfcrm_xpEwkrOaqr5iXQgDh5yS60UXHq5s8X4V")


async def main():
    headers = {"Content-Type": "application/json"}
    if CRM_API_TOKEN:
        headers["Authorization"] = f"Bearer {CRM_API_TOKEN}"

    url = f"{CRM_BASE_URL.rstrip('/')}/leads"
    print(f"GET {url}")
    print(f"headers: {headers}\n")

    async with aiohttp.ClientSession() as session:
        # 1) без параметров — как отдаёт по умолчанию
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            print(f"--- Без параметров --- status={resp.status}")
            text = await resp.text()
            print(text[:3000])
            print()

        # 2) с limit/sort — как пробует бот сейчас
        async with session.get(
            url,
            params={"limit": 5, "sort": "-created_at"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            print(f"--- С limit=5&sort=-created_at --- status={resp.status}")
            text = await resp.text()
            print(text[:3000])


if __name__ == "__main__":
    asyncio.run(main())
