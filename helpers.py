from __future__ import annotations

from aiogram import Bot
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keyboards.common import sub_gate
from services.settings import get_setting
from services.users import User, verify_subscription
from utils.formatting import money


async def require_access(session: AsyncSession, bot: Bot, event: Message | CallbackQuery) -> bool:
    tg_user = event.from_user
    user = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    if user is None:
        return False
    if user.is_blocked:
        text = "🚫 <b>ДОСТУП ОБМЕЖЕНО</b>\n\nЗверніться до адміністратора: @ua_101"
        if isinstance(event, CallbackQuery):
            await event.answer("🚫 Доступ обмежено", show_alert=True)
        else:
            await event.answer(text)
        return False
    ok, _ = await verify_subscription(session, bot, user)
    if ok:
        return True
    username = await get_setting(session, "channel_username", "")
    url = f"https://t.me/{username.lstrip('@')}" if username else "https://t.me/"
    text = "🔒 <b>СПОЧАТКУ ПІДПИШИСЯ НА КАНАЛ</b>\n\nЩоб користуватися ботом, виконай підписку та натисни перевірку."
    markup = sub_gate(url)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)
    return False
