from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.markdown import hbold
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import TaskType, Transaction
from keyboards.common import back_home, main_menu
from keyboards.tasks import task_card
from keyboards.withdraw import payout_methods
from services.history import get_history
from services.rewards import consume_spin_and_reward
from services.settings import get_setting
from utils.config import Config
from services.tasks import complete_task, get_active_tasks
from services.users import get_user_by_tg_id, get_profile
from services.withdrawals import create_withdrawal, list_payout_methods
from utils.formatting import masked_destination, money
from utils.security import parse_amount, validate_card
from utils.states import WithdrawalStates
from utils.texts import RULES_TEXT, SUPPORT_TEXT
from .helpers import require_access

router = Router()


async def render_home(callback: CallbackQuery, session: AsyncSession, is_admin: bool):
    user = await get_profile(session, callback.from_user.id)
    await callback.message.edit_text(
        f"🏠 <b>ГОЛОВНЕ МЕНЮ</b>\n\n"
        f"💰 Баланс: <b>{money(user.balance)} грн</b>\n"
        f"🎁 Спінів: <b>{user.spins}</b>",
        reply_markup=main_menu(is_admin),
    )


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Config):
    is_admin = callback.from_user.id in config.admin_ids
    if not await require_access(session, bot, callback):
        return
    await render_home(callback, session, is_admin)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    user = await get_profile(session, callback.from_user.id)
    text = (
        "👤 <b>МІЙ ПРОФІЛЬ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Баланс: <b>{money(user.balance)} грн</b>\n"
        f"🎁 Спінів: <b>{user.spins}</b>\n"
        f"👥 Запрошено друзів: <b>{user.referral_count}</b>\n"
        f"🎯 Виконано завдань: <b>{user.task_count}</b>\n"
        f"💸 Всього отримано: <b>{money(user.total_won)} грн</b>\n"
        f"💳 Всього виплачено: <b>{money(user.total_paid)} грн</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "menu:spin")
async def menu_spin(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    from services.admin import get_prizes
    prizes = await get_prizes(session)
    user = await get_profile(session, callback.from_user.id)
    prize_lines = []
    icons = ["💰", "💰", "💰", "⭐", "💎", "💎", "🔥"]
    for prize, icon in zip(prizes, icons):
        prize_lines.append(f"{icon} {money(prize.amount)} грн")
    text = (
        "🎁 <b>ОТРИМАТИ ПРИЗ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Доступних спінів: <b>{user.spins}</b>\n\n"
        "🎁 <b>МОЖЛИВІ ПРИЗИ:</b>\n" + "\n".join(prize_lines)
    )
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 ОТРИМАТИ ПРИЗ", callback_data="spin:run")],
        [InlineKeyboardButton(text="🏠 ГОЛОВНЕ МЕНЮ", callback_data="menu:home")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "spin:run")
async def spin_run(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    try:
        amount = await consume_spin_and_reward(session, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    amounts = ["10", "20", "30", "50", "100", "200", "300"]
    message = callback.message
    await message.edit_text("🎁 <b>ВИЗНАЧАЄМО ТВІЙ ПРИЗ...</b>\n\nСервер уже визначив результат.\n\n⚡ Крутимо приз…")
    for idx in range(8):
        current = amounts[idx % len(amounts)]
        lines = []
        for value in amounts:
            lines.append(f"👉 <b>{value} грн</b> 👈" if value == current else f"{value} грн")
        await asyncio.sleep(0.12 + idx * 0.045)
        await message.edit_text("🎁 <b>ВИЗНАЧАЄМО ТВІЙ ПРИЗ...</b>\n\n" + "\n".join(lines))
    final_lines = [f"👉 <b>{value} грн</b> 👈" if Decimal(value) == amount else f"{value} грн" for value in amounts]
    await message.edit_text("🎁 <b>ФІКСУЄМО РЕЗУЛЬТАТ...</b>\n\n" + "\n".join(final_lines))
    await asyncio.sleep(0.35)
    user = await get_profile(session, callback.from_user.id)
    await message.edit_text(
        f"🎉 <b>ВИГРАШ ВИЗНАЧЕНО!</b>\n\n💰 <b>+{money(amount)} грн</b>\n\n💳 Баланс: <b>{money(user.balance)} грн</b>",
        reply_markup=back_home(),
    )
    await callback.answer("🎉 Вітаємо!")


@router.callback_query(F.data == "menu:tasks")
async def menu_tasks(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    user = await get_profile(session, callback.from_user.id)
    tasks = await get_active_tasks(session, user.id)
    await callback.message.edit_text(
        "📋 <b>ЗАВДАННЯ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "Виконуй доступні завдання та отримуй винагороди.\n\n"
        + ("✨ Поки що нових завдань немає." if not tasks else "Обери завдання нижче 👇"),
        reply_markup=back_home(),
    )
    for task in tasks:
        type_icon = {TaskType.SUBSCRIBE: "📢", TaskType.VIEW_CHANNEL: "👀", TaskType.LINK: "🔗", TaskType.OTHER: "📣"}[task.task_type]
        text = f"{type_icon} <b>{task.title}</b>\n\n{task.description}\n\n💰 Нагорода: <b>{money(task.reward)} грн</b>"
        await callback.message.answer(text, reply_markup=task_card(task.id, task.link))
    await callback.answer()


@router.callback_query(F.data.startswith("task:check:"))
async def task_check(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    try:
        status, reward = await complete_task(session, bot, callback.from_user.id, task_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if status == "completed":
        user = await get_profile(session, callback.from_user.id)
        await callback.message.edit_text(
            f"🎉 <b>Завдання виконано!</b>\n\n💰 <b>+{money(reward)} грн</b>\n💳 Баланс: <b>{money(user.balance)} грн</b>",
            reply_markup=back_home(),
        )
    else:
        await callback.message.edit_text("⏳ <b>Заявку прийнято на перевірку.</b>\n\nПісля підтвердження винагорода буде нарахована.", reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "menu:ref")
async def menu_ref(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    user = await get_profile(session, callback.from_user.id)
    me = await bot.get_me()
    url = f"https://t.me/{me.username}?start=ref_{user.telegram_id}"
    remaining = 0 if user.referral_count % 3 == 0 else 3 - user.referral_count % 3
    spins_earned = user.referral_count // 3
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ЗАПРОСИТИ ДРУЗІВ", url=f"https://t.me/share/url?url={url}&text=%F0%9F%8E%81%20%D0%97%D0%B0%D0%BB%D1%83%D1%87%D0%B0%D0%B9%D1%81%D1%8F%20%D0%B4%D0%BE%20reward-бот%D0%B0!")],
        [InlineKeyboardButton(text="🏠 ГОЛОВНЕ МЕНЮ", callback_data="menu:home")],
    ])
    text = (
        "👥 <b>ТВОЯ РЕФЕРАЛЬНА СИСТЕМА</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Запрошено: <b>{user.referral_count}</b>\n"
        f"До наступного спіну: <b>{remaining}</b>\n"
        f"🎁 Отримано спінів за друзів: <b>{spins_earned}</b>\n\n"
        f"🔗 Твоє посилання:\n<code>{url}</code>"
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "menu:withdraw")
async def menu_withdraw(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    user = await get_profile(session, callback.from_user.id)
    minimum = Decimal(await get_setting(session, "min_withdrawal", "50"))
    methods = await list_payout_methods(session)
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = await payout_methods(methods)
    if not methods:
        text = "💸 <b>ВИВЕДЕННЯ</b>\n\n😕 Способи виплати поки не налаштовані."
    else:
        text = f"💸 <b>ВИВЕДЕННЯ</b>\n━━━━━━━━━━━━━━━━━━━━\n💰 Баланс: <b>{money(user.balance)} грн</b>\n\nМінімальна виплата: <b>{money(minimum)} грн</b>\n\nОбери спосіб виплати 👇"
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("wd:method:"))
async def wd_method(callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    if not await require_access(session, bot, callback):
        return
    method_id = int(callback.data.rsplit(":", 1)[1])
    method = await __import__("services.withdrawals", fromlist=["get_payout_method"]).get_payout_method(session, method_id)
    if not method:
        await callback.answer("Спосіб не знайдено", show_alert=True)
        return
    await state.update_data(method_id=method_id, method_kind=method.kind, method_name=method.name)
    await state.set_state(WithdrawalStates.waiting_amount)
    minimum = Decimal(await get_setting(session, "min_withdrawal", "50"))
    await callback.message.edit_text(f"💳 <b>{method.name}</b>\n\n💰 Введи суму виплати.\nМінімум: <b>{money(minimum)} грн</b>")
    await callback.answer()


@router.message(WithdrawalStates.waiting_amount)
async def wd_amount(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, message):
        return
    try:
        amount = parse_amount(message.text or "")
    except Exception:
        await message.answer("❌ Введи коректну суму, наприклад <b>50</b> або <b>125.50</b>.")
        return
    await state.update_data(amount=str(amount))
    kind = (await state.get_data()).get("method_kind")
    if kind == "card":
        await state.set_state(WithdrawalStates.waiting_destination)
        await message.answer("💳 Введи номер банківської картки (12–19 цифр).")
    else:
        await state.set_state(WithdrawalStates.waiting_destination)
        await message.answer("💳 Вкажи реквізит / адресу для виплати.")


@router.message(WithdrawalStates.waiting_destination)
async def wd_destination(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, message):
        return
    data = await state.get_data()
    destination = (message.text or "").strip()
    if data.get("method_kind") == "card":
        try:
            destination = validate_card(destination)
        except Exception as exc:
            await message.answer(f"❌ {exc}")
            return
    try:
        withdrawal = await create_withdrawal(session, message.from_user.id, int(data["method_id"]), Decimal(data["amount"]), destination)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ <b>ЗАЯВКУ СТВОРЕНО</b>\n\n"
        f"💰 Сума: <b>{money(withdrawal.amount)} грн</b>\n"
        f"💳 Реквізит: <code>{masked_destination(destination)}</code>\n"
        "⏳ Статус: <b>Очікує</b>\n\n"
        "Сума вже зарезервована з доступного балансу та буде повернута у разі відхилення.",
        reply_markup=back_home(),
    )


@router.callback_query(F.data == "menu:history")
async def menu_history(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    user = await get_profile(session, callback.from_user.id)
    items = await get_history(session, user.id, 20)
    if not items:
        text = "📜 <b>ІСТОРІЯ</b>\n\nПоки що операцій немає."
    else:
        rows = ["📜 <b>ІСТОРІЯ</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for item in items:
            sign = "+" if Decimal(item.amount) >= 0 else ""
            date = item.created_at.strftime("%d.%m.%Y %H:%M") if item.created_at else ""
            rows.append(f"{date} • {item.description} • <b>{sign}{money(item.amount)} грн</b>")
        text = "\n".join(rows)
    await callback.message.edit_text(text, reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "menu:rules")
async def menu_rules(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    await callback.message.edit_text(RULES_TEXT, reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def menu_support(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if not await require_access(session, bot, callback):
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    support = await get_setting(session, "support_username", "ua_101")
    support_clean = support.lstrip("@")
    support_url = f"https://t.me/{support_clean}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 НАПИСАТИ АДМІНУ", url=support_url)],
        [InlineKeyboardButton(text="🤝 СПІВПРАЦЯ", url=support_url)],
        [InlineKeyboardButton(text="🏠 ГОЛОВНЕ МЕНЮ", callback_data="menu:home")],
    ])
    await callback.message.edit_text(SUPPORT_TEXT, reply_markup=kb)
    await callback.answer()
