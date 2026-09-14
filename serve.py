"""Публикует локально запущенного бота наружу через cloudflared — чтобы проверить Mini App
в Telegram с компьютера, не разворачивая ничего в облаке.

Запуск: сначала bot.py (он сам раздаёт app/ и API на APP_PORT), затем
    .venv\\Scripts\\python.exe serve.py

Telegram открывает Mini App только по HTTPS, поэтому нужен туннель. Скрипт ищет
tools\\cloudflared.exe, поднимает «quick tunnel» (без аккаунта Cloudflare), вылавливает
из его вывода адрес *.trycloudflare.com и пишет его в webapp_url.txt — оттуда его читает
бот (перезапуск не нужен, отправь /start). Адрес меняется при каждом запуске.

Для постоянной работы это не годится — см. README, раздел «Развёртывание».
"""

from __future__ import annotations

import re
import subprocess

from config import APP_PORT, BASE_DIR, WEBAPP_URL_FILE

CLOUDFLARED = BASE_DIR / "tools" / "cloudflared.exe"
LOG_FILE = BASE_DIR / "serve.log"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def run_tunnel() -> None:
    if not CLOUDFLARED.exists():
        print(f"[!] {CLOUDFLARED} не найден.")
        print("    Скачать (одна команда в PowerShell, из папки wallet):")
        print('    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/'
              'cloudflared-windows-amd64.exe" -OutFile "tools\\cloudflared.exe"')
        return
    proc = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--url", f"http://localhost:{APP_PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    print(f"[.] Поднимаю туннель cloudflared → http://localhost:{APP_PORT} (там должен работать bot.py)…")
    url = ""
    with LOG_FILE.open("w", encoding="utf-8") as log:
        for line in proc.stdout:
            log.write(line)
            log.flush()
            print("  cf |", line.rstrip()[:160])
            m = URL_RE.search(line)
            # api.trycloudflare.com — служебный адрес в логе, не наш туннель.
            if not url and m and not m.group(0).startswith("https://api."):
                url = m.group(0)
                WEBAPP_URL_FILE.write_text(url + "\n", encoding="utf-8")
                print()
                print(f"[+] Mini App доступен по адресу: {url}")
                print(f"    Записал в {WEBAPP_URL_FILE.name}. В боте отправь /start — появится кнопка.")
                print()
    code = proc.wait()
    print(f"[!] cloudflared завершился (код {code}). Полный вывод — в {LOG_FILE.name}.")


def main() -> None:
    try:
        run_tunnel()
    except KeyboardInterrupt:
        pass
    finally:
        if WEBAPP_URL_FILE.exists():
            WEBAPP_URL_FILE.unlink()
        print("Остановлено.")


if __name__ == "__main__":
    main()
