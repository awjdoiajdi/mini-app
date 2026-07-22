import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, FormData, web
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
AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
CAPTURE_TTL = 15 * 60
CAPTURES: dict[str, tuple[float, int, str]] = {}

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
        "review": (
            "Коротко оцени день: что получилось, что мешало, что перенести на завтра. "
            "Верни JSON: {\"summary\":\"...\",\"wins\":[\"...\"],\"risks\":[\"...\"],"
            "\"tomorrow\":[\"...\"]}. Без мотивационной воды."
        ),
        "chat": (
            "Ответь как личный AI-помощник по задачам и дню пользователя. Видишь только переданный контекст. "
            "Если пользователь просит создать задачи или в сообщении есть явные дела, предложи их в tasks. "
            "Если режим rescue, предложи plan с taskId и date/time/delete/done только для переданных задач. "
            "Если речь про еду, верни один recipe. "
            "Верни JSON: {\"reply\":\"...\",\"tasks\":[{\"title\":\"...\",\"date\":\"YYYY-MM-DD\","
            "\"time\":\"HH:MM\",\"duration\":30,\"priority\":\"medium\",\"energy\":\"medium\","
            "\"category\":\"Личное\"}],\"recipe\":null,\"plan\":[]}. priority и energy: low, medium или high. Без воды."
        ),
        "recipe": (
            "Предложи 3 блюда из продуктов пользователя. Учитывай простоту, обычную домашнюю кухню и что можно докупить. "
            "Верни JSON: {\"message\":\"...\",\"recipes\":[{\"title\":\"...\",\"time\":\"25 мин\","
            "\"difficulty\":\"легко\",\"calories\":\"примерно 500 ккал\",\"ingredients\":[\"...\"],"
            "\"shopping\":[\"...\"],\"steps\":[\"...\"]}]}. Не выдумывай дорогие продукты без нужды."
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


def cleanup_captures(now: float | None = None):
    now = time.monotonic() if now is None else now
    for capture_id, (expires, _, _) in list(CAPTURES.items()):
        if expires <= now:
            CAPTURES.pop(capture_id, None)


def remember_capture(text: str, user_id: int, now: float | None = None) -> str:
    cleanup_captures(now)
    capture_id = secrets.token_urlsafe(12)
    # ponytail: in-memory handoff keeps bot-to-app private without a DB; use Redis after multi-instance scaling.
    CAPTURES[capture_id] = ((time.monotonic() if now is None else now) + CAPTURE_TTL, user_id, text[:4000])
    return capture_id


def pop_capture(capture_id: str, user_id: int, now: float | None = None) -> str | None:
    cleanup_captures(now)
    item = CAPTURES.get(capture_id)
    if not item or item[1] != user_id:
        return None
    CAPTURES.pop(capture_id, None)
    return item[2]


def capture_url(text: str, user_id: int) -> str:
    separator = "&" if "?" in APP_URL else "?"
    return f"{APP_URL}{separator}capture_id={remember_capture(text, user_id)}"


def groq_audio_models() -> list[str]:
    configured = os.environ.get("GROQ_AUDIO_MODEL", "whisper-large-v3-turbo").strip()
    return list(dict.fromkeys(model for model in (configured, "whisper-large-v3-turbo", "whisper-large-v3") if model))


def groq_error(text: str) -> str:
    try:
        data = json.loads(text)
        text = data.get("error", {}).get("message") or data.get("message") or text
    except json.JSONDecodeError:
        pass
    return " ".join(str(text).split())[:240]


async def groq_transcribe_voice(data: bytes) -> str:
    last_error = ""
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        for model in groq_audio_models():
            form = FormData()
            form.add_field("model", model)
            form.add_field("response_format", "json")
            form.add_field("file", data, filename="voice.ogg", content_type="audio/ogg")
            async with session.post(AUDIO_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, data=form) as response:
                if response.status == 200:
                    text = (await response.json()).get("text", "").strip()
                    if text:
                        return text
                    raise RuntimeError("Groq не услышал речь в голосовом.")
                last_error = f"{response.status}: {groq_error(await response.text())}"
    raise RuntimeError(f"Groq не распознал голос. {last_error}")


async def groq_read_image(data: bytes) -> str:
    image = base64.b64encode(data).decode()
    payload = {
        "model": os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        "temperature": 0.1,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": "Извлеки из скриншота или фото только реальные задачи, даты, время и важный контекст. Отвечай кратким русским текстом."},
            {"role": "user", "content": [
                {"type": "text", "text": "Найди задачи на изображении. Если задач нет, скажи это."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]},
        ],
    }
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        async with session.post(AI_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload) as response:
            if response.status != 200:
                raise RuntimeError(f"Groq не прочитал скриншот ({response.status})")
            return (await response.json())["choices"][0]["message"]["content"].strip()


async def groq_fridge_photo(data: bytes):
    image = base64.b64encode(data).decode()
    payload = {
        "model": os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        "temperature": 0.2,
        "max_tokens": 1400,
        "messages": [
            {"role": "system", "content": ai_messages("recipe", {})[0]["content"]},
            {"role": "user", "content": [
                {"type": "text", "text": "Определи продукты на фото и предложи блюда."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]},
        ],
    }
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        async with session.post(AI_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload) as response:
            if response.status != 200:
                raise RuntimeError(f"Groq не разобрал продукты ({response.status})")
            return parse_ai_json((await response.json())["choices"][0]["message"]["content"])


async def answer_capture(message: types.Message, text: str, title: str):
    if not text.strip():
        await message.answer("Не вижу текста для разбора. Отправь сообщение, голос или скриншот с задачами.")
        return
    if not message.from_user:
        await message.answer("Не вижу Telegram-пользователя для приватной передачи в Mini App.")
        return
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Разобрать в DailyOS", web_app=WebAppInfo(url=capture_url(text, message.from_user.id)))
    await message.answer(title, reply_markup=keyboard.as_markup())


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Открыть DailyOS", web_app=WebAppInfo(url=APP_URL))
    name = html.escape(message.from_user.first_name) if message.from_user else "друг"
    await message.answer(
        f"<b>DailyOS</b>\n\nПривет, {name}! Отправь сюда текст, пересланное сообщение, голос или скриншот. "
        "Я перенесу это в AI-разбор, а приложение соберёт понятный план дня.",
        reply_markup=keyboard.as_markup(),
    )


@dp.message(F.text)
async def capture_message(message: types.Message):
    await answer_capture(
        message,
        message.text.strip()[:4000],
        "Перенёс текст в DailyOS. Открой AI-разбор и подтверди задачи перед сохранением.",
    )


@dp.message(F.voice)
async def capture_voice(message: types.Message):
    if not GROQ_API_KEY:
        await message.answer("Голосовой разбор требует GROQ_API_KEY в Render.")
        return
    try:
        file = BytesIO()
        await bot.download(message.voice.file_id, destination=file)
        text = await groq_transcribe_voice(file.getvalue())
    except Exception as exc:
        await message.answer(str(exc))
        return
    await answer_capture(message, text, "Голос распознан. Открой DailyOS и преврати его в задачи.")


@dp.message(F.photo)
async def capture_photo(message: types.Message):
    if not GROQ_API_KEY:
        await message.answer("Разбор скриншотов требует GROQ_API_KEY в Render.")
        return
    try:
        file = BytesIO()
        await bot.download(message.photo[-1].file_id, destination=file)
        text = await groq_read_image(file.getvalue())
    except Exception as exc:
        await message.answer(str(exc))
        return
    await answer_capture(message, text, "Скриншот прочитан. Открой DailyOS и подтверди найденные задачи.")


async def index(_request: web.Request):
    return web.FileResponse(FRONTEND)


async def health(_request: web.Request):
    return web.json_response({"status": "ok", "ai": bool(GROQ_API_KEY), "provider": "groq"})


async def capture(request: web.Request):
    try:
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    text = pop_capture(request.match_info["capture_id"], user["id"])
    if text is None:
        raise web.HTTPNotFound(text="Разбор устарел. Отправь сообщение боту ещё раз.")
    return web.json_response({"text": text})


async def fridge_photo(request: web.Request):
    if not GROQ_API_KEY:
        raise web.HTTPServiceUnavailable(text="GROQ_API_KEY не настроен")
    try:
        validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        reader = await request.multipart()
        field = await reader.next()
        if not field or field.name != "photo":
            raise ValueError("Фото не найдено")
        data = await field.read(decode=False)
        if len(data) > 5_000_000:
            raise ValueError("Фото больше 5 МБ")
        result = await groq_fridge_photo(data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except RuntimeError as exc:
        raise web.HTTPBadGateway(text=str(exc)) from exc
    return web.json_response({"result": result})


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
    app = web.Application(client_max_size=6 * 1024 * 1024)
    app["rate_limits"] = defaultdict(deque)
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/capture/{capture_id}", capture),
        web.post("/api/fridge/photo", fridge_photo),
        web.post("/api/ai", ai),
    ])
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
