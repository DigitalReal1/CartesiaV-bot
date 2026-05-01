import os
import asyncio
import tempfile
import sys
import json
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from cartesia import Cartesia
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CARTESIA_API_KEY = os.getenv('CARTESIA_API_KEY')
FFMPEG_PATH = os.getenv('FFMPEG_PATH', 'ffmpeg-8.1-essentials_build/bin/ffmpeg.exe')
CARTESIA_MODEL = 'sonic-2'
PRONUNCIATION_DICT_ID = os.getenv('PRONUNCIATION_DICT_ID')

if FFMPEG_PATH and not os.path.isabs(FFMPEG_PATH):
    FFMPEG_PATH = os.path.join(os.path.dirname(__file__), FFMPEG_PATH)
if FFMPEG_PATH and not os.path.exists(FFMPEG_PATH):
    # Fallback to ffmpeg on PATH (common on Windows when installed system-wide)
    FFMPEG_PATH = "ffmpeg"

print(f"Token loaded: {bool(TELEGRAM_BOT_TOKEN)} | Cartesia Key loaded: {bool(CARTESIA_API_KEY)}")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Make Windows console output Unicode-safe (fixes Korean/Japanese/etc prints).
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- Persistence (save per-user settings across restarts) ---
PREFS_PATH = os.path.join(os.path.dirname(__file__), "user_prefs.json")


def _atomic_write_json(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _load_prefs() -> dict:
    if not os.path.exists(PREFS_PATH):
        return {}
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        # If prefs are corrupted, ignore and start fresh.
        return {}


def _save_prefs() -> None:
    payload = {
        "voice": {str(k): v for k, v in user_voice_preference.items()},
        "model": {str(k): v for k, v in user_model_preference.items()},
        "lang": {str(k): v for k, v in user_language_preference.items()},
        "vibe": {str(k): v for k, v in user_vibe.items()},
    }
    _atomic_write_json(PREFS_PATH, payload)


def _restore_prefs() -> None:
    prefs = _load_prefs()

    def _to_int_keys(d: dict) -> dict:
        out = {}
        for k, v in (d or {}).items():
            try:
                out[int(k)] = v
            except Exception:
                continue
        return out

    user_voice_preference.update(_to_int_keys(prefs.get("voice", {})))
    user_model_preference.update(_to_int_keys(prefs.get("model", {})))
    user_language_preference.update(_to_int_keys(prefs.get("lang", {})))
    user_vibe.update(_to_int_keys(prefs.get("vibe", {})))


# Voice options
VOICE_OPTIONS = {
    'Chan 2': 'bfe0a83d-c918-4c62-bb01-fb4b70e8cbc8',
    'Cathy': 'e8e5fffb-252c-436d-b842-8879b84445b6',
    'Rae': '66c6b81c-ddb7-4892-bdd5-19b5a7be38e7',
    'Rupert': '0ad65e7f-006c-47cf-bd31-52279d487913',
    'Cole': '3e39e9a5-585c-4f5f-bac6-5e4905c51095',
}
user_voice_preference = {}
DEFAULT_VOICE_NAME = 'Chan 2'

# Model options (matches Cartesia docs)
MODEL_OPTIONS = {
    "Sonic 3 (Stable)": "sonic-3",
    "Sonic 3 (Latest)": "sonic-3-latest",
    "Sonic 2": "sonic-2",
    "Sonic Turbo": "sonic-turbo",
    "Sonic": "sonic",
}
user_model_preference = {}
DEFAULT_MODEL_NAME = next((k for k, v in MODEL_OPTIONS.items() if v == CARTESIA_MODEL), "Sonic 2")

# Language options
LANGUAGE_OPTIONS = {
    "Auto-detect": None,
    "English": "en",
    "Korean": "ko",
    "Japanese": "ja",
    "Spanish": "es",
    "Hindi": "hi",
}
user_language_preference = {}
DEFAULT_LANGUAGE_NAME = "Auto-detect"

# Default vibe settings
user_vibe = {}
DEFAULT_VIBE = {"speed": 1.0, "volume": 1.0, "emotion": "neutral"}

EMOTION_OPTIONS = [
    "neutral",
    "happy",
    "excited",
    "curious",
    "calm",
    "sad",
    "angry",
]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _format_vibe(vibe: dict) -> str:
    # Use HTML formatting to avoid Telegram Markdown entity issues (e.g. underscores).
    return (
        "generation_config\n"
        f"emotion: <code>{vibe['emotion']}</code>\n"
        f"speed: <code>{vibe['speed']:.2f}x</code>\n"
        f"volume: <code>{vibe['volume']:.2f}x</code>"
    )


def _vibe_keyboard(vibe: dict) -> InlineKeyboardMarkup:
    speed = vibe["speed"]
    volume = vibe["volume"]
    emotion = vibe["emotion"]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Emotion", callback_data="vibe_emotion_menu"),
                InlineKeyboardButton(text=f"{emotion}", callback_data="vibe_emotion_menu"),
            ],
            [
                InlineKeyboardButton(text="Speed −", callback_data="vibe_speed_-"),
                InlineKeyboardButton(text=f"{speed:.2f}x", callback_data="vibe_noop"),
                InlineKeyboardButton(text="Speed +", callback_data="vibe_speed_+"),
            ],
            [
                InlineKeyboardButton(text="Volume −", callback_data="vibe_volume_-"),
                InlineKeyboardButton(text=f"{volume:.2f}x", callback_data="vibe_noop"),
                InlineKeyboardButton(text="Volume +", callback_data="vibe_volume_+"),
            ],
        ]
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Send me any text and I'll reply with a Telegram voice note generated by Cartesia!\n"
        "Use /voice to change the voice.\n"
        "Use /model to choose the TTS model.\n"
        "Use /lang to choose transcript language.\n"
        "Use /vibe to adjust speed/emotion/volume (interactive panel)"
    )


@dp.message(Command('voice'))
async def voice_menu(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"voice_{name}") for name in VOICE_OPTIONS.keys()]
        ]
    )
    await message.answer("Choose a voice:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("voice_"))
async def set_voice(call: types.CallbackQuery):
    name = call.data.replace("voice_", "")
    if name not in VOICE_OPTIONS:
        await call.answer("Unknown voice.")
        return
    user_voice_preference[call.from_user.id] = name
    _save_prefs()
    await call.answer()
    await call.message.answer(f"✅ Voice updated to *{name}*", parse_mode="Markdown")


@dp.message(Command("model"))
async def model_menu(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"model_{name}")]
            for name in MODEL_OPTIONS.keys()
        ]
    )
    active_name = user_model_preference.get(message.from_user.id, DEFAULT_MODEL_NAME)
    await message.answer(f"Choose a model (current: *{active_name}*):", parse_mode="Markdown", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("model_"))
async def set_model(call: types.CallbackQuery):
    name = call.data.replace("model_", "")
    if name not in MODEL_OPTIONS:
        await call.answer("Unknown model.")
        return
    user_model_preference[call.from_user.id] = name
    _save_prefs()
    await call.answer()
    await call.message.answer(f"✅ Model updated to *{name}*", parse_mode="Markdown")

@dp.message(Command("lang"))
async def lang_menu(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"lang_{name}")]
            for name in LANGUAGE_OPTIONS.keys()
        ]
    )
    active_name = user_language_preference.get(message.from_user.id, DEFAULT_LANGUAGE_NAME)
    await message.answer(f"Choose a language (current: *{active_name}*):", parse_mode="Markdown", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("lang_"))
async def set_lang(call: types.CallbackQuery):
    name = call.data.replace("lang_", "")
    if name not in LANGUAGE_OPTIONS:
        await call.answer("Unknown language.")
        return
    user_language_preference[call.from_user.id] = name
    _save_prefs()
    await call.answer()
    await call.message.answer(f"✅ Language updated to *{name}*", parse_mode="Markdown")


@dp.message(Command('vibe'))
async def set_vibe(message: types.Message):
    vibe = user_vibe.get(message.from_user.id, DEFAULT_VIBE.copy())
    args = message.text.split()[1:]

    # If user provides args, accept a simple text form; otherwise show the panel.
    # Examples:
    # /vibe fast happy
    # /vibe speed=1.20 volume=0.90 emotion=calm
    if args:
        for raw in args:
            token = raw.strip()
            lower = token.lower()

            if lower in {"fast", "faster"}:
                vibe["speed"] = 1.2
                continue
            if lower in {"slow", "slower"}:
                vibe["speed"] = 0.85
                continue
            if lower in {"normal", "default"}:
                vibe["speed"] = 1.0
                vibe["volume"] = 1.0
                vibe["emotion"] = "neutral"
                continue

            if "=" in lower:
                key, val = lower.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key == "speed":
                    vibe["speed"] = _clamp(float(val), 0.6, 1.5)
                elif key == "volume":
                    vibe["volume"] = _clamp(float(val), 0.5, 2.0)
                elif key == "emotion":
                    vibe["emotion"] = val if val in EMOTION_OPTIONS else "neutral"
                continue

            # Emotion as a bare token
            if lower in EMOTION_OPTIONS:
                vibe["emotion"] = lower

        user_vibe[message.from_user.id] = vibe
        _save_prefs()
        await message.answer(f"🎛 Vibe updated:\n{_format_vibe(vibe)}", parse_mode="HTML")
        return

    await message.answer(
        "🎛 Vibe panel:\n" + _format_vibe(vibe),
        parse_mode="HTML",
        reply_markup=_vibe_keyboard(vibe),
    )


@dp.callback_query(F.data == "vibe_noop")
async def vibe_noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("vibe_speed_"))
async def vibe_set_speed(call: types.CallbackQuery):
    vibe = user_vibe.get(call.from_user.id, DEFAULT_VIBE.copy())
    direction = call.data.replace("vibe_speed_", "")
    step = 0.05
    vibe["speed"] = _clamp(vibe["speed"] + (step if direction == "+" else -step), 0.6, 1.5)
    user_vibe[call.from_user.id] = vibe
    _save_prefs()
    await call.answer()
    await call.message.edit_text("🎛 Vibe panel:\n" + _format_vibe(vibe), parse_mode="HTML", reply_markup=_vibe_keyboard(vibe))


@dp.callback_query(F.data.startswith("vibe_volume_"))
async def vibe_set_volume(call: types.CallbackQuery):
    vibe = user_vibe.get(call.from_user.id, DEFAULT_VIBE.copy())
    direction = call.data.replace("vibe_volume_", "")
    step = 0.05
    vibe["volume"] = _clamp(vibe["volume"] + (step if direction == "+" else -step), 0.5, 2.0)
    user_vibe[call.from_user.id] = vibe
    _save_prefs()
    await call.answer()
    await call.message.edit_text("🎛 Vibe panel:\n" + _format_vibe(vibe), parse_mode="HTML", reply_markup=_vibe_keyboard(vibe))


@dp.callback_query(F.data == "vibe_emotion_menu")
async def vibe_emotion_menu(call: types.CallbackQuery):
    vibe = user_vibe.get(call.from_user.id, DEFAULT_VIBE.copy())
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=e, callback_data=f"vibe_emotion_{e}") for e in EMOTION_OPTIONS[:4]],
            [InlineKeyboardButton(text=e, callback_data=f"vibe_emotion_{e}") for e in EMOTION_OPTIONS[4:]],
            [InlineKeyboardButton(text="← Back", callback_data="vibe_back")],
        ]
    )
    await call.answer()
    await call.message.edit_text("Select emotion:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("vibe_emotion_"))
async def vibe_set_emotion(call: types.CallbackQuery):
    vibe = user_vibe.get(call.from_user.id, DEFAULT_VIBE.copy())
    emotion = call.data.replace("vibe_emotion_", "")
    vibe["emotion"] = emotion if emotion in EMOTION_OPTIONS else "neutral"
    user_vibe[call.from_user.id] = vibe
    _save_prefs()
    await call.answer()
    await call.message.edit_text("🎛 Vibe panel:\n" + _format_vibe(vibe), parse_mode="HTML", reply_markup=_vibe_keyboard(vibe))


@dp.callback_query(F.data == "vibe_back")
async def vibe_back(call: types.CallbackQuery):
    vibe = user_vibe.get(call.from_user.id, DEFAULT_VIBE.copy())
    await call.answer()
    await call.message.edit_text("🎛 Vibe panel:\n" + _format_vibe(vibe), parse_mode="HTML", reply_markup=_vibe_keyboard(vibe))


def generate_tts_sync(text, voice_id, vibe, model, pronunciation_dict_id=None, language=None):
    """Synchronous Cartesia TTS call for v3.0.2 — runs in thread executor."""
    client = Cartesia(api_key=CARTESIA_API_KEY)

    voice_config = {"mode": "id", "id": voice_id}

    output_format = {
        "container": "wav",
        "encoding": "pcm_f32le",
        "sample_rate": 44100,
    }

    kwargs = {
        "model_id": model,
        "transcript": text,
        "voice": voice_config,
        "output_format": output_format,
    }
    if pronunciation_dict_id:
        # Cartesia SDK expects `pronunciation_dict_id`
        kwargs["pronunciation_dict_id"] = pronunciation_dict_id
    if language:
        kwargs["language"] = language

    # Sonic-3 supports generation_config with speed/volume/emotion.
    if isinstance(model, str) and model.startswith("sonic-3"):
        kwargs["generation_config"] = {
            "emotion": vibe.get("emotion", "neutral"),
            "speed": float(vibe.get("speed", 1.0)),
            "volume": float(vibe.get("volume", 1.0)),
        }

    # Cartesia SDK returns a BinaryAPIResponse (not an iterator)
    response = client.tts.generate(**kwargs)
    return response.read()


@dp.message(F.text)
async def handle_text(message: types.Message):
    try:
        print(f"Message received: {message.text}")
        text = message.text.strip()
        if not text:
            await message.answer("Please send some text.")
            return

        voice_name = user_voice_preference.get(message.from_user.id, DEFAULT_VOICE_NAME)
        voice_id = VOICE_OPTIONS[voice_name]
        vibe = user_vibe.get(message.from_user.id, DEFAULT_VIBE.copy())
        model_name = user_model_preference.get(message.from_user.id, DEFAULT_MODEL_NAME)
        model_id = MODEL_OPTIONS.get(model_name, CARTESIA_MODEL)
        lang_name = user_language_preference.get(message.from_user.id, DEFAULT_LANGUAGE_NAME)
        language = LANGUAGE_OPTIONS.get(lang_name, None)

        print(f"Active voice: {voice_name} | model={model_id} | language={language or 'auto'} | vibe={vibe}")

        # Run sync TTS in thread so we don't block the event loop
        loop = asyncio.get_event_loop()
        audio_bytes = await loop.run_in_executor(
            None,
            generate_tts_sync,
            text,
            voice_id,
            vibe,
            model_id,
            PRONUNCIATION_DICT_ID,
            language,
        )
        print(f"Audio generated: {len(audio_bytes)} bytes")

        if len(audio_bytes) == 0:
            raise RuntimeError("Cartesia returned empty audio — check your API key and model name.")

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = os.path.join(tmpdir, "temp.wav")
            ogg_path = os.path.join(tmpdir, "output.ogg")

            with open(wav_path, "wb") as f:
                f.write(audio_bytes)

            # Convert WAV → OGG Opus using async subprocess (non-blocking)
            cmd = [
                FFMPEG_PATH,
                "-y", "-i", wav_path,
                "-c:a", "libopus",
                "-b:a", "32k",
                ogg_path
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg failed: {stderr.decode()}")
            print(f"FFmpeg finished: {ogg_path}")

            voice_file = FSInputFile(ogg_path)
            await message.answer_voice(voice=voice_file)

    except Exception as e:
        print(f"Exception: {e}", flush=True)
        import traceback
        traceback.print_exc()
        await message.answer("⚠️ An error occurred while generating your voice note.")


async def main():
    import logging
    logging.basicConfig(level=logging.INFO)
    _restore_prefs()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
