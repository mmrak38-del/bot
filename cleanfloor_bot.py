"""
Telegram-бот CleanFloor (Светлогорск) — один файл.

Функционал:
    /start -> приветствие + 3 инлайн-кнопки:
        - "Заказать уборку" -> подробная анкета (объект, вид уборки, площадь,
          когда нужна уборка, адрес/район, телефон, имя и фамилия) -> лид в CRM
        - "Заказать звонок" -> имя, фамилия, телефон -> лид в CRM
        - "Связаться с менеджером" -> свободный вопрос -> уходит менеджеру
          напрямую (в CRM НЕ пишется), менеджер отвечает Reply-сообщением,
          ответ прилетает клиенту как "Ответ менеджера: ..."

Установка зависимостей:
    pip install aiogram aiohttp python-dotenv --break-system-packages

Перед запуском заполните блок НАСТРОЙКИ ниже (или создайте .env рядом
с этим файлом — значения из .env подставятся автоматически).
"""

import asyncio
import json
import logging
import os
import re

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================== НАСТРОЙКИ ==================================

# Токен вашего Telegram-бота (получить у @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8164490986:AAHZZKdkYr58TkLZS34shZpU7II5tWpxHe4")

# Telegram ID менеджера — сюда придут все заявки и вопросы клиентов.
# Узнать свой ID можно у бота @userinfobot. Перед запуском менеджер должен
# сам написать вашему боту /start, иначе бот не сможет написать ему первым.
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "1116173212"))

# Адрес вашей CRM
CRM_BASE_URL = os.getenv("CRM_BASE_URL", "https://nexus-crm-production-a018.up.railway.app")

# API-токен CRM: CRM -> Интеграции -> "API-токен для сайта"
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "cfcrm_xpEwkrOaqr5iXQgDh5yS60UXHq5s8X4V")

# ID воронки в CRM, куда должны падать заявки из бота.
# У вас в CRM всего одна (основная) воронка — по умолчанию у таких CRM
# она имеет id = 1 (это же значение указано в примере виджета в вашей CRM:
# data-pipeline="1"). Если позже создадите вторую воронку и заявки должны
# падать в неё — посмотрите её ID в разделе "Воронки" (например через
# вкладку Network в браузере при создании лида вручную) и поменяйте число.
CRM_PIPELINE_ID = int(os.getenv("CRM_PIPELINE_ID", "1"))

# Как часто (в секундах) бот опрашивает CRM в поисках новых заявок,
# пришедших НЕ из бота (например с сайта). Это гарантирует, что менеджер
# получит уведомление о любой заявке в CRM, независимо от источника.
CRM_POLL_INTERVAL = int(os.getenv("CRM_POLL_INTERVAL", "10"))

# Файл, в котором бот запоминает ID уже обработанных лидов CRM, чтобы
# при перезапуске не разослать уведомления повторно и не пропустить новые.
SEEN_LEADS_FILE = os.getenv("SEEN_LEADS_FILE", "seen_leads.json")

# =============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

SOURCE_CLEANING = "Telegram-бот — Заказать уборку"
SOURCE_CALL = "Telegram-бот — Заказать звонок"

# message_id уведомления у менеджера -> chat_id клиента, задавшего вопрос
pending_questions: dict[int, int] = {}


# ============================== CRM-КЛИЕНТ ==================================

class CRMClient:
    """
    Обёртка над REST API cleanfloorCRM.

    По документации на главной странице CRM создание лида выглядит так:

        POST {CRM_BASE_URL}/leads
        {
            "name": "Иван Иванов",
            "phone": "+79991234567",
            "pipeline_id": 1,
            "client_type": "физ",
            "source": "Заказать уборку",
            "comment": "Объект: Квартира; Вид уборки: Генеральная; ..."
        }

    Токен передаётся как Bearer в заголовке Authorization — самый частый
    вариант для таких CRM. Если после теста лиды не будут появляться в CRM
    (в логах бота будет видна ошибка), откройте в браузере
    {CRM_BASE_URL}/docs — там Swagger с точным форматом авторизации,
    и нужно будет поправить метод _headers() ниже.
    """

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def create_lead(
        self,
        name: str,
        phone: str,
        source: str,
        pipeline_id: int = 1,
        telegram: str | None = None,
        client_type: str = "физ",
        comment: str | None = None,
    ) -> tuple[bool, int | None]:
        """Создаёт лид в CRM.

        Возвращает (успех, id_лида). id_лида используется, чтобы сразу
        пометить лид как "уже уведомили" и не продублировать уведомление
        фоновым опросом CRM (см. poll_new_leads ниже).
        """
        url = f"{self.base_url}/leads"
        payload = {
            "name": name,
            "phone": phone,
            "pipeline_id": pipeline_id,
            "client_type": client_type,
            "source": source,
        }
        if telegram:
            payload["telegram"] = telegram
        if comment:
            payload["comment"] = comment

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        lead_id = None
                        try:
                            data = await resp.json()
                            if isinstance(data, dict):
                                inner = data.get("lead") if isinstance(data.get("lead"), dict) else data
                                lead_id = CRMClient.extract_lead_id(inner)
                        except Exception:
                            pass
                        return True, lead_id
                    body = await resp.text()
                    logger.error("Не удалось создать лид в CRM: %s %s", resp.status, body)
                    return False, None
        except Exception:
            logger.exception("Ошибка запроса к CRM")
            return False, None

    async def get_recent_leads(self, limit: int = 50) -> list[dict]:
        """Забирает список последних лидов из CRM (для фонового опроса).

        Т.к. точный формат этого эндпоинта не задокументирован, пробуем
        по очереди несколько распространённых вариантов запроса (без
        параметров / с limit / с пагинацией) и несколько вариантов формы
        ответа (голый список / обёрнутый в items-data-results-leads).
        Первый успешный (status 200 + удалось найти список) — используем.
        """
        url = f"{self.base_url}/leads"
        attempts = [
            {},
            {"limit": limit},
            {"limit": limit, "sort": "-created_at"},
            {"per_page": limit},
            {"page_size": limit},
        ]

        for params in attempts:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        params=params or None,
                        headers=self._headers(),
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        try:
                            data = await resp.json()
                        except Exception:
                            continue

                        leads = None
                        if isinstance(data, list):
                            leads = data
                        elif isinstance(data, dict):
                            for key in ("items", "data", "results", "leads"):
                                if isinstance(data.get(key), list):
                                    leads = data[key]
                                    break

                        if leads is not None:
                            if not CRMClient._logged_shape:
                                logger.info(
                                    "CRM GET /leads (params=%s) вернул %d записей. Пример первой записи: %s",
                                    params,
                                    len(leads),
                                    json.dumps(leads[0], ensure_ascii=False) if leads else "пусто",
                                )
                                CRMClient._logged_shape = True
                            return leads
            except Exception:
                logger.exception("Ошибка запроса списка лидов к CRM (params=%s)", params)
                continue

        logger.error(
            "Не удалось получить список лидов ни одним из известных способов. "
            "Откройте %s/docs и проверьте формат ответа GET /leads вручную.",
            self.base_url,
        )
        return []

    _logged_shape = False

    @staticmethod
    def extract_lead_id(lead: dict):
        for key in ("id", "_id", "lead_id", "leadId", "uuid"):
            if key in lead and lead[key] is not None:
                return lead[key]
        return None


crm = CRMClient(CRM_BASE_URL, CRM_API_TOKEN)

# ID лидов CRM, о которых менеджер уже уведомлён (заявки из бота попадают
# сюда сразу при создании; заявки с сайта/других источников — фоновым
# опросом poll_new_leads). Персистится в SEEN_LEADS_FILE, чтобы пережить
# перезапуск бота без повторных/пропущенных уведомлений.
notified_lead_ids: set[int] = set()


def _load_seen_leads() -> None:
    try:
        if os.path.exists(SEEN_LEADS_FILE):
            with open(SEEN_LEADS_FILE, "r", encoding="utf-8") as f:
                notified_lead_ids.update(json.load(f))
    except Exception:
        logger.exception("Не удалось загрузить %s", SEEN_LEADS_FILE)


def _save_seen_leads() -> None:
    try:
        with open(SEEN_LEADS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(notified_lead_ids), f)
    except Exception:
        logger.exception("Не удалось сохранить %s", SEEN_LEADS_FILE)


def _lead_notification_text(lead: dict) -> str:
    name = lead.get("name") or "—"
    phone = lead.get("phone") or "—"
    source = lead.get("source") or "Не указан (скорее всего сайт)"
    comment = lead.get("comment") or ""
    client_type = lead.get("client_type")
    client_type_label = "Юридическое лицо" if client_type == "юр" else "Физическое лицо" if client_type == "физ" else None

    lines = [
        "🆕 Пришла новая заявка в CRM!",
        f"Источник: {source}",
        f"Имя: {name}",
        f"Телефон: {phone}",
    ]
    if client_type_label:
        lines.append(f"Тип клиента: {client_type_label}")
    if comment:
        lines.append(comment)
    return "\n".join(lines)


async def poll_new_leads(bot: Bot) -> None:
    """Фоновая задача: раз в CRM_POLL_INTERVAL секунд проверяет CRM на
    новые заявки и шлёт уведомление менеджеру о ЛЮБОЙ заявке, которую
    бот ещё не показывал — независимо от того, откуда она пришла
    (сайт, бот, ручное добавление в CRM и т.д.).

    Заявки, созданные самим ботом, уже уведомляются мгновенно в момент
    создания (см. cleaning_name/_finish_call_order) и их id сразу
    добавляется в notified_lead_ids — так что здесь они не дублируются.
    """
    if not MANAGER_CHAT_ID:
        return

    _load_seen_leads()
    first_run = not notified_lead_ids

    while True:
        try:
            leads = await crm.get_recent_leads(limit=50)
            new_leads = []
            for lead in leads:
                lead_id = CRMClient.extract_lead_id(lead)
                if lead_id is None or lead_id in notified_lead_ids:
                    continue
                new_leads.append((lead_id, lead))

            if first_run:
                # При первом запуске не спамим менеджера историей — просто
                # запоминаем всё, что уже есть в CRM, и уведомляем только
                # о том, что появится после.
                for lead_id, _ in new_leads:
                    notified_lead_ids.add(lead_id)
                _save_seen_leads()
                first_run = False
                logger.info("Фоновый опрос CRM запущен, база: %d лидов.", len(notified_lead_ids))
            else:
                # Уведомляем в хронологическом порядке (старые -> новые).
                for lead_id, lead in reversed(new_leads):
                    try:
                        await bot.send_message(MANAGER_CHAT_ID, _lead_notification_text(lead))
                    except Exception:
                        logger.exception("Не удалось отправить уведомление менеджеру о лиде %s", lead_id)
                        continue
                    notified_lead_ids.add(lead_id)
                if new_leads:
                    _save_seen_leads()
        except Exception:
            logger.exception("Ошибка в фоновом опросе CRM на новые заявки")

        await asyncio.sleep(CRM_POLL_INTERVAL)


# ============================== СОСТОЯНИЯ (FSM) =============================

class CleaningOrder(StatesGroup):
    object_type = State()
    cleaning_type = State()
    cleaning_type_custom = State()
    area = State()
    when = State()
    address = State()
    phone = State()
    name = State()


class CallOrder(StatesGroup):
    name = State()
    surname = State()
    phone = State()


class QuestionForm(StatesGroup):
    question = State()


# ============================== КЛАВИАТУРЫ ==================================

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧹 Заказать уборку", callback_data="menu:cleaning")],
            [InlineKeyboardButton(text="📞 Заказать звонок", callback_data="menu:call")],
            [InlineKeyboardButton(text="💬 Связаться с менеджером", callback_data="menu:manager")],
        ]
    )


OBJECT_TYPES = {
    "obj:flat": "Квартира",
    "obj:house": "Дом",
    "obj:office": "Офис",
    "obj:shop": "Магазин",
    "obj:industrial": "Производственное помещение",
}

# Квартира/дом — считаем частным лицом. Офис/магазин/производственное
# помещение — автоматически проставляем в CRM "юридическое лицо".
OBJECT_TO_CLIENT_TYPE = {
    "obj:flat": "физ",
    "obj:house": "физ",
    "obj:office": "юр",
    "obj:shop": "юр",
    "obj:industrial": "юр",
}

CLEANING_TYPES = {
    "clean:support": "Поддерживающая уборка",
    "clean:general": "Генеральная уборка",
    "clean:after_repair": "После ремонта",
    "clean:windows": "Мытье окон",
    "clean:furniture": "Химчистка мебели",
    "clean:other": "Другое",
}

AREAS = {
    "area:lt50": "До 50 м²",
    "area:50_100": "50–100 м²",
    "area:100_200": "100–200 м²",
    "area:gt200": "Более 200 м²",
}

WHEN_OPTIONS = {
    "when:today": "Сегодня",
    "when:tomorrow": "Завтра",
    "when:week": "На этой неделе",
}


def kb_from_dict(options: dict, columns: int = 1) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=text, callback_data=key) for key, text in options.items()]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def is_valid_phone(text: str) -> bool:
    """Строгая проверка под конкретные форматы номеров (Беларусь/Россия):
      - местный:            8XXXXXXXXXX      (11 цифр, начинается с 8)
      - международный BY:   +375XXXXXXXXX    (375 + 9 цифр)
      - международный RU:   +7XXXXXXXXXX     (7 + 10 цифр)
    Плюс и любые пробелы/скобки/дефисы не важны. Благодаря точному
    количеству цифр в каждом формате лишняя или недостающая цифра
    (например, "802958892731" вместо "80295889273") больше не пройдёт."""
    cleaned = re.sub(r"[^\d+]", "", text)
    return bool(re.fullmatch(r"(\+?375\d{9}|\+?7\d{10}|8\d{10})", cleaned))


# ============================== СТАРТ ========================================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! Мы профессионально выполняем уборку квартир, домов, офисов "
        "и коммерческих помещений в Светлогорске.\n\n"
        "✅ Работаем без выходных\n"
        "✅ Используем профессиональную химию и оборудование\n"
        "✅ Гарантируем качество уборки\n\n"
        "Выберите, что вас интересует:",
        reply_markup=main_menu_kb(),
    )


# ======================= ВЕТКА "ЗАКАЗАТЬ УБОРКУ" =============================

@router.callback_query(F.data == "menu:cleaning")
async def cleaning_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CleaningOrder.object_type)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Какой объект необходимо убрать?",
        reply_markup=kb_from_dict(OBJECT_TYPES, columns=1),
    )
    await callback.answer()


@router.callback_query(CleaningOrder.object_type, F.data.startswith("obj:"))
async def cleaning_object(callback: CallbackQuery, state: FSMContext):
    await state.update_data(
        object_type=OBJECT_TYPES[callback.data],
        client_type=OBJECT_TO_CLIENT_TYPE[callback.data],
    )
    await state.set_state(CleaningOrder.cleaning_type)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Какой вид уборки нужен?",
        reply_markup=kb_from_dict(CLEANING_TYPES, columns=1),
    )
    await callback.answer()


@router.callback_query(CleaningOrder.cleaning_type, F.data.startswith("clean:"))
async def cleaning_type_chosen(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)

    if callback.data == "clean:other":
        await state.set_state(CleaningOrder.cleaning_type_custom)
        await callback.message.answer("Опишите, какой вид уборки вам нужен:")
        await callback.answer()
        return

    await state.update_data(cleaning_type=CLEANING_TYPES[callback.data])
    await state.set_state(CleaningOrder.area)
    await callback.message.answer(
        "Какая площадь помещения?",
        reply_markup=kb_from_dict(AREAS, columns=1),
    )
    await callback.answer()


@router.message(CleaningOrder.cleaning_type_custom)
async def cleaning_type_custom_input(message: Message, state: FSMContext):
    await state.update_data(cleaning_type=message.text.strip())
    await state.set_state(CleaningOrder.area)
    await message.answer(
        "Какая площадь помещения?",
        reply_markup=kb_from_dict(AREAS, columns=1),
    )


@router.callback_query(CleaningOrder.area, F.data.startswith("area:"))
async def cleaning_area(callback: CallbackQuery, state: FSMContext):
    await state.update_data(area=AREAS[callback.data])
    await state.set_state(CleaningOrder.when)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Когда нужна уборка?",
        reply_markup=kb_from_dict(WHEN_OPTIONS, columns=1),
    )
    await callback.answer()


@router.callback_query(CleaningOrder.when, F.data.startswith("when:"))
async def cleaning_when(callback: CallbackQuery, state: FSMContext):
    await state.update_data(when=WHEN_OPTIONS[callback.data])
    await state.set_state(CleaningOrder.address)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Введите адрес или район:")
    await callback.answer()


@router.message(CleaningOrder.address)
async def cleaning_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await state.set_state(CleaningOrder.phone)
    await message.answer(
        "Оставьте номер телефона (или отправьте его кнопкой ниже):",
        reply_markup=phone_request_kb(),
    )


@router.message(CleaningOrder.phone, F.contact)
async def cleaning_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(CleaningOrder.name)
    await message.answer("Оставьте имя и фамилию:", reply_markup=ReplyKeyboardRemove())


@router.message(CleaningOrder.phone, F.text)
async def cleaning_phone_text(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not is_valid_phone(phone):
        await message.answer(
            "Похоже, это не номер телефона. Введите номер полностью, "
            "например: +375291234567, или отправьте его кнопкой ниже.",
            reply_markup=phone_request_kb(),
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(CleaningOrder.name)
    await message.answer("Оставьте имя и фамилию:", reply_markup=ReplyKeyboardRemove())


@router.message(CleaningOrder.name)
async def cleaning_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    full_name = message.text.strip()
    username = f"@{message.from_user.username}" if message.from_user.username else None

    client_type = data.get("client_type", "физ")

    comment = (
        f"Объект: {data.get('object_type')}; "
        f"Вид уборки: {data.get('cleaning_type')}; "
        f"Площадь: {data.get('area')}; "
        f"Когда: {data.get('when')}; "
        f"Адрес/район: {data.get('address')}"
    )

    ok, lead_id = await crm.create_lead(
        name=full_name,
        phone=data.get("phone", ""),
        source=SOURCE_CLEANING,
        pipeline_id=CRM_PIPELINE_ID,
        telegram=username,
        client_type=client_type,
        comment=comment,
    )
    if lead_id is not None:
        notified_lead_ids.add(lead_id)

    await message.answer(
        "Спасибо! Ваша заявка принята. В ближайшее время с вами свяжется менеджер."
    )
    await message.answer("Чтобы вернуться в меню — введите /start")

    if MANAGER_CHAT_ID:
        crm_note = "" if ok else "\n⚠️ Не удалось автоматически создать лид в CRM, добавьте вручную."
        client_type_label = "Юридическое лицо" if client_type == "юр" else "Физическое лицо"
        await bot.send_message(
            MANAGER_CHAT_ID,
            "🆕 Пришла новая заявка в CRM!\n"
            f"Источник: {SOURCE_CLEANING}\n"
            f"Имя: {full_name}\n"
            f"Телефон: {data.get('phone', '')}\n"
            f"Тип клиента: {client_type_label}\n"
            f"{comment}"
            f"{crm_note}",
        )

    await state.clear()


# ======================= ВЕТКА "ЗАКАЗАТЬ ЗВОНОК" =============================

@router.callback_query(F.data == "menu:call")
async def call_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CallOrder.name)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Введите ваше имя:")
    await callback.answer()


@router.message(CallOrder.name)
async def call_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CallOrder.surname)
    await message.answer("Введите вашу фамилию:")


@router.message(CallOrder.surname)
async def call_surname(message: Message, state: FSMContext):
    await state.update_data(surname=message.text.strip())
    await state.set_state(CallOrder.phone)
    await message.answer(
        "Введите ваш номер телефона (или отправьте его кнопкой ниже):",
        reply_markup=phone_request_kb(),
    )


async def _finish_call_order(message: Message, state: FSMContext, bot: Bot, phone: str):
    data = await state.get_data()
    full_name = f"{data.get('name', '')} {data.get('surname', '')}".strip()
    username = f"@{message.from_user.username}" if message.from_user.username else None

    ok, lead_id = await crm.create_lead(
        name=full_name,
        phone=phone,
        source=SOURCE_CALL,
        pipeline_id=CRM_PIPELINE_ID,
        telegram=username,
    )
    if lead_id is not None:
        notified_lead_ids.add(lead_id)

    await message.answer(
        "Спасибо! Ваша заявка принята. В ближайшее время с вами свяжется менеджер.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Чтобы вернуться в меню — введите /start")

    if MANAGER_CHAT_ID:
        crm_note = "" if ok else "\n⚠️ Не удалось автоматически создать лид в CRM, добавьте вручную."
        await bot.send_message(
            MANAGER_CHAT_ID,
            "🆕 Пришла новая заявка в CRM!\n"
            f"Источник: {SOURCE_CALL}\n"
            f"Имя: {full_name}\n"
            f"Телефон: {phone}"
            f"{crm_note}",
        )

    await state.clear()


@router.message(CallOrder.phone, F.contact)
async def call_phone_contact(message: Message, state: FSMContext, bot: Bot):
    await _finish_call_order(message, state, bot, phone=message.contact.phone_number)


@router.message(CallOrder.phone, F.text)
async def call_phone_text(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip()
    if not is_valid_phone(phone):
        await message.answer(
            "Похоже, это не номер телефона. Введите номер полностью, "
            "например: +375291234567, или отправьте его кнопкой ниже.",
            reply_markup=phone_request_kb(),
        )
        return
    await _finish_call_order(message, state, bot, phone=phone)


# ======================= ВЕТКА "СВЯЗАТЬСЯ С МЕНЕДЖЕРОМ" ======================

@router.callback_query(F.data == "menu:manager")
async def question_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(QuestionForm.question)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Напишите ваш вопрос а мы ответим на него как можно быстрее.")
    await callback.answer()


@router.message(QuestionForm.question)
async def question_received(message: Message, state: FSMContext, bot: Bot):
    await state.clear()

    if MANAGER_CHAT_ID:
        client_name = message.from_user.full_name
        username = f"@{message.from_user.username}" if message.from_user.username else "без username"

        sent = await bot.send_message(
            MANAGER_CHAT_ID,
            "❗️Новая заявка!\n"
            f"От: {client_name} ({username})\n\n"
            f"Ответьте на вопрос клиента:\n{message.text}\n\n"
            "👉 Чтобы ответить клиенту — ответьте (Reply) на это сообщение.",
        )
        pending_questions[sent.message_id] = message.chat.id

    await message.answer("Спасибо за ваш вопрос! Мы ответим вам как можно скорее.")


@router.message(F.chat.id == MANAGER_CHAT_ID, F.reply_to_message)
async def manager_reply(message: Message, bot: Bot):
    original_id = message.reply_to_message.message_id
    client_chat_id = pending_questions.get(original_id)

    if not client_chat_id:
        return  # менеджер ответил не на сообщение с вопросом клиента

    await bot.send_message(client_chat_id, f"Ответ менеджера: {message.text}")
    await message.reply("✅ Ответ отправлен клиенту.")
    pending_questions.pop(original_id, None)


# ============================== ЗАПУСК =======================================

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВАШ_ТОКЕН_БОТА":
        raise RuntimeError("Укажите BOT_TOKEN в блоке НАСТРОЙКИ или в файле .env")
    if not MANAGER_CHAT_ID:
        logger.warning("MANAGER_CHAT_ID не задан — уведомления менеджеру отправляться не будут.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)

    # Фоновая задача: следит за CRM и уведомляет менеджера о ЛЮБОЙ новой
    # заявке, откуда бы она ни пришла (сайт, бот, ручное добавление).
    poll_task = asyncio.create_task(poll_new_leads(bot))
    try:
        await dp.start_polling(bot)
    finally:
        poll_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
