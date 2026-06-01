# Telegram-бот для отслеживания акций MOEX

Личный Telegram-бот проверяет российские акции из `tickers.txt` через MOEX ISS API:

- кнопка `Проверить сейчас` показывает акции, которые сейчас выше предыдущего закрытия;
- команда `/report` вручную строит отчет по последнему закрытому торговому дню;
- ежедневный отчет отправляется в заданное время из `.env`.

Данные берутся из MOEX ISS. Бесплатные данные MOEX могут приходить с задержкой.

## 1. Создайте Telegram-бота через BotFather

1. Откройте Telegram и найдите `@BotFather`.
2. Отправьте команду `/newbot`.
3. Введите имя бота.
4. Введите username бота, который заканчивается на `bot`.
5. BotFather выдаст токен. Его нужно вставить в `.env` как `TELEGRAM_BOT_TOKEN`.

Никому не отправляйте токен и не публикуйте файл `.env`.

## 2. Подготовьте проект на Windows

Откройте PowerShell в папке проекта и выполните:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Если PowerShell запрещает активацию окружения, один раз выполните:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Затем снова активируйте окружение:

```powershell
.\.venv\Scripts\Activate.ps1
```

## 3. Заполните `.env`

Минимально нужно указать:

```dotenv
TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather
REPORT_TIME=23:00
TIMEZONE=Europe/Moscow
```

`TELEGRAM_CHAT_ID` можно оставить пустым. После команды `/start` бот сохранит ваш chat_id в `chat_id.txt` и покажет его в сообщении. Потом этот id можно вставить в `.env`:

```dotenv
TELEGRAM_CHAT_ID=123456789
```

Чтобы ограничить доступ только вашим Telegram-пользователем, укажите:

```dotenv
ALLOWED_USER_ID=123456789
```

Обычно для личного чата `ALLOWED_USER_ID` совпадает с вашим user id, а не всегда с chat_id группы.

## 4. Заполните `tickers.txt`

Укажите тикеры MOEX по одному на строку:

```text
SBER
LKOH
GAZP
MOEX
```

Пустые строки и комментарии после `#` игнорируются.

## 5. Запустите бота вручную

```powershell
python bot.py
```

Откройте Telegram, найдите своего бота и отправьте:

```text
/start
```

Бот покажет кнопку `Проверить сейчас` и ваш `chat_id`.

## 6. Проверьте кнопку

Нажмите `Проверить сейчас`. Бот отправит сообщение `Проверяю данные...`, затем покажет акции, которые сейчас выше предыдущего закрытия.

Если по отдельному тикеру данные получить не удалось, бот не остановится, а добавит блок `Не удалось получить данные`.

## 7. Ежедневный отчет

Время отчета задается в `.env`:

```dotenv
REPORT_TIME=23:00
TIMEZONE=Europe/Moscow
```

Отчет отправляется только пока процесс `python bot.py` запущен. Для отчета по закрытию дня бот использует исторические торговые данные MOEX, поэтому незавершенная дневная свеча не берется как закрытие дня.

Проверить отчет вручную можно командой:

```text
/report
```

## 8. Автозапуск на Windows

Простой вариант - создать файл `start_bot.ps1` рядом с проектом:

```powershell
Set-Location "C:\Users\admin\Desktop\test-codex"
.\.venv\Scripts\Activate.ps1
python bot.py
```

Затем откройте `Планировщик заданий` Windows:

1. Создайте простую задачу.
2. Триггер: `При входе в систему`.
3. Действие: `Запустить программу`.
4. Программа: `powershell.exe`.
5. Аргументы:

```text
-ExecutionPolicy Bypass -File "C:\Users\admin\Desktop\test-codex\start_bot.ps1"
```

После входа в Windows бот будет запускаться и сам отправлять ежедневный отчет в `REPORT_TIME`.

## 9. Как остановить бота

Если бот запущен в PowerShell, нажмите:

```text
Ctrl+C
```

Если бот запущен через Планировщик заданий, остановите задачу в Планировщике.

## 10. Логи

Логи пишутся в консоль и файл:

```text
bot.log
```

Файл `.env`, `chat_id.txt` и `bot.log` добавлены в `.gitignore`.
