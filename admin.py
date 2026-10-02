from __future__ import annotations

import asyncio
from decimal import Decimal
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AdminLog, PayoutMethod, Prize, TransactionType, User, WithdrawalStatus
from keyboards.admin import admin_menu, confirm
from keyboards.tasks import task_review
from keyboards.withdraw import withdrawal_admin
from services.admin import add_admin_log, dashboard, get_pending_withdrawals, get_prizes, list_methods, list_recent_users, set_method_active, set_prize_chance
from services.rewards import add_spins, credit_balance
from services.settings import get_setting, set_setting
from services.stats import dashboard as stats_dashboard
from services.tasks import get_pending_completions, review_task_completion
from services.users import get_user_by_tg_id
from services.withdrawals import process_withdrawal
from utils.config import Config
from utils.formatting import masked_destination, money
from utils.security import parse_amount, parse_int, validate_url
from utils.states import (
    AdminBroadcastStates, AdminChannelStates, AdminChanceStates, AdminCreditStates,
    AdminPayoutMethodStates, AdminSettingStates, AdminSpinStates, AdminTaskStates,
)

router = Router()

TASK_TYPES = [
    ("📢 Підписка на Telegram-канал", "subscribe"),
    ("👀 Перегляд Telegram-каналу", "view_channel"),
    ("🔗 Перехід за посиланням", "link"),
    ("📣 Інше завдання", "other"),
]


def is_admin_user(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


def admin_only(callback: CallbackQuery, config: Config) -> bool:
    return is_admin_user(callback.from_user.id, config)


@router.callback_query(F.data == "admin:open")
async def admin_open(callback: CallbackQuery, config: Config):
    if not admin_only(callback, config):
        await callback.answer("⛔ Немає доступу", show_alert=True)
        return
    await callback.message.edit_text("👑 <b>АДМІН-ПАНЕЛЬ</b>\n\nКерування reward-платформою:", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config):
        return
    s = await dashboard(session)
    text = (
        "📊 <b>СТАТИСТИКА</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Всього користувачів: <b>{s['total_users']}</b>\n"
        f"🟢 Активних за 24 год: <b>{s['active']}</b>\n"
        f"📅 Нових сьогодні: <b>{s['new_today']}</b>\n"
        f"📅 Нових за 7 днів: <b>{s['new_week']}</b>\n"
        f"🎁 Всього виграшних спінів: <b>{s['spins']}</b>\n"
        f"💰 Позитивних нарахувань: <b>{money(s['credited'])} грн</b>\n"
        f"💸 Всього виплачено: <b>{money(s['paid'])} грн</b>\n"
        f"📋 Виконано завдань: <b>{s['tasks']}</b>\n"
        f"👥 Всього рефералів: <b>{s['refs']}</b>"
    )
    await callback.message.edit_text(text, reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config):
        return
    users = await list_recent_users(session, 20)
    if not users:
        text = "👥 <b>КОРИСТУВАЧІ</b>\n\nПоки немає користувачів."
    else:
        rows = ["👥 <b>ОСТАННІ КОРИСТУВАЧІ</b>"]
        for u in users:
            uname = f"@{u.username}" if u.username else "без username"
            rows.append(f"<code>{u.telegram_id}</code> • {escape(uname)} • 💰 {money(u.balance)} • 🎁 {u.spins}")
        text = "\n".join(rows)
    await callback.message.edit_text(text, reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:balance")
async def adm_balance(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config):
        return
    await state.set_state(AdminCreditStates.waiting_user_id)
    await state.update_data(mode="lookup")
    await callback.message.edit_text("💰 <b>БАЛАНСИ</b>\n\nВведи Telegram ID користувача:")
    await callback.answer()


@router.message(AdminCreditStates.waiting_user_id)
async def admin_user_id(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids:
        return
    try:
        tg_id = parse_int(message.text or "", minimum=1)
    except Exception:
        await message.answer("❌ Некоректний Telegram ID")
        return
    data = await state.get_data()
    mode = data.get("mode")
    user = await get_user_by_tg_id(session, tg_id)
    if mode == "lookup":
        if not user:
            await message.answer("❌ Користувача не знайдено", reply_markup=admin_menu())
        else:
            uname = f"@{user.username}" if user.username else "без username"
            await message.answer(
                f"👤 <b>КОРИСТУВАЧ</b>\n\nID: <code>{user.telegram_id}</code>\nUsername: {escape(uname)}\n\n💰 Баланс: <b>{money(user.balance)} грн</b>\n🎁 Спінів: <b>{user.spins}</b>\n👥 Рефералів: <b>{user.referral_count}</b>\n💸 Всього отримано: <b>{money(user.total_won)} грн</b>\n💳 Всього виплачено: <b>{money(user.total_paid)} грн</b>",
                reply_markup=admin_menu(),
            )
        await state.clear()
        return
    await state.set_state(AdminCreditStates.waiting_amount)
    await state.update_data(user_id=tg_id)
    await message.answer("💰 Введіть суму для зарахування (можна зі знаком + або - лише через окрему операцію не підтримується):")


@router.message(AdminCreditStates.waiting_amount)
async def admin_credit_amount(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids:
        return
    data = await state.get_data()
    try:
        amount = parse_amount(message.text or "")
    except Exception as exc:
        await message.answer(f"❌ {exc}")
        return
    user = await get_user_by_tg_id(session, int(data["user_id"]))
    if not user:
        await message.answer("❌ Користувача не знайдено")
        await state.clear()
        return
    await state.update_data(amount=str(amount), username=user.username, first_name=user.first_name)
    await state.set_state(AdminCreditStates.waiting_amount)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ПІДТВЕРДИТИ", callback_data="adm:creditconfirm"),
        InlineKeyboardButton(text="❌ СКАСУВАТИ", callback_data="adm:cancelstate"),
    ]])
    await message.answer(f"👤 Користувач: <b>{escape('@'+user.username if user.username else user.first_name)}</b>\n💰 Сума: <b>{money(amount)} грн</b>", reply_markup=kb)


@router.callback_query(F.data == "adm:creditconfirm")
async def admin_credit_confirm(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config):
    if not admin_only(callback, config):
        return
    data = await state.get_data()
    if "user_id" not in data or "amount" not in data:
        await callback.answer("Стан застарів", show_alert=True)
        return
    amount = Decimal(data["amount"])
    try:
        user = await credit_balance(session, int(data["user_id"]), amount, TransactionType.ADMIN_CREDIT, "Адмінське нарахування")
        await add_admin_log(session, callback.from_user.id, "credit_balance", user.telegram_id, f"+{amount} грн")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("✅ <b>Кошти зараховані.</b>", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:credit")
async def adm_credit_start(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config):
        return
    await state.set_state(AdminCreditStates.waiting_user_id)
    await state.update_data(mode="credit")
    await callback.message.edit_text("💵 <b>ВИДАЧА ГРН</b>\n\nВведи Telegram ID користувача:")
    await callback.answer()


@router.callback_query(F.data == "adm:spins")
async def adm_spins_start(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config):
        return
    await state.set_state(AdminSpinStates.waiting_user_id)
    await callback.message.edit_text("🎁 <b>ВИДАЧА СПІНІВ</b>\n\nВведи Telegram ID користувача:")
    await callback.answer()


@router.message(AdminSpinStates.waiting_user_id)
async def adm_spins_user(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids:
        return
    try:
        user_id = parse_int(message.text or "", minimum=1)
    except Exception:
        await message.answer("❌ Некоректний ID")
        return
    await state.update_data(user_id=user_id)
    await state.set_state(AdminSpinStates.waiting_amount)
    await message.answer("🎁 Введи кількість спінів. Наприклад: <b>5</b> або <b>-2</b>")


@router.message(AdminSpinStates.waiting_amount)
async def adm_spins_amount(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids:
        return
    try:
        amount = int((message.text or "").strip())
    except Exception:
        await message.answer("❌ Введи ціле число, наприклад +5 або -2.")
        return
    data = await state.get_data()
    user = await get_user_by_tg_id(session, int(data["user_id"]))
    if not user:
        await message.answer("❌ Користувача не знайдено")
        return
    try:
        user_after = await add_spins(session, user.telegram_id, amount)
        await add_admin_log(session, message.from_user.id, "change_spins", user.telegram_id, f"{amount:+d}")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        await state.clear()
        return
    await state.clear()
    await message.answer(f"✅ Спіни змінено. Новий баланс спінів: <b>{user_after.spins}</b>", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:tasks")
async def adm_tasks(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config):
        return
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="➕ ДОДАТИ ЗАВДАННЯ", callback_data="adm:taskadd"))
    pending = await get_pending_completions(session)
    if pending:
        b.add(InlineKeyboardButton(text=f"🕵️ ПЕРЕВІРКИ ({len(pending)})", callback_data="adm:taskpending"))
    b.add(InlineKeyboardButton(text="🏠 АДМІН-ПАНЕЛЬ", callback_data="admin:open"))
    b.adjust(1)
    await callback.message.edit_text("🎯 <b>КЕРУВАННЯ ЗАВДАННЯМИ</b>\n\nСтворення та перевірка завдань.", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "adm:taskadd")
async def adm_task_add(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config):
        return
    await state.clear()
    await state.set_state(AdminTaskStates.waiting_title)
    await callback.message.edit_text("➕ <b>НОВЕ ЗАВДАННЯ</b>\n\n1/9. Введи назву:")
    await callback.answer()


@router.message(AdminTaskStates.waiting_title)
async def task_title(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AdminTaskStates.waiting_description)
    await message.answer("2/9. Введи опис:")


@router.message(AdminTaskStates.waiting_description)
async def task_description(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    await state.update_data(description=(message.text or "").strip())
    await state.set_state(AdminTaskStates.waiting_type)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"adm:tasktype:{value}")] for label, value in TASK_TYPES])
    await message.answer("3/9. Обери тип:", reply_markup=kb)


@router.callback_query(F.data.startswith("adm:tasktype:"))
async def task_type(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    value = callback.data.split(":", 2)[2]
    await state.update_data(task_type=value)
    await state.set_state(AdminTaskStates.waiting_link)
    await callback.message.edit_text("4/9. Введи посилання для завдання:")
    await callback.answer()


@router.message(AdminTaskStates.waiting_link)
async def task_link(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: link = validate_url(message.text or "")
    except Exception as exc:
        await message.answer(f"❌ {exc}"); return
    await state.update_data(link=link)
    await state.set_state(AdminTaskStates.waiting_reward)
    await message.answer("5/9. Введи винагороду в грн:")


@router.message(AdminTaskStates.waiting_reward)
async def task_reward(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: reward = parse_amount(message.text or "")
    except Exception as exc:
        await message.answer(f"❌ {exc}"); return
    if reward <= 0: await message.answer("❌ Винагорода має бути більше 0"); return
    await state.update_data(reward=str(reward))
    await state.set_state(AdminTaskStates.waiting_max)
    await message.answer("6/9. Максимальна кількість виконань. Введи число або <b>0</b> для необмеженої:")


@router.message(AdminTaskStates.waiting_max)
async def task_max(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: value = parse_int(message.text or "", minimum=0)
    except Exception as exc: await message.answer(f"❌ {exc}"); return
    await state.update_data(max_completions=None if value == 0 else value)
    await state.set_state(AdminTaskStates.waiting_order)
    await message.answer("7/9. Порядок показу. Введи ціле число, наприклад <b>0</b>:")


@router.message(AdminTaskStates.waiting_order)
async def task_order(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: value = int((message.text or "").strip())
    except Exception: await message.answer("❌ Потрібне ціле число"); return
    await state.update_data(sort_order=value)
    await state.set_state(AdminTaskStates.waiting_verify)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    await message.answer("8/9. Чи потрібна ручна перевірка?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ТАК", callback_data="adm:taskverify:1"), InlineKeyboardButton(text="❌ НІ", callback_data="adm:taskverify:0")
    ]]))


@router.callback_query(F.data.startswith("adm:taskverify:"))
async def task_verify(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    value = callback.data.endswith(":1")
    await state.update_data(requires_verification=value)
    await state.set_state(AdminTaskStates.waiting_active)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    await callback.message.edit_text("9/9. Зробити завдання активним одразу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ АКТИВНЕ", callback_data="adm:taskactive:1"), InlineKeyboardButton(text="⏸ НЕАКТИВНЕ", callback_data="adm:taskactive:0")
    ]]))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:taskactive:"))
async def task_active(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    value = callback.data.endswith(":1")
    await state.update_data(is_active=value)
    data = await state.get_data()
    preview = (
        "📋 <b>НОВЕ ЗАВДАННЯ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Назва: <b>{escape(data.get('title',''))}</b>\n"
        f"Опис: {escape(data.get('description',''))}\n"
        f"Тип: <b>{escape(data.get('task_type',''))}</b>\n"
        f"Винагорода: <b>{data.get('reward')} грн</b>\n"
        f"Ліміт: <b>{data.get('max_completions') or '∞'}</b>\n"
        f"Порядок: <b>{data.get('sort_order',0)}</b>\n"
        f"Перевірка: <b>{'так' if data.get('requires_verification') else 'ні'}</b>\n"
        f"Активне: <b>{'так' if value else 'ні'}</b>\n\n"
        f"Посилання: {escape(data.get('link',''))}"
    )
    await callback.message.edit_text(preview, reply_markup=confirm("adm:tasksave", "adm:cancelstate"))
    await callback.answer()


@router.callback_query(F.data == "adm:tasksave")
async def task_save(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    data = await state.get_data()
    from database.models import Task, TaskType
    try:
        task = Task(
            title=data["title"], description=data.get("description", ""), task_type=TaskType(data["task_type"]), link=data["link"],
            reward=Decimal(data["reward"]), max_completions=data.get("max_completions"), sort_order=int(data.get("sort_order", 0)),
            requires_verification=bool(data.get("requires_verification", True)), is_active=bool(data.get("is_active", True)),
        )
        session.add(task); await session.commit()
        await add_admin_log(session, callback.from_user.id, "create_task", None, f"task_id={task.id}; title={task.title}")
    except Exception as exc:
        await session.rollback(); await callback.answer(f"Помилка: {exc}", show_alert=True); return
    await state.clear()
    await callback.message.edit_text(f"✅ <b>Завдання #{task.id} створено.</b>", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:taskpending")
async def task_pending(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    pending = await get_pending_completions(session, 10)
    if not pending:
        await callback.answer("Немає заявок", show_alert=True); return
    for completion, task, user in pending:
        uname = f"@{user.username}" if user.username else str(user.telegram_id)
        await callback.message.answer(
            f"🕵️ <b>ЗАЯВКА НА ПЕРЕВІРКУ</b>\n\nЗавдання: <b>{escape(task.title)}</b>\nКористувач: <code>{user.telegram_id}</code> {escape(uname)}\n💰 Винагорода: <b>{money(completion.reward)} грн</b>",
            reply_markup=task_review(completion.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:taskapprove:"))
async def task_approve(callback: CallbackQuery, session: AsyncSession, config: Config, bot: Bot):
    if not admin_only(callback, config): return
    cid = int(callback.data.rsplit(":", 1)[1])
    try:
        status, reward, user_id = await review_task_completion(session, cid, True)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True); return
    user = await session.get(User, user_id)
    try:
        await bot.send_message(user.telegram_id, f"🎉 <b>Завдання підтверджено!</b>\n\n💰 +{money(reward)} грн")
    except Exception:
        pass
    await add_admin_log(session, callback.from_user.id, "approve_task", user.telegram_id, f"completion={cid}; +{reward}")
    await callback.message.edit_text("✅ <b>Підтверджено.</b>")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:taskreject:"))
async def task_reject(callback: CallbackQuery, session: AsyncSession, config: Config, bot: Bot):
    if not admin_only(callback, config): return
    cid = int(callback.data.rsplit(":", 1)[1])
    try:
        status, reward, user_id = await review_task_completion(session, cid, False)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True); return
    user = await session.get(User, user_id)
    try: await bot.send_message(user.telegram_id, "❌ <b>Заявку на завдання відхилено.</b>")
    except Exception: pass
    await add_admin_log(session, callback.from_user.id, "reject_task", user.telegram_id, f"completion={cid}")
    await callback.message.edit_text("❌ <b>Відхилено.</b>")
    await callback.answer()


@router.callback_query(F.data == "adm:chances")
async def adm_chances(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    prizes = await get_prizes(session)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(text="✏️ ЗМІНИТИ", callback_data="adm:chanceedit")]]
    text = "🎲 <b>ШАНСИ ПРИЗІВ</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join([f"💰 {money(p.amount)} грн — <b>{p.chance}%</b>" for p in prizes]) + "\n\n⚠️ Сума шансів повинна дорівнювати <b>100%</b>."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="🏠 АДМІН-ПАНЕЛЬ", callback_data="admin:open")]]))
    await callback.answer()


@router.callback_query(F.data == "adm:chanceedit")
async def adm_chance_edit(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    prizes = await get_prizes(session)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{money(p.amount)} грн — {p.chance}%", callback_data=f"adm:chance:{p.id}")] for p in prizes])
    await callback.message.edit_text("🎲 Обери приз, для якого змінити шанс:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:chance:"))
async def adm_chance_select(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    pid = int(callback.data.rsplit(":", 1)[1])
    prize = await session.get(Prize, pid)
    if not prize:
        await callback.answer("Приз не знайдено", show_alert=True); return
    await state.set_state(AdminChanceStates.waiting_chance)
    await state.update_data(prize_id=pid)
    await callback.message.edit_text(f"🎲 <b>{money(prize.amount)} грн</b>\n\nВведи новий шанс у % (наприклад <b>12</b> або <b>12.5</b>):")
    await callback.answer()


@router.message(AdminChanceStates.waiting_chance)
async def adm_chance_value(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: value = Decimal((message.text or "").replace(",", "."))
    except Exception: await message.answer("❌ Некоректний відсоток"); return
    data = await state.get_data()
    try: await set_prize_chance(session, int(data["prize_id"]), value)
    except ValueError as exc: await message.answer(f"❌ {exc}"); return
    await state.clear(); await message.answer("✅ Шанс збережено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:withdrawals")
async def adm_withdrawals(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    items = await get_pending_withdrawals(session)
    if not items:
        await callback.message.edit_text("💸 <b>ВИПЛАТИ</b>\n\n✅ Немає заявок, що очікують.", reply_markup=admin_menu())
        await callback.answer(); return
    for wd in items:
        user = await session.get(User, wd.user_id)
        method = await session.get(PayoutMethod, wd.payout_method_id)
        await callback.message.answer(
            f"💸 <b>ЗАЯВКА #{wd.id}</b>\n\n👤 <code>{user.telegram_id}</code>\n💳 {escape(method.name)}\n💰 <b>{money(wd.amount)} грн</b>\nРеквізит: <code>{masked_destination(wd.destination)}</code>\n⏳ Очікує",
            reply_markup=withdrawal_admin(wd.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:wdpaid:"))
async def adm_wd_paid(callback: CallbackQuery, session: AsyncSession, config: Config, bot: Bot):
    if not admin_only(callback, config): return
    wid = int(callback.data.rsplit(":", 1)[1])
    try: wd = await process_withdrawal(session, wid, WithdrawalStatus.PAID, "Виплачено")
    except ValueError as exc: await callback.answer(str(exc), show_alert=True); return
    user = await session.get(User, wd.user_id)
    await add_admin_log(session, callback.from_user.id, "withdrawal_paid", user.telegram_id, f"withdrawal={wid}; amount={wd.amount}")
    try: await bot.send_message(user.telegram_id, f"✅ <b>ВИПЛАТУ ВИКОНАНО</b>\n\n💰 {money(wd.amount)} грн")
    except Exception: pass
    await callback.message.edit_text(f"✅ <b>Виплачено #{wid}</b>")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:wdreject:"))
async def adm_wd_reject(callback: CallbackQuery, session: AsyncSession, config: Config, bot: Bot):
    if not admin_only(callback, config): return
    wid = int(callback.data.rsplit(":", 1)[1])
    try: wd = await process_withdrawal(session, wid, WithdrawalStatus.REJECTED, "Відхилено")
    except ValueError as exc: await callback.answer(str(exc), show_alert=True); return
    user = await session.get(User, wd.user_id)
    await add_admin_log(session, callback.from_user.id, "withdrawal_rejected", user.telegram_id, f"withdrawal={wid}; refund={wd.amount}")
    try: await bot.send_message(user.telegram_id, f"❌ <b>ВИПЛАТУ ВІДХИЛЕНО</b>\n\n💰 {money(wd.amount)} грн повернено на баланс.")
    except Exception: pass
    await callback.message.edit_text(f"❌ <b>Відхилено #{wid}</b>")
    await callback.answer()


@router.callback_query(F.data == "adm:channels")
async def adm_channels(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    username = await get_setting(session, "channel_username", "")
    chat_id = await get_setting(session, "channel_chat_id", "")
    support = await get_setting(session, "support_username", "ua_101")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ USERNAME КАНАЛУ", callback_data="adm:setchannel")],
        [InlineKeyboardButton(text="✏️ CHAT ID КАНАЛУ", callback_data="adm:setchatid")],
        [InlineKeyboardButton(text="🏠 АДМІН-ПАНЕЛЬ", callback_data="admin:open")],
    ])
    await callback.message.edit_text(f"📢 <b>КАНАЛИ</b>\n\nОсновний канал: <b>{escape(username or 'не задано')}</b>\nChat ID: <code>{escape(chat_id or 'не задано')}</code>\nПідтримка: <b>@{escape(support.lstrip('@'))}</b>\n\nДля перевірки підписок бот має бути адміном каналу.", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:setchannel")
async def adm_setchannel(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminChannelStates.waiting_username)
    await callback.message.edit_text("📢 Введи username каналу без або з @. Наприклад: <b>ua_2024k</b>")
    await callback.answer()


@router.message(AdminChannelStates.waiting_username)
async def adm_channel_username(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids: return
    username = (message.text or "").strip().lstrip("@")
    if not username or " " in username:
        await message.answer("❌ Некоректний username"); return
    await set_setting(session, "channel_username", username)
    await state.clear()
    await message.answer("✅ Username каналу оновлено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:setchatid")
async def adm_setchatid(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminChannelStates.waiting_chat_id)
    await callback.message.edit_text("📢 Введи Chat ID каналу. Для супергрупи/каналу це зазвичай число на кшталт <code>-100123...</code>:")
    await callback.answer()


@router.message(AdminChannelStates.waiting_chat_id)
async def adm_channel_chatid(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids: return
    value = (message.text or "").strip()
    try: int(value)
    except Exception: await message.answer("❌ Chat ID має бути числом"); return
    await set_setting(session, "channel_chat_id", value)
    await state.clear(); await message.answer("✅ Chat ID каналу оновлено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    minimum = await get_setting(session, "min_withdrawal", "50")
    support = await get_setting(session, "support_username", "ua_101")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 ЗМІНИТИ МІНІМУМ ВИПЛАТИ", callback_data="adm:minwd")],
        [InlineKeyboardButton(text="📞 ЗМІНИТИ SUPPORT", callback_data="adm:support")],
        [InlineKeyboardButton(text="💳 СПОСОБИ ВИПЛАТИ", callback_data="adm:payouts")],
        [InlineKeyboardButton(text="🏠 АДМІН-ПАНЕЛЬ", callback_data="admin:open")],
    ])
    await callback.message.edit_text(f"⚙️ <b>НАЛАШТУВАННЯ</b>\n\nМінімальна виплата: <b>{money(Decimal(minimum))} грн</b>\nПідтримка: <b>@{escape(support.lstrip('@'))}</b>", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:minwd")
async def adm_minwd(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminSettingStates.waiting_min_withdrawal)
    await callback.message.edit_text("💸 Введи новий мінімум виплати в грн:")
    await callback.answer()


@router.message(AdminSettingStates.waiting_min_withdrawal)
async def adm_minwd_value(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids: return
    try: amount = parse_amount(message.text or "")
    except Exception as exc: await message.answer(f"❌ {exc}"); return
    await set_setting(session, "min_withdrawal", str(amount))
    await state.clear(); await message.answer("✅ Мінімальну виплату змінено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:support")
async def adm_support(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminSettingStates.waiting_support_username)
    await callback.message.edit_text("📞 Введи username підтримки без @ або з @:")
    await callback.answer()


@router.message(AdminSettingStates.waiting_support_username)
async def adm_support_value(message: Message, state: FSMContext, session: AsyncSession, config: Config):
    if message.from_user.id not in config.admin_ids: return
    username = (message.text or "").strip().lstrip("@")
    if not username or " " in username:
        await message.answer("❌ Некоректний username"); return
    await set_setting(session, "support_username", username)
    await state.clear(); await message.answer("✅ Username підтримки оновлено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:payouts")
async def adm_payouts(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    methods = await list_methods(session)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(text="➕ ДОДАТИ СПОСІБ", callback_data="adm:payadd")]]
    for method in methods:
        state = "✅" if method.is_active else "⏸"
        rows.append([InlineKeyboardButton(text=f"{state} {method.name}", callback_data=f"adm:paytoggle:{method.id}")])
    rows.append([InlineKeyboardButton(text="🏠 НАЛАШТУВАННЯ", callback_data="adm:settings")])
    await callback.message.edit_text("💳 <b>СПОСОБИ ВИПЛАТИ</b>\n\nНатисни на спосіб, щоб увімкнути/вимкнути його:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data == "adm:payadd")
async def adm_payadd(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminPayoutMethodStates.waiting_name)
    await callback.message.edit_text("💳 Введи назву способу виплати:")
    await callback.answer()


@router.message(AdminPayoutMethodStates.waiting_name)
async def adm_pay_name(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(AdminPayoutMethodStates.waiting_kind)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    await message.answer("Тип реквізиту:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Картка", callback_data="adm:paykind:card"), InlineKeyboardButton(text="📝 Текст", callback_data="adm:paykind:text")
    ]]))


@router.callback_query(F.data.startswith("adm:paykind:"))
async def adm_pay_kind(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    kind = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    session.add(PayoutMethod(name=data["name"], kind=kind, is_active=True))
    await session.commit(); await state.clear()
    await callback.message.edit_text("✅ Спосіб виплати додано.", reply_markup=admin_menu()); await callback.answer()


@router.callback_query(F.data.startswith("adm:paytoggle:"))
async def adm_pay_toggle(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    mid = int(callback.data.rsplit(":", 1)[1])
    method = await session.get(PayoutMethod, mid)
    if not method:
        await callback.answer("Не знайдено", show_alert=True); return
    await set_method_active(session, mid, not method.is_active)
    await callback.message.edit_text("✅ Статус способу змінено.", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.set_state(AdminBroadcastStates.waiting_type)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 ТЕКСТ", callback_data="adm:bc:txt"),
        InlineKeyboardButton(text="🖼 ФОТО", callback_data="adm:bc:photo"),
        InlineKeyboardButton(text="🎥 ВІДЕО", callback_data="adm:bc:video"),
    ]])
    await callback.message.edit_text("📢 <b>РОЗСИЛКА</b>\n\nОбери тип повідомлення:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:bc:"))
async def adm_bc_type(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    kind = callback.data.rsplit(":", 1)[1]
    await state.update_data(content_type=kind)
    await state.set_state(AdminBroadcastStates.waiting_content)
    prompt = "Надішли текст повідомлення:" if kind == "txt" else ("Надішли фото. Можеш додати підпис." if kind == "photo" else "Надішли відео. Можеш додати підпис.")
    await callback.message.edit_text(f"📢 {prompt}")
    await callback.answer()


@router.message(AdminBroadcastStates.waiting_content)
async def adm_bc_content(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    data = await state.get_data(); kind = data.get("content_type")
    if kind == "txt":
        if not message.text: await message.answer("❌ Потрібен текст"); return
        await state.update_data(text=message.text, media_id=None)
    elif kind == "photo":
        if not message.photo: await message.answer("❌ Надішли фото"); return
        await state.update_data(text=message.caption or "", media_id=message.photo[-1].file_id)
    else:
        if not message.video: await message.answer("❌ Надішли відео"); return
        await state.update_data(text=message.caption or "", media_id=message.video.file_id)
    await state.set_state(AdminBroadcastStates.waiting_button_text)
    await message.answer("Кнопка під повідомленням?\nВведи текст кнопки або <b>-</b> щоб без кнопки.")


@router.message(AdminBroadcastStates.waiting_button_text)
async def adm_bc_button_text(message: Message, state: FSMContext, config: Config):
    if message.from_user.id not in config.admin_ids: return
    value = (message.text or "").strip()
    if value == "-":
        await state.update_data(button_text=None, button_url=None)
        await state.set_state(AdminBroadcastStates.waiting_button_url)
        await message.answer("Готово. Напиши <b>ПІДТВЕРДИТИ</b> для розсилки або <b>СКАСУВАТИ</b>:")
    else:
        await state.update_data(button_text=value)
        await state.set_state(AdminBroadcastStates.waiting_button_url)
        await message.answer("Введи URL кнопки:")


@router.message(AdminBroadcastStates.waiting_button_url)
async def adm_bc_button_url(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config):
    if message.from_user.id not in config.admin_ids: return
    data = await state.get_data()
    raw = (message.text or "").strip()
    if data.get("button_text"):
        if raw.upper() in {"СКАСУВАТИ", "CANCEL"}:
            await state.clear(); await message.answer("❌ Скасовано.", reply_markup=admin_menu()); return
        try: url = validate_url(raw)
        except Exception as exc: await message.answer(f"❌ {exc}"); return
        await state.update_data(button_url=url)
        data = await state.get_data()
    else:
        if raw.upper() != "ПІДТВЕРДИТИ":
            await state.clear(); await message.answer("❌ Скасовано.", reply_markup=admin_menu()); return
        data = await state.get_data()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ПОЧАТИ РОЗСИЛКУ", callback_data="adm:bcconfirm"),
        InlineKeyboardButton(text="❌ СКАСУВАТИ", callback_data="adm:cancelstate")
    ]])
    preview = data.get("text", "")
    if data.get("content_type") == "txt": await message.answer("👀 <b>ПРЕВ'Ю:</b>\n\n" + preview, reply_markup=kb)
    elif data.get("content_type") == "photo": await message.answer_photo(data["media_id"], caption="👀 <b>ПРЕВ'Ю:</b>\n" + preview, reply_markup=kb)
    else: await message.answer_video(data["media_id"], caption="👀 <b>ПРЕВ'Ю:</b>\n" + preview, reply_markup=kb)


@router.callback_query(F.data == "adm:bcconfirm")
async def adm_bc_confirm(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Config):
    if not admin_only(callback, config): return
    data = await state.get_data()
    from database.models import Broadcast
    bc = Broadcast(content_type=data["content_type"], text=data.get("text", ""), media_id=data.get("media_id"), button_text=data.get("button_text"), button_url=data.get("button_url"))
    session.add(bc); await session.commit(); await session.refresh(bc)
    result = await session.execute(select(User.telegram_id).where(User.is_blocked.is_(False)))
    user_ids = [row[0] for row in result.all()]
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    markup = None
    if data.get("button_text") and data.get("button_url"):
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=data["button_text"], url=data["button_url"])]] )
    sent = delivered = failed = 0
    delay = config.broadcast_delay
    await callback.message.edit_text("📢 <b>Розсилку запущено…</b>")
    for tg_id in user_ids:
        sent += 1
        try:
            if data["content_type"] == "txt":
                await bot.send_message(tg_id, data.get("text", ""), reply_markup=markup)
            elif data["content_type"] == "photo":
                await bot.send_photo(tg_id, data["media_id"], caption=data.get("text", ""), reply_markup=markup)
            else:
                await bot.send_video(tg_id, data["media_id"], caption=data.get("text", ""), reply_markup=markup)
            delivered += 1
        except Exception as exc:
            failed += 1
            text_exc = str(exc)
            if "bot was blocked" in text_exc.lower():
                user = await session.scalar(select(User).where(User.telegram_id == tg_id))
                if user: user.is_blocked = True
        if sent % 50 == 0:
            await callback.message.edit_text(f"📢 Розсилка триває…\n\nВідправлено: <b>{sent}</b>\n✅ Доставлено: <b>{delivered}</b>\n❌ Помилки: <b>{failed}</b>")
        await asyncio.sleep(delay)
    bc.sent_count = sent; bc.delivered_count = delivered; bc.failed_count = failed
    await session.commit()
    await add_admin_log(session, callback.from_user.id, "broadcast", None, f"id={bc.id}; sent={sent}; delivered={delivered}; failed={failed}")
    await state.clear()
    await callback.message.edit_text(f"✅ <b>РОЗСИЛКУ ЗАВЕРШЕНО</b>\n\n📢 Відправлено: <b>{sent}</b>\n✅ Доставлено: <b>{delivered}</b>\n❌ Помилки: <b>{failed}</b>", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:logs")
async def adm_logs(callback: CallbackQuery, session: AsyncSession, config: Config):
    if not admin_only(callback, config): return
    result = await session.execute(select(AdminLog).order_by(AdminLog.created_at.desc()).limit(30))
    logs = result.scalars().all()
    rows = ["📜 <b>ЛОГИ АДМІНІСТРАТИВНИХ ОПЕРАЦІЙ</b>"]
    for log in logs:
        date = log.created_at.strftime("%d.%m %H:%M") if log.created_at else ""
        rows.append(f"{date} • <b>{escape(log.action)}</b> • {log.admin_telegram_id} • {escape(log.details)}")
    await callback.message.edit_text("\n".join(rows), reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:cancelstate")
async def adm_cancel_state(callback: CallbackQuery, state: FSMContext, config: Config):
    if not admin_only(callback, config): return
    await state.clear(); await callback.message.edit_text("❌ <b>Скасовано.</b>", reply_markup=admin_menu()); await callback.answer()
