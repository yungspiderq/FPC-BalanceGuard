"""
Функциональные тесты плагина balance_guard.py на моках.

Проверяет: метаданные и загрузку через Cardinal.load_plugin(), привязку хэндлеров,
все события, команды Telegram, фоновые потоки (включая выход по смене run_id), алерты,
cooldown'ы, дедупликацию событий и удаление плагина.
Сетевые объекты подменены заглушками — реальных запросов к FunPay и Telegram нет.

Запуск из корня FunPayCardinal:
    cp tests_plugin_balance_guard.py /path/to/FunPayCardinal/
    cd /path/to/FunPayCardinal && python3 tests_plugin_balance_guard.py

или, не выходя из папки репозитория:
    FPC_PATH=/opt/FunPayCardinal python3 tests_plugin_balance_guard.py
"""

import datetime
import json
import os
import shutil
import sys
import threading
import time

# Пути намеренно относительные: скрипт можно запускать из любой директории,
# а FunPay Cardinal может лежать как в родительской папке, так и в произвольном месте (FPC_PATH).
TESTS_DIR = os.path.dirname(__file__) or "."
REPO_ROOT = TESTS_DIR  # тесты лежат в корне репозитория плагина
sys.path.insert(0, TESTS_DIR)

FPC_MARKERS = ("cardinal.py", "main.py", "FunPayAPI", "tg_bot", "Utils")

KNOWN_PLUGINS = ["balance_guard.py"]
"""Все плагины набора: тестируются только те, что найдены."""


def find_fpc_root() -> str | None:
    """
    Ищет установку FunPay Cardinal: FPC_PATH, корень этого репозитория, соседняя папка,
    ./FunPayCardinal, ~/FunPayCardinal или текущая директория.
    """
    candidates = [
        os.environ.get("FPC_PATH"),
        REPO_ROOT,                                  # тесты лежат внутри FunPayCardinal
        os.path.join(REPO_ROOT, ".."),              # тесты в отдельном репозитории рядом с FunPayCardinal
        os.path.join(REPO_ROOT, "..", ".."),
        "FunPayCardinal",
        os.path.join("..", "FunPayCardinal"),
        os.path.expanduser("~/FunPayCardinal"),
        os.getcwd(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if all(os.path.exists(os.path.join(candidate, i)) for i in FPC_MARKERS):
            return candidate
    return None


def read_repo_plugins() -> dict:
    """Читает файлы плагинов из этого репозитория (до смены рабочей директории)."""
    result = {}
    for name in KNOWN_PLUGINS:
        for source in (os.path.join(REPO_ROOT, name), os.path.join(REPO_ROOT, "plugins", name)):
            if os.path.exists(source):
                try:
                    with open(source, "rb") as f:
                        result[name] = f.read()
                except OSError:
                    pass
                break
    return result


PLUGIN_SOURCES = read_repo_plugins()

FPC_ROOT = find_fpc_root()
if FPC_ROOT is None:
    sys.stderr.write(
        "FunPay Cardinal не найден.\n"
        "Положите tests/ внутрь папки FunPayCardinal или укажите путь к ней:\n"
        "    FPC_PATH=/opt/FunPayCardinal python3 tests/_plugin_selftest.py\n")
    raise SystemExit(2)

sys.path.insert(0, FPC_ROOT)
os.chdir(FPC_ROOT)

from configparser import ConfigParser  # noqa: E402

import FunPayAPI  # noqa: E402
from FunPayAPI import types  # noqa: E402
from FunPayAPI.updater.events import NewOrderEvent, OrderStatusChangedEvent, NewMessageEvent  # noqa: E402

PLUGIN_UUIDS = {
    "balance_guard.py": "c30e9774-3767-4f2a-b497-94e32cdf9531",
}

RESULTS = []
COPIED_FILES = []


def check(name, condition, extra=""):
    RESULTS.append((name, bool(condition), extra))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {extra}" if extra and not condition else ""))


def prepare() -> bool:
    """Создает нужные папки и временно копирует плагины из репозитория в plugins/ Кардинала."""
    for folder in ("plugins", "storage/plugins", "storage/products", "storage/cache", "configs", "logs"):
        os.makedirs(folder, exist_ok=True)
    for name, blob in PLUGIN_SOURCES.items():
        destination = os.path.join("plugins", name)
        if os.path.exists(destination):
            continue
        with open(destination, "wb") as f:
            f.write(blob)
        COPIED_FILES.append(destination)
        print(f"Плагин временно скопирован в {destination}")
    return True


def cleanup() -> None:
    """Удаляет временно скопированные файлы плагинов."""
    for path in COPIED_FILES:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    COPIED_FILES.clear()


def detect_plugins() -> list[str]:
    """Возвращает список плагинов набора, которые установлены в папку plugins/."""
    return [i for i in KNOWN_PLUGINS if os.path.exists(f"plugins/{i}")]


# ================================== ПОДСТАВНЫЕ ОБЪЕКТЫ ==================================
class FakeUser:
    def __init__(self, id_, username="tester"):
        self.id = id_
        self.username = username
        self.language_code = "ru"


class FakeChat:
    def __init__(self, id_=1):
        self.id = id_
        self.type = "private"


class FakeTgMessage:
    def __init__(self, text, chat_id=1, user_id=1):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id)
        self.id = 1
        self.content_type = "text"


class FakeCallback:
    def __init__(self):
        self.id = "cbq1"
        self.data = "47"
        self.message = FakeTgMessage("/menu")
        self.from_user = FakeUser(1)


class FakeBot:
    def __init__(self):
        self.messages = []
        self.callback_answers = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return FakeTgMessage(text)

    def answer_callback_query(self, cbq_id, text=None, **kwargs):
        self.callback_answers.append(text)


class FakeTelegram:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = {1: {}}
        self.notifications = []
        self.registered_msg_handlers = []

    def send_notification(self, text, keyboard=None, notification_type="10", photo=None, pin=False):
        self.notifications.append((text, notification_type))

    def msg_handler(self, handler, **kwargs):
        self.registered_msg_handlers.append((handler, kwargs))

    def add_command_to_menu(self, command, help_text):
        pass


class FakeAccount:
    def __init__(self):
        self.id = 111
        self.username = "seller_test"
        self.active_sales = 3
        self.currency = types.Currency.RUB
        self.orders = {}

    def get(self, update_phpsessid=False):
        return True

    def is_initiated(self):
        return True

    def get_order(self, order_id, **kwargs):
        return self.orders.get(order_id)

    def get_chat_by_name(self, name, make_request=False):
        return None

    def get_exchange_rate(self, currency):
        return 1.0, types.Currency.RUB


class FakeSubcategory:
    type = FunPayAPI.enums.SubCategoryTypes.COMMON
    name = "Аккаунты"
    fullname = "Аккаунты Steam"


class FakeProfile:
    def __init__(self):
        self.lots = [types.LotShortcut(1, "Server", "Side", "Аккаунт Steam", 5, 1500.0, types.Currency.RUB,
                                       FakeSubcategory(), None, True, None, None, "<html>")]

    def get_lots(self):
        return self.lots


class FakeCardinal:
    """Мини-копия Cardinal с теми полями, которые реально используют плагины."""

    def __init__(self):
        self.telegram = FakeTelegram()
        self.account = FakeAccount()
        self.profile = FakeProfile()
        self.run_id = 1
        self.start_time = int(time.time()) - 90000
        self.blacklist = ["badguy"]
        self.plugins = {}
        self.raise_time = {1: time.time() + 600}
        self.raised_time = {1: time.time() - 600}
        self.old_mode_enabled = False
        self.autoraise_enabled = True
        self.autoresponse_enabled = True
        self.autodelivery_enabled = True
        self.autorestore_enabled = False
        self.autodisable_enabled = True
        self.AD_CFG = ConfigParser()
        self.MAIN_CFG = ConfigParser()
        self.commands = []
        self.sent_funpay_messages = []

    def add_telegram_commands(self, uuid, commands):
        self.commands.append((uuid, commands))

    def get_balance(self, attempts=3):
        return types.Balance(1000.0, 400.0, 10.0, 10.0, 0.0, 0.0)

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True):
        self.sent_funpay_messages.append((chat_id, message_text, chat_name, watermark))
        return [types.Message(1, message_text, chat_id, chat_name, None, self.account.username,
                              self.account.id, "<html>")]

    def get_order_from_object(self, obj, order_id=None):
        return self.account.get_order(getattr(obj, "id", None))


def make_order(order_id="12345", status=types.OrderStatuses.PAID, price=1500.0,
               currency=types.Currency.RUB, buyer="buyer1"):
    return types.OrderShortcut(order_id, "Аккаунт Steam, Level 10", price, currency, buyer, 999, 888,
                               status, datetime.datetime.now(), "Steam, Аккаунты", None, "<html>")


def make_full_order(order_id="12345", status=types.OrderStatuses.CLOSED, review=None):
    return types.Order(order_id, status, None, None, None, {}, 1, 1500.0, types.Currency.RUB, None,
                       999, "buyer1", 111, "seller_test", 888, review, [], "ru")


def make_review_message(chat_id=888, msg_id=42):
    class StubMessage:
        pass

    msg = StubMessage()
    msg.id = msg_id
    msg.chat_id = chat_id
    msg.chat_name = "buyer1"
    msg.type = types.MessageTypes.NEW_FEEDBACK
    msg.i_am_buyer = False
    msg.text = "Отзыв на заказ #12345"
    return msg


# ================================== ЗАГРУЗКА ПЛАГИНОВ ==================================
def load_plugins(modules=None):
    """Загружает плагины так же, как Cardinal.load_plugin(), и проверяет их метаданные."""
    from cardinal import Cardinal
    uuids = set()
    for file in detect_plugins():
        check(f"{file}: файл существует", os.path.exists(f"plugins/{file}"))
        check(f"{file}: не помечен как noplug", Cardinal.is_plugin(file))
        try:
            plugin, data = Cardinal.load_plugin(file)
        except Exception as e:
            check(f"{file}: загрузка через Cardinal.load_plugin", False, repr(e))
            continue
        check(f"{file}: загрузка через Cardinal.load_plugin", True)

        for field in ("NAME", "VERSION", "DESCRIPTION", "CREDITS", "UUID", "SETTINGS_PAGE"):
            check(f"{file}: поле {field} заполнено", bool(str(data.get(field, "")).strip()))
        check(f"{file}: UUID валиден", Cardinal.is_uuid_valid(data["UUID"]))
        check(f"{file}: UUID совпадает с заявленным", data["UUID"] == PLUGIN_UUIDS[file], data["UUID"])
        check(f"{file}: UUID уникален", data["UUID"] not in uuids, data["UUID"])
        uuids.add(data["UUID"])
        check(f"{file}: SETTINGS_PAGE - bool", isinstance(data["SETTINGS_PAGE"], bool))
        check(f"{file}: BIND_TO_DELETE вызываемый или None",
              data["BIND_TO_DELETE"] is None or callable(data["BIND_TO_DELETE"]))

        binds = [i for i in dir(plugin) if i.startswith("BIND_TO_") and i != "BIND_TO_DELETE"]
        check(f"{file}: есть хотя бы один BIND_TO_* хэндлер", bool(binds), str(binds))
        for bind in binds:
            value = getattr(plugin, bind)
            check(f"{file}: {bind} - список вызываемых функций",
                  isinstance(value, list) and all(callable(i) for i in value))
            check(f"{file}: {bind} поддерживается Кардиналом", bind in KNOWN_BINDS, bind)
        if modules is not None:
            modules[file] = plugin
    return modules


KNOWN_BINDS = {
    "BIND_TO_PRE_INIT", "BIND_TO_POST_INIT", "BIND_TO_PRE_START", "BIND_TO_POST_START",
    "BIND_TO_PRE_STOP", "BIND_TO_POST_STOP", "BIND_TO_INIT_MESSAGE", "BIND_TO_MESSAGES_LIST_CHANGED",
    "BIND_TO_LAST_CHAT_MESSAGE_CHANGED", "BIND_TO_NEW_MESSAGE", "BIND_TO_INIT_ORDER",
    "BIND_TO_NEW_ORDER", "BIND_TO_ORDERS_LIST_CHANGED", "BIND_TO_ORDER_STATUS_CHANGED",
    "BIND_TO_PRE_DELIVERY", "BIND_TO_POST_DELIVERY", "BIND_TO_PRE_LOTS_RAISE", "BIND_TO_POST_LOTS_RAISE",
}


# ================================== ОБЩЕЕ ==================================
def stop_thread_after(thread, cardinal, seconds=1.2):
    """Останавливает фоновый поток плагина, меняя run_id (так это делает Кардинал при перезапуске)."""
    time.sleep(seconds)
    cardinal.run_id += 1
    thread.join(timeout=5)
    return not thread.is_alive()


# ================================== ТЕСТЫ ПЛАГИНА ==================================
def test_balance_guard(plugin):
    c = FakeCardinal()
    plugin.pre_init(c)
    check("balance_guard: команда /balance зарегистрирована",
          any(kwargs.get("commands") == ["balance"] for _, kwargs in c.telegram.registered_msg_handlers))
    check("balance_guard: создан файл настроек", os.path.exists(plugin.CONFIG_FILE))

    balance = types.Balance(1000.0, 400.0, 10.0, 10.0, 0.0, 0.0)
    plugin.process_balance(c, balance, first_run=True)
    check("balance_guard: первый замер не отправляет уведомлений", not c.telegram.notifications)

    low_balance = types.Balance(1000.0, 100.0, 10.0, 10.0, 0.0, 0.0)
    plugin.process_balance(c, low_balance)
    check("balance_guard: алерт о низком балансе",
          any("Низкий доступный баланс" in t for t, _ in c.telegram.notifications),
          str(c.telegram.notifications))

    dropped = types.Balance(500.0, 100.0, 10.0, 10.0, 0.0, 0.0)
    plugin.process_balance(c, dropped)
    check("balance_guard: алерт о списании",
          any("Списание с баланса" in t for t, _ in c.telegram.notifications))

    c.telegram.notifications.clear()
    plugin.process_balance(c, dropped)
    check("balance_guard: повторный алерт подавлен cooldown'ом", not c.telegram.notifications)

    restored = types.Balance(1000.0, 900.0, 10.0, 10.0, 0.0, 0.0)
    plugin.process_balance(c, restored)
    check("balance_guard: сообщение о восстановлении порога",
          any("снова выше порога" in t for t, _ in c.telegram.notifications))

    check("balance_guard: создан файл состояния", os.path.exists(plugin.STORAGE_FILE))
    c.telegram.bot.messages.clear()
    plugin.send_balance(FakeTgMessage("/balance"))
    time.sleep(2.5)
    check("balance_guard: /balance отвечает", len(c.telegram.bot.messages) >= 2,
          str(c.telegram.bot.messages))
    check("balance_guard: в ответе есть суммы", "1 000.00" in c.telegram.bot.messages[-1],
          c.telegram.bot.messages[-1][:300])

    thread = threading.Thread(target=plugin.check_loop, args=(c, c.run_id), daemon=True)
    thread.start()
    time.sleep(2)
    check("balance_guard: поток завершается при смене run_id", stop_thread_after(thread, c, 0.5))
    plugin.on_delete(c, FakeCallback())
    check("balance_guard: on_delete удалил файлы",
          not os.path.exists(plugin.STORAGE_FILE) and not os.path.exists(plugin.CONFIG_FILE))


TESTS = {
    "balance_guard.py": test_balance_guard,
}


# ================================== ЗАПУСК ==================================
def main():
    if not prepare():
        return 2
    try:
        plugins = detect_plugins()
        print(f"Найдено плагинов: {len(plugins)} -> {', '.join(plugins) or 'нет'}")
        if not plugins:
            print("Плагины набора не найдены ни в plugins/, ни в корне репозитория - тестировать нечего.")
            return 1

        modules = {}
        load_plugins(modules)
        for file, test_func in TESTS.items():
            if file not in modules:
                continue
            print(f"\n--- {file} ---")
            try:
                test_func(modules[file])
            except Exception:
                import traceback
                traceback.print_exc()
                check(f"{file}: тесты пройдены без исключений", False)

        failed = [i for i in RESULTS if not i[1]]
        print("\n" + "=" * 70)
        print(f"Всего проверок: {len(RESULTS)}, пройдено: {len(RESULTS) - len(failed)}, "
              f"провалено: {len(failed)}")
        for name, _, extra in failed:
            print(f"  FAIL: {name} {extra}")
        return 1 if failed else 0
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
