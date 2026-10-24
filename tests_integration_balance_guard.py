"""
Интеграционные тесты плагина balance_guard.py: загрузка НАСТОЯЩИМ объектом Cardinal.

В отличие от tests_plugin_balance_guard.py (моки), здесь проход по боевому пути:
Cardinal.load_plugins() -> add_handlers() -> run_handlers() -> toggle_plugin().
Проверяется, что хэндлеры попадают в нужные списки и получают plugin_uuid, что команды
и переводы регистрируются, что хэндлеры выключенного плагина не вызываются и что в логах
бота нет ни одной ошибки. Сетевые объекты подменены заглушками.

Запуск из корня FunPayCardinal:
    cp tests_plugin_balance_guard.py tests_integration_balance_guard.py /path/to/FunPayCardinal/
    cd /path/to/FunPayCardinal && python3 tests_integration_balance_guard.py

или, не выходя из папки репозитория:
    FPC_PATH=/opt/FunPayCardinal python3 tests_integration_balance_guard.py
"""

import datetime
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tests_plugin_balance_guard as st  # noqa: E402  (он же находит Кардинала и готовит окружение)

from configparser import ConfigParser  # noqa: E402

from FunPayAPI import types  # noqa: E402
from FunPayAPI.updater.events import NewOrderEvent, OrderStatusChangedEvent, NewMessageEvent  # noqa: E402
from cardinal import Cardinal  # noqa: E402

PLUGIN_UUIDS = st.PLUGIN_UUIDS
PLUGIN_COMMANDS = {
    "sales_analytics.py": "stats",
    "balance_guard.py": "balance",
    "health_monitor.py": "health",
    "review_reminder.py": "remind",
    "stock_watcher.py": "stock",
}
COMMAND_HEADERS = {
    "stats": "Статистика продаж",
    "balance": "Баланс FunPay",
    "health": "Состояние FunPay Cardinal",
    "remind": "Напоминания об отзывах",
    "stock": "Остатки товаров",
}

MAIN_CFG_TEMPLATE = """[FunPay]
golden_key: selftest_key
user_agent: Mozilla/5.0 (SelfTest)
autoRaise: 1
autoResponse: 1
autoDelivery: 1
multiDelivery: 0
autoRestore: 1
autoDisable: 1
oldMsgGetMode: 0
keepSentMessagesUnread: 0
locale: ru

[Telegram]
enabled: 1
token: selftest_token
secretKeyHash: selftest_hash
proxy:
blockLogin: 0

[BlockList]
blockDelivery: 1
blockResponse: 1
blockNewMessageNotification: 0
blockNewOrderNotification: 0
blockCommandNotification: 0

[NewMessageView]
includeMyMessages: 1
includeFPMessages: 1
includeBotMessages: 1
notifyOnlyMyMessages: 0
notifyOnlyFPMessages: 0
notifyOnlyBotMessages: 0
showImageName: 0

[Greetings]
ignoreSystemMessages: 0
onlyNewChats: 1
sendGreetings: 1
greetingsText: Привет, $username!
greetingsCooldown: 2

[OrderConfirm]
watermark: 1
sendReply: 1
replyText: Спасибо за подтверждение заказа!

[ReviewReply]
star1Reply: 0
star2Reply: 0
star3Reply: 0
star4Reply: 0
star5Reply: 0
star1ReplyText:
star2ReplyText:
star3ReplyText:
star4ReplyText:
star5ReplyText:

[Proxy]
enable: 0
proxy:
check: 0

[Other]
watermark:
requestsDelay: 2
language: ru
"""

ERROR_RECORDS = []


class LogCatcher(logging.Handler):
    """Ловит ошибки в логах: run_handlers() глотает исключения плагинов и пишет их в лог."""

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            ERROR_RECORDS.append(f"{record.name}: {record.getMessage()}")


def build_main_config() -> tuple:
    os.makedirs("configs", exist_ok=True)
    path = "configs/_main_selftest.cfg"
    with open(path, "w", encoding="utf-8") as f:
        f.write(MAIN_CFG_TEMPLATE)
    from Utils import config_loader as cfg_loader
    return cfg_loader.load_main_config(path), path


def main():
    if not st.prepare():
        return 2
    catcher = LogCatcher()
    for logger_name in ("FPC", "TGBot", "main", "FunPayAPI"):
        logger = logging.getLogger(logger_name)
        logger.addHandler(catcher)
        logger.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(catcher)

    cfg_path = None
    try:
        present = st.detect_plugins()
        print(f"Плагины в plugins/: {', '.join(present) or 'нет'}")
        if not present:
            print("Тестировать нечего.")
            return 1
        expected_uuids = {PLUGIN_UUIDS[i] for i in present}

        os.makedirs("storage/products", exist_ok=True)
        MAIN_CFG, cfg_path = build_main_config()
        cardinal = Cardinal(MAIN_CFG, ConfigParser(), ConfigParser(), ConfigParser(), "0.0.0-selftest")

        # подменяем все, что могло бы сделать реальный сетевой запрос
        cardinal.account = st.FakeAccount()
        cardinal.telegram = st.FakeTelegram()
        cardinal.profile = st.FakeProfile()
        cardinal.get_balance = lambda attempts=3: types.Balance(12500.50, 3200.00, 84.20, 84.20, 0.0, 0.0)
        cardinal.AD_CFG["Лот с товаром"] = {"productsFileName": "_selftest_full.txt", "disable": "0"}
        cardinal.AD_CFG["Пустой лот"] = {"productsFileName": "_selftest_empty.txt", "disable": "0"}
        with open("storage/products/_selftest_full.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(f"login{i}:pass{i}" for i in range(10)))
        with open("storage/products/_selftest_empty.txt", "w", encoding="utf-8") as f:
            f.write("")

        print("\n--- 1. Cardinal.load_plugins() ---")
        cardinal.load_plugins()
        st.check("все плагины набора зарегистрированы в cardinal.plugins",
                 expected_uuids.issubset(set(cardinal.plugins)), str(set(cardinal.plugins)))
        for file in present:
            uuid = PLUGIN_UUIDS[file]
            if uuid not in cardinal.plugins:
                continue
            data = cardinal.plugins[uuid]
            st.check(f"{file}: PluginData заполнен (name/version/path)",
                     bool(data.name) and bool(data.version) and data.path == f"plugins/{file}",
                     f"{data.name} {data.version} {data.path}")
            st.check(f"{file}: автор - yungspider", data.credits == "yungspider", data.credits)
            st.check(f"{file}: settings_page = False", data.settings_page is False)
            st.check(f"{file}: delete_handler привязан", callable(data.delete_handler))

        print("\n--- 2. Cardinal.add_handlers() ---")
        before = {name: len(lst) for name, lst in cardinal.handler_bind_var_names.items()}
        cardinal.add_handlers()
        after = {name: len(lst) for name, lst in cardinal.handler_bind_var_names.items()}
        added = {k: after[k] - before[k] for k in after if after[k] != before[k]}

        # ожидаемое число хэндлеров считаем из самих модулей плагинов
        modules = {file: cardinal.plugins[PLUGIN_UUIDS[file]].plugin for file in present
                   if PLUGIN_UUIDS[file] in cardinal.plugins}
        expected = {}
        for module in modules.values():
            for bind in st.KNOWN_BINDS:
                functions = getattr(module, bind, None)
                if functions:
                    expected[bind] = expected.get(bind, 0) + len(functions)
        st.check("хэндлеры плагинов добавлены в списки кардинала", bool(added), str(added))
        st.check("число хэндлеров совпадает с объявленным в плагинах", added == expected,
                 f"факт {added} != ожидание {expected}")

        tagged = []
        for name, lst in cardinal.handler_bind_var_names.items():
            for func in lst:
                if getattr(func, "plugin_uuid", None) in expected_uuids:
                    tagged.append(func.__name__)
        st.check("каждому хэндлеру плагина присвоен plugin_uuid",
                 len(tagged) == sum(expected.values()), f"{len(tagged)} vs {sum(expected.values())}")

        print("\n--- 3. Cardinal.run_handlers(pre_init) ---")
        cardinal.run_handlers(cardinal.pre_init_handlers, (cardinal,))
        commands = {uuid: sorted(cardinal.plugins[uuid].commands) for uuid in expected_uuids
                    if uuid in cardinal.plugins}
        expected_commands = {PLUGIN_UUIDS[f]: [PLUGIN_COMMANDS[f]] for f in modules}
        st.check("команды плагинов зарегистрированы", commands == expected_commands, str(commands))
        st.check("TG-хэндлеры команд зарегистрированы",
                 len(cardinal.telegram.registered_msg_handlers) == len(modules),
                 str(len(cardinal.telegram.registered_msg_handlers)))
        st.check("созданы файлы настроек плагинов",
                 all(os.path.exists(cardinal.plugins[PLUGIN_UUIDS[f]].plugin.CONFIG_FILE) for f in modules))

        from locales.localizer import Localizer
        ru_lang = Localizer().languages["ru"]
        translations_ok = all(hasattr(ru_lang, f"{PLUGIN_UUIDS[f]}_cmd_{PLUGIN_COMMANDS[f]}") for f in modules)
        st.check("переводы команд добавлены в локализатор", translations_ok)
        sample = list(modules)[0]
        key = f"{PLUGIN_UUIDS[sample]}_cmd_{PLUGIN_COMMANDS[sample]}"
        st.check("описание команды переводится локализатором",
                 Localizer().translate(key) != key, key)

        print("\n--- 4. Диспетчеризация событий через Cardinal.run_handlers() ---")
        order = st.make_order("900001", price=2500.0)
        event = NewOrderEvent("selftest-tag", order)
        event.config_section_name = "Лот с товаром"
        event.config_section_obj = cardinal.AD_CFG["Лот с товаром"]
        event.delivered = True
        event.goods_delivered = 1
        cardinal.run_handlers(cardinal.new_order_handlers, (cardinal, event))
        cardinal.run_handlers(cardinal.post_delivery_handlers, (cardinal, event))

        closed_order = st.make_order("900001", status=types.OrderStatuses.CLOSED, price=2500.0)
        cardinal.account.orders["900001"] = st.make_full_order("900001", types.OrderStatuses.CLOSED, None)
        cardinal.run_handlers(cardinal.order_status_changed_handlers,
                              (cardinal, OrderStatusChangedEvent("selftest-tag", closed_order)))
        cardinal.run_handlers(cardinal.new_message_handlers,
                              (cardinal, NewMessageEvent("selftest-tag", st.make_review_message())))

        # выдача по лоту с пустым товарным файлом - триггер для stock_watcher
        empty_event = NewOrderEvent("selftest-tag-empty", st.make_order("900004", price=100.0))
        empty_event.config_section_name = "Пустой лот"
        empty_event.config_section_obj = cardinal.AD_CFG["Пустой лот"]
        empty_event.delivered = True
        cardinal.run_handlers(cardinal.post_delivery_handlers, (cardinal, empty_event))
        time.sleep(2)

        if "sales_analytics.py" in modules:
            with open("storage/plugins/sales_analytics.json", encoding="utf-8") as f:
                stats = json.load(f)
            day = stats["days"].get(datetime.datetime.now().strftime("%Y-%m-%d"), {})
            st.check("sales_analytics: заказ учтен через реальный run_handlers", day.get("orders") == 1, str(day))
            st.check("sales_analytics: выручка 2500 RUB", day.get("revenue", {}).get("rub") == 2500.0, str(day))
            st.check("sales_analytics: имя лота из auto_delivery.cfg",
                     "Лот с товаром" in day.get("lots", {}), str(day.get("lots")))
            st.check("sales_analytics: заказ отмечен выданным",
                     stats["orders"]["900001"]["delivered"] is True, str(stats["orders"]))
            st.check("sales_analytics: отзыв учтен", day.get("reviews") == 1, str(day))
        if "health_monitor.py" in modules:
            health_module = modules["health_monitor.py"]
            st.check("health_monitor: событие заказа учтено", health_module._state["last_order_event"] > 0)
            st.check("health_monitor: событие сообщения учтено", health_module._state["last_message_event"] > 0)
        if "review_reminder.py" in modules:
            with open("storage/plugins/review_reminder.json", encoding="utf-8") as f:
                reminders = json.load(f)
            st.check("review_reminder: напоминание запланировано (заказ закрыт без отзыва)",
                     "900001" in reminders["pending"], str(reminders))
        if "stock_watcher.py" in modules:
            st.check("stock_watcher: алерт о пустом товарном файле",
                     any("ТОВАР ЗАКОНЧИЛСЯ" in t for t, _ in cardinal.telegram.notifications),
                     str(cardinal.telegram.notifications)[:300])

        print("\n--- 5. Команды Telegram через реальный Cardinal ---")
        for file in modules:
            command = PLUGIN_COMMANDS[file]
            cardinal.telegram.bot.messages.clear()
            handler = None
            for func, kwargs in cardinal.telegram.registered_msg_handlers:
                if command in kwargs.get("commands", []):
                    handler = func
                    break
            st.check(f"/{command}: хэндлер найден", handler is not None)
            if handler:
                handler(st.FakeTgMessage(f"/{command}"))
            time.sleep(2.5 if command == "balance" else 1.2)
            messages = cardinal.telegram.bot.messages
            st.check(f"/{command}: пришел ответ", bool(messages), str(messages))
            st.check(f"/{command}: заголовок «{COMMAND_HEADERS[command]}»",
                     any(COMMAND_HEADERS[command] in i for i in messages), str(messages)[:300])

        print("\n--- 6. Неавторизованный пользователь ---")
        cardinal.telegram.bot.messages.clear()
        for func, kwargs in cardinal.telegram.registered_msg_handlers:
            if kwargs.get("commands"):
                func(st.FakeTgMessage(f"/{kwargs['commands'][0]}", user_id=4242))
        st.check("команды от чужого пользователя проигнорированы", not cardinal.telegram.bot.messages,
                 str(cardinal.telegram.bot.messages)[:200])

        print("\n--- 7. Cardinal.toggle_plugin() ---")
        toggle_target = "sales_analytics.py" if "sales_analytics.py" in modules else list(modules)[0]
        uuid = PLUGIN_UUIDS[toggle_target]
        cardinal.toggle_plugin(uuid)
        st.check(f"{toggle_target}: плагин выключен", cardinal.plugins[uuid].enabled is False)
        if toggle_target == "sales_analytics.py":
            cardinal.run_handlers(cardinal.new_order_handlers,
                                  (cardinal, NewOrderEvent("selftest-tag2", st.make_order("900002", price=100.0))))
            with open("storage/plugins/sales_analytics.json", encoding="utf-8") as f:
                stats2 = json.load(f)
            st.check("хэндлеры выключенного плагина не выполняются", "900002" not in stats2["orders"],
                     str(list(stats2["orders"])))
        cardinal.toggle_plugin(uuid)
        st.check(f"{toggle_target}: плагин снова включен", cardinal.plugins[uuid].enabled is True)
        if toggle_target == "sales_analytics.py":
            cardinal.run_handlers(cardinal.new_order_handlers,
                                  (cardinal, NewOrderEvent("selftest-tag3", st.make_order("900003", price=100.0))))
            with open("storage/plugins/sales_analytics.json", encoding="utf-8") as f:
                stats3 = json.load(f)
            st.check("после включения хэндлеры снова работают", "900003" in stats3["orders"])

        print("\n--- 8. Ошибки в логах ---")
        real_errors = [i for i in ERROR_RECORDS if "TRACEBACK" not in i]
        st.check("в логах нет ошибок от плагинов", not real_errors, " | ".join(real_errors[:5]))
    finally:
        print("\n--- 9. Уборка ---")
        for path in [cfg_path, "storage/products/_selftest_full.txt", "storage/products/_selftest_empty.txt",
                     "storage/plugins/sales_analytics.json", "storage/plugins/balance_guard.json",
                     "storage/plugins/review_reminder.json", "storage/plugins/stock_watcher.json"]:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        st.cleanup()

    failed = [i for i in st.RESULTS if not i[1]]
    print("\n" + "=" * 70)
    print(f"Всего проверок: {len(st.RESULTS)}, пройдено: {len(st.RESULTS) - len(failed)}, "
          f"провалено: {len(failed)}")
    for name, _, extra in failed:
        print(f"  FAIL: {name} {extra}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
