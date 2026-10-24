"""
BalanceGuard — контроль баланса (плагин для FunPay Cardinal v0.1.17+)

💳 Баланс FunPay меняется тихо: возврат списал деньги, вывод не туда,
а доступный остаток закончился в самый неподходящий момент. Плагин
периодически сверяет баланс и сразу сообщает в Telegram о том, что
требует вашего внимания.

Возможности:
 • Текущий баланс по всем валютам: общий и доступный к выводу.
 • Динамика общего баланса за последние 24 часа.
 • Алерт, когда доступный остаток опустился ниже порога (с гистерезисом —
   без потока повторных уведомлений, и с сообщением «баланс снова в норме»).
 • Алерт при неожиданном уменьшении общего баланса: возврат, вывод средств
   или доступ к аккаунту у кого-то ещё.
 • Опциональный алерт о поступлениях на баланс.
 • Все запросы к FunPay — в отдельном потоке, поллинг Telegram не блокируется.

Команды Telegram:
 • /balance — баланс, динамика за 24 часа, время обновления.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Настройки: storage/plugins/balance_guard.cfg (перечитываются на лету).
Данные: storage/plugins/balance_guard.json (замеры и время алертов).
Автор: yungspider
"""

from __future__ import annotations

import configparser
import datetime
import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import FunPayAPI
from telebot.types import Message
from tg_bot import utils as tg_utils
from locales.localizer import Localizer

logger = logging.getLogger("FPC.BalanceGuard")
localizer = Localizer()
_ = localizer.translate

# ═══════════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ═══════════════════════════════════
NAME = "BalanceGuard (Balance Watchdog & Alerts)"
VERSION = "1.1.0"
DESCRIPTION = ("Контроль баланса FunPay: общий и доступный остаток по всем валютам, динамика за 24 часа. " \
               "Предупреждает о низком доступном балансе, неожиданном списании (возврат, вывод, чужой доступ) и " \
               "о поступлениях. Настройки: storage/plugins/balance_guard.cfg. Команда: /balance")
CREDITS = "yungspider"
UUID = "c30e9774-3767-4f2a-b497-94e32cdf9531"
SETTINGS_PAGE = False

# ═══════════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════════
STORAGE_FILE = "storage/plugins/balance_guard.json"
CONFIG_FILE = "storage/plugins/balance_guard.cfg"
MAX_HISTORY = 400
"""Сколько замеров баланса хранить для расчета изменения за 24 часа."""

CURRENCIES = ("RUB", "USD", "EUR")
CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}

DEFAULT_CONFIG_TEXT = """[BalanceGuard]
; Включен ли плагин (1 / 0).
enabled = 1
; Интервал проверки баланса (минуты). Меньше - чаще запросы к FunPay.
checkInterval = 15
; Валюта, за доступным остатком которой следить (RUB / USD / EUR).
currency = RUB
; Порог доступного баланса: ниже этой суммы придет предупреждение.
minAvailable = 500
; Предупреждать ли об уменьшении общего баланса (1 / 0).
dropAlert = 1
; Насколько сильно должен уменьшиться баланс, чтобы считать это подозрительным (0 = любое уменьшение).
dropThreshold = 1
; Уведомлять ли о поступлениях на баланс (1 / 0).
notifyOnGrow = 0
; Минимальная сумма поступления для уведомления.
growThreshold = 1
; Пауза между повторными предупреждениями о низком балансе (минуты).
alertCooldown = 120
; Хранить ли историю замеров для показа динамики в /balance (1 / 0).
keepHistory = 1
"""

_lock = threading.RLock()
_cfg: dict = {}
_cfg_mtime: float = -1.0
_state: dict = {}
_cardinal: Cardinal | None = None
_thread_started = False


# ═════════════════════════════════════════ КОНФИГ ═════════════════════════════════════════
def load_config() -> dict:
    """Читает настройки плагина (создает файл при отсутствии, перечитывает при изменении)."""
    global _cfg, _cfg_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0.0
        except OSError:
            mtime = 0.0
        if _cfg and mtime == _cfg_mtime:
            return _cfg

        if not os.path.exists(CONFIG_FILE):
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_CONFIG_TEXT)
                mtime = os.path.getmtime(CONFIG_FILE)
                logger.info(f"[BalanceGuard] Создан файл настроек $YELLOW{CONFIG_FILE}$RESET")
            except Exception:
                logger.error("[BalanceGuard] Не удалось создать файл настроек.")
                logger.debug("TRACEBACK", exc_info=True)

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(CONFIG_FILE, encoding="utf-8")
        except Exception:
            logger.error("[BalanceGuard] Не удалось прочитать конфиг, использую значения по умолчанию.")
            logger.debug("TRACEBACK", exc_info=True)
            parser = configparser.ConfigParser(interpolation=None)
        if "BalanceGuard" not in parser:
            parser.read_string(DEFAULT_CONFIG_TEXT)
        section = parser["BalanceGuard"]

        def get_bool(name: str, default: bool) -> bool:
            try:
                return section.getboolean(name, fallback=default)
            except Exception:
                return default

        def get_float(name: str, default: float) -> float:
            try:
                return float(section.get(name, str(default)))
            except (ValueError, TypeError):
                return default

        def get_int(name: str, default: int) -> int:
            try:
                return int(section.get(name, str(default)))
            except (ValueError, TypeError):
                return default

        currency = str(section.get("currency", "RUB")).strip().upper()
        if currency not in CURRENCIES:
            currency = "RUB"

        _cfg = {
            "enabled": get_bool("enabled", True),
            "interval": max(1, get_int("checkInterval", 15)) * 60,
            "currency": currency,
            "min_available": get_float("minAvailable", 500.0),
            "drop_alert": get_bool("dropAlert", True),
            "drop_threshold": max(0.0, get_float("dropThreshold", 1.0)),
            "notify_on_grow": get_bool("notifyOnGrow", False),
            "grow_threshold": max(0.0, get_float("growThreshold", 1.0)),
            "alert_cooldown": max(1, get_int("alertCooldown", 120)) * 60,
            "keep_history": get_bool("keepHistory", True),
        }
        _cfg_mtime = mtime
        return _cfg


# ═══════════════════════════════════════ ХРАНИЛИЩЕ ═══════════════════════════════════════
def load_state() -> dict:
    """Загружает последнее состояние баланса и историю замеров."""
    global _state
    with _lock:
        if _state:
            return _state
        state = {"last": None, "history": [], "low_balance_alerted": False, "last_alert": 0}
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    state["last"] = raw.get("last")
                    state["history"] = raw.get("history") if isinstance(raw.get("history"), list) else []
                    state["low_balance_alerted"] = bool(raw.get("low_balance_alerted", False))
                    state["last_alert"] = float(raw.get("last_alert", 0) or 0)
            except Exception:
                logger.error("[BalanceGuard] Не удалось прочитать файл состояния.")
                logger.debug("TRACEBACK", exc_info=True)
        _state = state
        return _state


def save_state() -> None:
    """Атомарно сохраняет состояние плагина."""
    with _lock:
        if not _state:
            return
        try:
            os.makedirs(os.path.dirname(STORAGE_FILE), exist_ok=True)
            tmp_file = f"{STORAGE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(_state, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_file, STORAGE_FILE)
        except Exception:
            logger.error("[BalanceGuard] Не удалось сохранить состояние.")
            logger.debug("TRACEBACK", exc_info=True)


def balance_to_dict(balance: FunPayAPI.types.Balance) -> dict:
    """Преобразует объект баланса в словарь для хранения."""
    return {
        "t": int(time.time()),
        "total_rub": float(balance.total_rub or 0),
        "available_rub": float(balance.available_rub or 0),
        "total_usd": float(balance.total_usd or 0),
        "available_usd": float(balance.available_usd or 0),
        "total_eur": float(balance.total_eur or 0),
        "available_eur": float(balance.available_eur or 0),
    }


def get_totals(balance_dict: dict, currency: str) -> tuple[float, float]:
    """Возвращает (общий баланс, доступный баланс) для указанной валюты."""
    key = currency.lower()
    return float(balance_dict.get(f"total_{key}", 0.0)), float(balance_dict.get(f"available_{key}", 0.0))


def fmt(value: float) -> str:
    """Форматирует число с пробелом в качестве разделителя тысяч."""
    return f"{float(value):,.2f}".replace(",", " ")


def format_balance(balance_dict: dict) -> str:
    """Форматирует словарь баланса в многострочный HTML-текст."""
    lines = []
    for currency in CURRENCIES:
        total, available = get_totals(balance_dict, currency)
        if not total and not available:
            continue
        symbol = CURRENCY_SYMBOLS[currency]
        lines.append(f"{symbol} Общий: <b>{fmt(total)}</b>  |  Доступно: <b>{fmt(available)}</b>")
    return "\n".join(lines) if lines else "Баланс пуст."


def fetch_balance(c: Cardinal):
    """
    Запрашивает баланс FunPay (с несколькими попытками).

    :param c: объект кардинала.

    :return: объект Balance или None.
    """
    for attempt in range(2, -1, -1):
        try:
            balance = c.get_balance()
            if balance is not None:
                return balance
        except Exception:
            logger.warning(f"[BalanceGuard] Не удалось получить баланс. Осталось попыток: {attempt}")
            logger.debug("TRACEBACK", exc_info=True)
            time.sleep(2)
    return None


# ════════════════════════════════════ ЛОГИКА ПРОВЕРОК ════════════════════════════════════
def process_balance(c: Cardinal, balance: FunPayAPI.types.Balance, first_run: bool = False) -> None:
    """
    Сравнивает новый баланс с предыдущим и отправляет уведомления при необходимости.

    :param c: объект кардинала.
    :param balance: свежий баланс.
    :param first_run: первый замер после запуска (уведомления не отправляются).
    """
    cfg = load_config()
    state = load_state()
    new = balance_to_dict(balance)

    with _lock:
        old = state.get("last")
        state["last"] = new
        if cfg["keep_history"]:
            state["history"].append({"t": new["t"],
                                     "total": new[f"total_{cfg['currency'].lower()}"],
                                     "available": new[f"available_{cfg['currency'].lower()}"]})
            cutoff = int(time.time()) - 26 * 3600
            state["history"] = [i for i in state["history"] if int(i.get("t", 0)) >= cutoff][-MAX_HISTORY:]
    save_state()

    if old is None or first_run or not c.telegram:
        return

    currency = cfg["currency"]
    symbol = CURRENCY_SYMBOLS[currency]
    new_total, new_available = get_totals(new, currency)
    old_total, old_available = get_totals(old, currency)
    alerts = []

    # 1) Низкий доступный остаток (с гистерезисом, чтобы не спамить одним и тем же).
    if new_available < cfg["min_available"]:
        if not state.get("low_balance_alerted") or \
                time.time() - float(state.get("last_alert", 0)) > cfg["alert_cooldown"]:
            alerts.append(f"⚠️ <b>Низкий доступный баланс!</b>\n"
                          f"{symbol} Доступно: <b>{fmt(new_available)}</b> "
                          f"(порог: {fmt(cfg['min_available'])})")
            with _lock:
                state["low_balance_alerted"] = True
                state["last_alert"] = time.time()
            save_state()
    elif state.get("low_balance_alerted") and new_available >= cfg["min_available"]:
        with _lock:
            state["low_balance_alerted"] = False
        save_state()
        alerts.append(f"✅ <b>Баланс снова выше порога.</b>\n{symbol} Доступно: <b>{fmt(new_available)}</b>")

    # 2) Неожиданное уменьшение общего баланса.
    drop = round(old_total - new_total, 2)
    if cfg["drop_alert"] and drop > max(cfg["drop_threshold"], 0.001):
        alerts.append(f"🔻 <b>Списание с баланса!</b>\n{symbol} Было: <b>{fmt(old_total)}</b> → "
                      f"стало: <b>{fmt(new_total)}</b>\nСписано: <b>{fmt(drop)} {symbol}</b>\n"
                      f"<i>Возможен возврат по заказу или вывод средств. "
                      f"Если вы этого не делали - проверьте безопасность аккаунта.</i>")

    # 3) Поступление средств.
    grow = round(new_total - old_total, 2)
    if cfg["notify_on_grow"] and grow > max(cfg["grow_threshold"], 0.001):
        alerts.append(f"💰 <b>Поступление на баланс.</b>\n{symbol} Было: <b>{fmt(old_total)}</b> → "
                      f"стало: <b>{fmt(new_total)}</b>\nЗачислено: <b>{fmt(grow)} {symbol}</b>")

    for text in alerts:
        try:
            c.telegram.send_notification(text, notification_type=tg_utils.NotificationTypes.other)
        except Exception:
            logger.error("[BalanceGuard] Не удалось отправить уведомление в Telegram.")
            logger.debug("TRACEBACK", exc_info=True)


def check_loop(c: Cardinal, run_id: int) -> None:
    """Фоновый поток проверок баланса."""
    logger.info("[BalanceGuard] Поток контроля баланса запущен.")
    first = load_state().get("last") is None
    while True:
        if c.run_id != run_id:
            logger.info("[BalanceGuard] Поток контроля баланса остановлен.")
            return
        cfg = load_config()
        if cfg["enabled"]:
            try:
                balance = fetch_balance(c)
                if balance is not None:
                    process_balance(c, balance, first_run=first)
                    first = False
            except Exception:
                logger.error("[BalanceGuard] Ошибка при проверке баланса.")
                logger.debug("TRACEBACK", exc_info=True)
        for _ in range(cfg["interval"]):
            if c.run_id != run_id:
                return
            time.sleep(1)


# ════════════════════════════════════════ TELEGRAM ════════════════════════════════════════
def is_authorized(c: Cardinal, m: Message) -> bool:
    """Проверяет, есть ли отправитель в списке авторизованных пользователей ПУ."""
    if not c.telegram or m.from_user is None:
        return False
    return m.from_user.id in c.telegram.authorized_users


def send_balance(m: Message) -> None:
    """Обрабатывает команду /balance (запрос к FunPay выполняется в отдельном потоке)."""
    c = _cardinal
    if c is None or not c.telegram:
        return
    if not is_authorized(c, m):
        logger.warning(f"[BalanceGuard] Команда /balance от неавторизованного пользователя "
                       f"$YELLOW{m.from_user.id}$RESET проигнорирована.")
        return

    def worker():
        try:
            balance = fetch_balance(c)
            if balance is None:
                c.telegram.bot.send_message(m.chat.id, "❌ Не удалось получить баланс FunPay. "
                                                       "Попробуйте позже (подробности в логах).")
                return
            process_balance(c, balance)
            cfg = load_config()
            snapshot = balance_to_dict(balance)
            text = f"💳 <b>Баланс FunPay</b>\n\n{format_balance(snapshot)}"

            history = load_state().get("history") or []
            if len(history) > 1:
                currency = cfg["currency"]
                symbol = CURRENCY_SYMBOLS[currency]
                cutoff = int(time.time()) - 24 * 3600
                old_snapshots = [i for i in history if int(i.get("t", 0)) <= cutoff]
                reference = old_snapshots[-1] if old_snapshots else history[0]
                delta = float(snapshot[f"total_{currency.lower()}"]) - float(reference.get("total", 0))
                hours = max(1, round((time.time() - int(reference.get("t", time.time()))) / 3600))
                sign = "+" if delta >= 0 else "−"
                text += (f"\n\n📈 Изменение общего баланса ({currency}) за {hours} ч.: "
                         f"<b>{sign}{fmt(abs(delta))} {symbol}</b>")
            text += f"\n\n<i>Обновлено: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"
            c.telegram.bot.send_message(m.chat.id, text)
        except Exception:
            logger.error("[BalanceGuard] Ошибка при обработке команды /balance.")
            logger.debug("TRACEBACK", exc_info=True)
            try:
                c.telegram.bot.send_message(m.chat.id, "❌ Ошибка при получении баланса. Подробности в логах бота.")
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)

    try:
        c.telegram.bot.send_message(m.chat.id, "🔄 Запрашиваю баланс FunPay...")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
    threading.Thread(target=worker, daemon=True, name="BalanceGuardRequest").start()


# ═════════════════════════════════════ ИНИЦИАЛИЗАЦИЯ ═════════════════════════════════════
def pre_init(c: Cardinal) -> None:
    """Регистрирует команду /balance и переводы."""
    global _cardinal
    _cardinal = c
    load_config()
    load_state()

    for language, text in (("ru", "Текущий баланс FunPay"),
                           ("en", "Current FunPay balance"),
                           ("uk", "Поточний баланс FunPay")):
        try:
            localizer.add_translation(UUID, "cmd_balance", text, language)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    if not c.telegram:
        logger.info("[BalanceGuard] Telegram отключен, команда /balance не зарегистрирована.")
        return
    c.telegram.msg_handler(send_balance, commands=["balance"])
    c.add_telegram_commands(UUID, [("balance", f"{UUID}_cmd_balance", True)])
    logger.info("[BalanceGuard] Команда $YELLOW/balance$RESET зарегистрирована.")


def post_start(c: Cardinal) -> None:
    """Запускает фоновый поток контроля баланса."""
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    threading.Thread(target=check_loop, args=(c, c.run_id), daemon=True, name="BalanceGuardLoop").start()


def on_delete(c: Cardinal, call) -> None:
    """Удаляет файлы плагина при его удалении через ПУ Telegram."""
    for file in (STORAGE_FILE, CONFIG_FILE, f"{STORAGE_FILE}.tmp"):
        try:
            if os.path.exists(file):
                os.remove(file)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
    try:
        c.telegram.bot.answer_callback_query(call.id, "Файлы плагина Balance Guard удалены.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_START = [post_start]
BIND_TO_DELETE = on_delete
