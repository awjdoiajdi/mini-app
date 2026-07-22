import asyncio
import hashlib
import hmac
import html
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qsl, quote

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
APP_URL = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
PORT = int(os.environ.get("PORT", "10000"))
FRONTEND = Path(__file__).parent / "frontend" / "index.html"
AI_URL = "https://api.groq.com/openai/v1/chat/completions"
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def validate_init_data(init_data: str, token: str = BOT_TOKEN, max_age: int = 86_400, now: int | None = None):
    """Return the signed Telegram user or raise ValueError."""
    try:
        values = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user["id"] = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректные данные Telegram") from exc

    timestamp = int(time.time() if now is None else now)
    if auth_date > timestamp + 60 or timestamp - auth_date > max_age:
        raise ValueError("Сессия Telegram устарела")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("Подпись Telegram не прошла проверку")
    return user


def ai_messages(action: str, payload: dict) -> list[dict[str, str]]:
    prompts = {
        "brief": (
            "Сделай утренний или текущий briefing по данным пользователя. Верни JSON: "
            "{\"headline\":\"...\",\"risk\":\"...\",\"nextMove\":\"...\",\"praise\":\"...\","
            "\"tips\":[\"...\",\"...\"]}. Без воды, как личный оператор."
        ),
        "plan": (
            "Составь реалистичный план дня. Учитывай время, приоритет, энергию и длительность. "
            "Не меняй taskId. Верни JSON: {\"summary\":\"...\",\"schedule\":[{\"taskId\":\"...\","
            "\"start\":\"HH:MM\",\"reason\":\"...\"}],\"advice\":\"...\"}."
        ),
        "breakdown": (
            "Разбей одну задачу на 3-7 конкретных действий. Верни JSON: "
            "{\"summary\":\"...\",\"subtasks\":[{\"title\":\"...\",\"duration\":15}],"
            "\"firstStep\":\"...\"}. Длительность указывай в минутах."
        ),
        "capture": (
            "Преобразуй свободный текст в отдельные выполнимые задачи. Не выдумывай лишнее. "
            "Верни JSON: {\"tasks\":[{\"title\":\"...\",\"date\":\"YYYY-MM-DD\",\"time\":\"HH:MM\","
            "\"duration\":30,\"priority\":\"medium\",\"energy\":\"medium\",\"category\":\"Личное\"}],"
            "\"message\":\"...\"}. priority и energy: low, medium или high."
        ),
    }
    if action not in prompts:
        raise ValueError("Неизвестное AI-действие")
    system = (
        "Ты AI-планировщик DailyOS. Отвечай кратко на русском языке и только валидным JSON без Markdown. "
        "Сохраняй факты пользователя, не создавай несуществующие дедлайны. " + prompts[action]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def parse_ai_json(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise
        return json.loads(content[start:end + 1])


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Открыть Command Center", web_app=WebAppInfo(url=APP_URL))
    name = html.escape(message.from_user.first_name) if message.from_user else "друг"
    await message.answer(
        f"<b>DailyOS Command Center</b>\n\nПривет, {name}! Это личный оператор дня: миссии, привычки, "
        "AI-разбор мыслей, фокус-сессии и честный отчёт по прогрессу.\n\nОтправь мысль обычным сообщением или открой центр управления.",
        reply_markup=keyboard.as_markup(),
    )


@dp.message(F.text)
async def capture_message(message: types.Message):
    text = message.text.strip()[:700]
    can_transfer = len(text) <= 160
    separator = "&" if "?" in APP_URL else "?"
    url = f"{APP_URL}{separator}capture={quote(text)}" if can_transfer else APP_URL
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="Разобрать в Command Center",
        web_app=WebAppInfo(url=url),
    )
    await message.answer(
        ("Перенесу мысль в AI-разбор. Ты подтвердишь задачи перед сохранением."
         if can_transfer else
         "Сообщение длинное: открой DailyOS и вставь его в AI Inbox. Так текст не обрежется."),
        reply_markup=keyboard.as_markup(),
    )


async def index(_request: web.Request):
    return web.FileResponse(FRONTEND)


async def health(_request: web.Request):
    return web.json_response({"status": "ok", "ai": bool(GROQ_API_KEY), "provider": "groq"})


async def ai(request: web.Request):
    if not GROQ_API_KEY:
        raise web.HTTPServiceUnavailable(text="GROQ_API_KEY не настроен")
    try:
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        body = await request.json()
        action, payload = body.get("action"), body.get("payload")
        if not isinstance(action, str) or not isinstance(payload, dict):
            raise ValueError("Некорректный запрос")
        if len(json.dumps(payload, ensure_ascii=False)) > 12_000:
            raise ValueError("Слишком много данных для одного AI-запроса")
        messages = ai_messages(action, payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc

    # ponytail: in-memory limit is enough without a DB; use Redis only after horizontal scaling.
    calls = request.app["rate_limits"][user["id"]]
    minute_ago = time.monotonic() - 60
    while calls and calls[0] < minute_ago:
        calls.popleft()
    if len(calls) >= 8:
        raise web.HTTPTooManyRequests(text="Лимит: 8 AI-запросов в минуту")
    calls.append(time.monotonic())

    try:
        async with request.app["http"].post(
            AI_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
                "messages": messages,
                "temperature": 0.25,
                "max_tokens": 1400,
            },
        ) as response:
            if response.status != 200:
                if response.status == 401:
                    raise web.HTTPBadGateway(text="Groq API key недействителен")
                if response.status == 429:
                    raise web.HTTPTooManyRequests(text="Бесплатный лимит Groq исчерпан. Попробуй позже.")
                raise web.HTTPBadGateway(text=f"Groq временно недоступен ({response.status})")
            result = parse_ai_json((await response.json())["choices"][0]["message"]["content"])
            return web.json_response({"result": result})
    except asyncio.TimeoutError as exc:
        raise web.HTTPGatewayTimeout(text="Groq не ответил вовремя") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadGateway(text="Groq вернул некорректный ответ") from exc


async def startup(app: web.Application):
    app["http"] = ClientSession(timeout=ClientTimeout(total=35))
    await bot.set_webhook(
        f"{APP_URL.rstrip('/')}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )


async def cleanup(app: web.Application):
    await app["http"].close()
    await bot.session.close()


def main():
    app = web.Application(client_max_size=32 * 1024)
    app["rate_limits"] = defaultdict(deque)
    app.add_routes([web.get("/", index), web.get("/health", health), web.post("/api/ai", ai)])
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
