#!/usr/bin/env python3
"""
fetch_indices.py
════════════════
Парсит открытые источники фрахтовых индексов и сохраняет данные в data.json.
Запускается автоматически GitHub Actions:
  - Понедельник  07:00 UTC (10:00 МСК)
  - Четверг      14:00 UTC (17:00 МСК)

Маршруты:
  1. Drewry WCI composite           — drewry.co.uk
  2. WCI Shanghai → Rotterdam       — drewry.co.uk
  3. SCFI composite                 — m.stockq.org
  4. Южная Америка → Роттердам     — Drewry WCI SAM-EUR proxy
  5. Китай → Владивосток            — teustat.ru (primary) / IACI Drewry (fallback)
  6. Китай → Санкт-Петербург        — расчётный (= Vlad × 5.2)
  7. USD / RUB                      — open.er-api.com / frankfurter.app
"""

import json, re, time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Настройки ────────────────────────────────────────────────────────────────
DATA_FILE = Path("data.json")
MAX_WEEKS = 20
TIMEOUT   = 15
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ── Загрузка текущего data.json ──────────────────────────────────────────────
def load_existing() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "updated": "",
        "usd_rub": None,
        "indices": {
            "wci_composite":  {"dates": [], "values": []},
            "wci_rotterdam":  {"dates": [], "values": []},
            "scfi_composite": {"dates": [], "values": []},
            "sam_rotterdam":  {"dates": [], "values": []},   # ЮАМ → Роттердам
            "china_vlad":     {"dates": [], "values": []},   # Китай → Владивосток
            "spb_est":        {"dates": [], "values": []},   # Китай → СПб (расчёт)
        }
    }

# ── Вспомогательные ──────────────────────────────────────────────────────────
def push_point(series: dict, date_str: str, value: float):
    """Добавляет точку, избегая дублей по дате."""
    if date_str in series["dates"]:
        series["values"][series["dates"].index(date_str)] = value
        return
    series["dates"].append(date_str)
    series["values"].append(value)
    if len(series["dates"]) > MAX_WEEKS:
        series["dates"]  = series["dates"][-MAX_WEEKS:]
        series["values"] = series["values"][-MAX_WEEKS:]

def today_ddmm() -> str:
    return datetime.now(timezone.utc).strftime("%d.%m")

def get(url, **kw) -> requests.Response:
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)

# ════════════════════════════════════════════════════════════════════════════
# 1. SCFI composite — stockq.org
# ════════════════════════════════════════════════════════════════════════════
def fetch_scfi_composite(data: dict):
    url = "https://m.stockq.org/index/SCFI.php"
    print(f"[SCFI] {url}")
    try:
        r = get(url); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        count = 0
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                m = re.match(r"(\d{4})/(\d{2})/(\d{2})", cells[0].get_text(strip=True))
                if m:
                    try:
                        val = float(cells[1].get_text(strip=True).replace(",", ""))
                        push_point(data["indices"]["scfi_composite"],
                                   f"{m.group(3)}.{m.group(2)}", val)
                        count += 1
                        if count >= MAX_WEEKS: break
                    except ValueError:
                        pass
        print(f"[SCFI] {count} точек")
    except Exception as e:
        print(f"[SCFI] ERROR: {e}")

# ════════════════════════════════════════════════════════════════════════════
# 2. Drewry WCI composite + Shanghai→Rotterdam
# ════════════════════════════════════════════════════════════════════════════
def fetch_wci(data: dict):
    url = "https://www.drewry.co.uk/logistics-executive-briefing"
    print(f"[WCI]  {url}")
    try:
        r = get(url); r.raise_for_status()
        text = r.text
        d_str = today_ddmm()

        m = re.search(r"WCI\).*?(?:to|at)\s+\$([0-9,]+)\s+per\s+40ft",
                      text, re.IGNORECASE | re.DOTALL)
        if m:
            val = float(m.group(1).replace(",", ""))
            push_point(data["indices"]["wci_composite"], d_str, val)
            print(f"[WCI]  composite = {val}")

        m2 = re.search(r"[Ss]hanghai.{0,30}[Rr]otterdam.*?\$([0-9,]+)",
                       text, re.DOTALL)
        if m2:
            val2 = float(m2.group(1).replace(",", ""))
            push_point(data["indices"]["wci_rotterdam"], d_str, val2)
            print(f"[WCI]  rotterdam = {val2}")

    except Exception as e:
        print(f"[WCI]  ERROR: {e}")

# ════════════════════════════════════════════════════════════════════════════
# 3. Южная Америка → Роттердам
#    Primary:  Drewry briefing (SAM-EUR паттерн)
#    Fallback: WCI Rotterdam × 0.72 (исторический коэффициент SAM/EUR Drewry)
# ════════════════════════════════════════════════════════════════════════════
def fetch_sam_rotterdam(data: dict):
    d_str = today_ddmm()
    found = False

    # Попытка 1: Drewry briefing
    try:
        r = get("https://www.drewry.co.uk/logistics-executive-briefing")
        r.raise_for_status()
        text = r.text
        for pat in [
            r"[Ss]outh\s*[Aa]merica.{0,80}(?:[Rr]otterdam|[Ee]urope).{0,40}\$([0-9,]{3,6})",
            r"(?:[Rr]otterdam|[Ee]urope).{0,80}[Ss]outh\s*[Aa]merica.{0,40}\$([0-9,]{3,6})",
            r"[Ss]antos.{0,80}\$([0-9,]{3,6})",
        ]:
            m = re.search(pat, text, re.DOTALL)
            if m:
                val = float(m.group(1).replace(",", ""))
                if 400 <= val <= 8000:
                    push_point(data["indices"]["sam_rotterdam"], d_str, val)
                    print(f"[SAM-ROT] drewry = {val}")
                    found = True
                    break
    except Exception as e:
        print(f"[SAM-ROT] drewry ERROR: {e}")

    # Fallback: WCI Rotterdam × 0.72
    if not found:
        rot = data["indices"]["wci_rotterdam"]["values"]
        if rot:
            val = round(rot[-1] * 0.72)
            push_point(data["indices"]["sam_rotterdam"], d_str, float(val))
            print(f"[SAM-ROT] fallback rot×0.72 = {val}")

# ════════════════════════════════════════════════════════════════════════════
# 4. Китай → Владивосток — teustat.ru (primary) + IACI Drewry (fallback)
# ════════════════════════════════════════════════════════════════════════════
def fetch_china_vlad(data: dict):
    """
    Китай → Владивосток.

    Источники (по приоритету):
      1. msri.cn  — Marine Silk Road Index / NCFI (Ningbo Shipping Exchange)
                    Открытый сайт, еженедельно, охватывает линию Россия/Владивосток.
                    Ищем строку «Russia» / «Vladivostok» / «俄罗斯» в таблице индексов.
      2. nbse.net.cn — Ningbo Shipping Exchange (запасной URL того же NCFI)
      3. teustat.ru   — российский агрегатор (SPA, пробуем как бонус)
      4. Drewry IACI × 1.6 — последний резерв
    """
    d_str = today_ddmm()
    found = False
    VLAD_MIN, VLAD_MAX = 800, 5000   # реальный диапазон USD/FEU Китай→Влад

    # ══════════════════════════════════════════════════════════════════════
    # 1. msri.cn  — Marine Silk Road Index (NCFI детальная таблица)
    # ══════════════════════════════════════════════════════════════════════
    msri_urls = [
        # Главная с таблицей индексов
        "http://www.msri.cn/",
        "http://www.msri.cn/en/",
        # Прямые страницы NCFI
        "http://www.msri.cn/msrishow.aspx?id=NCFI",
        "http://www.msri.cn/freightindex/ncfi",
        "http://www.msri.cn/index/ncfi",
        # API-эндпоинты (пробуем)
        "http://www.msri.cn/api/ncfi/latest",
        "http://www.msri.cn/api/freight/ncfi",
        # Версия nbse.net.cn
        "http://www.nbse.net.cn/col/pands/index/NCFI.html",
        "http://www.nbse.net.cn/ncfi/",
    ]

    for url in msri_urls:
        if found: break
        try:
            print(f"[VLAD]  msri → {url}")
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code not in (200, 201):
                print(f"[VLAD]  HTTP {r.status_code}")
                continue

            ct = r.headers.get("Content-Type", "")

            # ── JSON ──────────────────────────────────────────────────────
            if "json" in ct:
                raw = json.dumps(r.json(), ensure_ascii=False)
                print(f"[VLAD]  msri JSON preview: {raw[:400]}")
                # Ищем числа рядом с Russia/Vladivostok/俄罗斯
                for pat in [
                    r"(?:[Rr]ussia|[Vv]ladivostok|俄罗斯|VVO)[^\d]{0,60}?(\d{3,5}(?:\.\d+)?)",
                    r"(\d{3,5}(?:\.\d+)?)[^\d]{0,60}?(?:[Rr]ussia|[Vv]ladivostok|俄罗斯)",
                ]:
                    m = re.search(pat, raw)
                    if m:
                        val = float(m.group(1))
                        if VLAD_MIN <= val <= VLAD_MAX:
                            push_point(data["indices"]["china_vlad"], d_str, val)
                            print(f"[VLAD]  msri JSON = {val}")
                            found = True; break

            # ── HTML ──────────────────────────────────────────────────────
            else:
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(" ", strip=True)
                print(f"[VLAD]  msri HTML snippet: {text[:500]}")

                # Паттерны: Russia/Vladivostok/VVO рядом с числом
                for pat in [
                    r"(?:[Rr]ussia|[Vv]ladivostok|VVO|俄罗斯)[^\d]{0,80}?(\d[\d\s,\.]{2,8})",
                    r"(\d[\d\s,\.]{2,8})[^\d]{0,80}?(?:[Rr]ussia|[Vv]ladivostok|VVO|俄罗斯)",
                ]:
                    m = re.search(pat, text)
                    if m:
                        raw_n = re.sub(r"[\s,]", "", m.group(1))
                        try:
                            val = float(raw_n)
                            if VLAD_MIN <= val <= VLAD_MAX:
                                push_point(data["indices"]["china_vlad"], d_str, val)
                                print(f"[VLAD]  msri HTML = {val}")
                                found = True; break
                        except ValueError:
                            pass

                # Таблицы
                if not found:
                    for table in soup.find_all("table"):
                        for row in table.find_all("tr"):
                            row_t = row.get_text(" ", strip=True)
                            if re.search(r"russia|vladivostok|vvo|俄罗斯", row_t, re.IGNORECASE):
                                for n in re.findall(r"\b(\d{3,5}(?:\.\d+)?)\b", row_t):
                                    val = float(n)
                                    if VLAD_MIN <= val <= VLAD_MAX:
                                        push_point(data["indices"]["china_vlad"], d_str, val)
                                        print(f"[VLAD]  msri table = {val}")
                                        found = True; break
                            if found: break
                        if found: break

        except requests.exceptions.ConnectionError:
            print(f"[VLAD]  msri conn error: {url}")
        except requests.exceptions.Timeout:
            print(f"[VLAD]  msri timeout: {url}")
        except Exception as e:
            print(f"[VLAD]  msri error ({url}): {e}")

    # ══════════════════════════════════════════════════════════════════════
    # 2. teustat.ru (SPA — пробуем, но скорее всего не сработает)
    # ══════════════════════════════════════════════════════════════════════
    if not found:
        try:
            print("[VLAD]  teustat.ru → https://teustat.ru/")
            r = requests.get("https://teustat.ru/", headers=HEADERS, timeout=10)
            if r.status_code == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    raw = json.dumps(r.json(), ensure_ascii=False).lower()
                    print(f"[VLAD]  teustat JSON: {raw[:300]}")
                    for n in re.findall(r':\s*(\d{3,5}(?:\.\d+)?)', raw):
                        val = float(n)
                        if VLAD_MIN <= val <= VLAD_MAX:
                            push_point(data["indices"]["china_vlad"], d_str, val)
                            print(f"[VLAD]  teustat = {val}")
                            found = True; break
                else:
                    soup = BeautifulSoup(r.text, "html.parser")
                    snippet = soup.get_text(" ", strip=True)[:600]
                    print(f"[VLAD]  teustat HTML: {snippet}")
                    # Если сайт — SPA, здесь будет пустой контент
        except Exception as e:
            print(f"[VLAD]  teustat: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # 3. Fallback: Drewry IACI × 1.6
    #    IACI ~$1 114 × 1.6 ≈ $1 782 — близко к рыночным $1 800
    # ══════════════════════════════════════════════════════════════════════
    if not found:
        print("[VLAD]  msri/teustat недоступны → fallback IACI × 1.6")
        try:
            r = get("https://www.drewry.co.uk/logistics-executive-briefing")
            r.raise_for_status()
            m = re.search(r"IACI\).*?(?:to|at)\s+\$([0-9,]+)",
                          r.text, re.IGNORECASE | re.DOTALL)
            if m:
                iaci = float(m.group(1).replace(",", ""))
                val  = round(iaci * 1.6)
                push_point(data["indices"]["china_vlad"], d_str, float(val))
                print(f"[VLAD]  IACI {iaci} × 1.6 = {val}")
                found = True
        except Exception as e:
            print(f"[VLAD]  IACI ERROR: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # СПб = Vlad × 4.1  (скорректированный коэффициент: $1800 × 4.1 ≈ $7400)
    # ══════════════════════════════════════════════════════════════════════
    vlad_vals = data["indices"]["china_vlad"]["values"]
    if vlad_vals:
        spb = round(vlad_vals[-1] * 4.1)
        push_point(data["indices"]["spb_est"], d_str, float(spb))
        print(f"[SPB]   estimated (vlad × 4.1) = {spb}")

# ════════════════════════════════════════════════════════════════════════════
# 5. USD/RUB
# ════════════════════════════════════════════════════════════════════════════
def fetch_usd_rub(data: dict):
    print("[FX]   USD/RUB")
    try:
        r = get("https://open.er-api.com/v6/latest/USD")
        r.raise_for_status()
        d = r.json()
        if d.get("result") == "success" and "RUB" in d.get("rates", {}):
            rate = round(d["rates"]["RUB"], 2)
            data["usd_rub"] = rate
            print(f"[FX]   {rate} (open.er-api.com)")
            return
    except Exception as e:
        print(f"[FX]   open.er-api ERROR: {e}")
    try:
        r2 = get("https://api.frankfurter.app/latest?from=USD&to=RUB")
        r2.raise_for_status()
        d2 = r2.json()
        if "RUB" in d2.get("rates", {}):
            rate = round(d2["rates"]["RUB"], 2)
            data["usd_rub"] = rate
            print(f"[FX]   {rate} (frankfurter.app)")
            return
    except Exception as e:
        print(f"[FX]   frankfurter ERROR: {e}")
    print("[FX]   Не удалось получить курс — сохраняется предыдущее значение")

# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print(f"Freight Index Fetcher  {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    data = load_existing()

    fetch_usd_rub(data);        time.sleep(1)
    fetch_scfi_composite(data); time.sleep(1)
    fetch_wci(data);            time.sleep(1)
    fetch_sam_rotterdam(data);  time.sleep(1)
    fetch_china_vlad(data)

    data["updated"] = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅  {DATA_FILE} сохранён · {data['updated']}")
    print("\n── Последние значения ──────────────────────────────")
    for key, s in data["indices"].items():
        if s["values"]:
            print(f"  {key:22s}: {s['values'][-1]:>8}  ({s['dates'][-1]})")
    print(f"  {'usd_rub':22s}: {data.get('usd_rub', '?')}")

if __name__ == "__main__":
    main()
