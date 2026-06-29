#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневный мониторинг позиции абитуриента в конкурсных списках.

Ищет абитуриента по ЕДИНОМУ КОДУ (УИД с Госуслуг) во всех конкурсных группах
пяти вузов и формирует отчёт:
    вуз | код | специальность | сырая позиция | реальная позиция | бюджетных мест

Запуск: python abitur_monitor.py
Зависимости: см. requirements.txt
Настройка секретов (Telegram и т.д.): через переменные окружения, см. README.md
"""

from __future__ import annotations

import os
import re
import csv
import sys
import json
import time
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────────────────
#  КОНФИГ
# ──────────────────────────────────────────────────────────────────────────

# Единый код абитуриента (УИД с Госуслуг). Один на все вузы.
APPLICANT_ID = os.environ.get("APPLICANT_ID", "1212030")

# Учитывать только бюджетные «основные места»? (квоты обычно не нужны)
ONLY_BUDGET_MAIN = True

# Telegram (берутся из переменных окружения / GitHub Secrets)
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Запись в Google-таблицу (опционально). Если SHEETS_ENABLED=0 — пропускается.
SHEETS_ENABLED = os.environ.get("SHEETS_ENABLED", "0") == "1"
SHEETS_ID = os.environ.get("SHEETS_ID", "1rpEuEuczAnwd1N_eW9YVGSeADUi0LhJU6jLPDrvaZXs")
SHEETS_TAB = os.environ.get("SHEETS_TAB", "Результаты")
# путь к JSON сервис-аккаунта Google (см. README)
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDS", "service_account.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s"
)
log = logging.getLogger("monitor")


# ──────────────────────────────────────────────────────────────────────────
#  МОДЕЛЬ ДАННЫХ
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Record:
    vuz: str
    code: str = ""          # код специальности, напр. 09.03.01
    name: str = ""          # название направления
    form: str = ""          # очная / очно-заочная / заочная
    basis: str = ""         # бюджет / платно
    quota: str = ""         # основные места / особая / отдельная / целевая
    raw_position: Optional[int] = None   # № строки в списке
    real_position: Optional[int] = None  # позиция среди реальных конкурентов
    plan_places: Optional[int] = None    # план приёма / КЦП
    score: Optional[int] = None
    priority: Optional[int] = None       # приоритет нашего абитуриента
    consent: Optional[bool] = None       # есть ли наше согласие
    competitors_above: Optional[int] = None
    url: str = ""


# ──────────────────────────────────────────────────────────────────────────
#  ОБЩИЙ ПАРСЕР ТАБЛИЦ
# ──────────────────────────────────────────────────────────────────────────

CODE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2})\b")
PLAN_RE = re.compile(r"План\s*приема\s*(\d+)", re.IGNORECASE)
ID_HEADER_KEYS = ("уид", "уникальный код", "код абитуриента", "id")
CONSENT_WORDS = ("согласие",)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _to_int(s: str) -> Optional[int]:
    m = re.search(r"-?\d+", s or "")
    return int(m.group()) if m else None


def parse_group_meta(title: str) -> dict:
    """Из заголовка группы вытащить код, название, форму, основу, квоту, план."""
    title = _norm(title)
    code_m = CODE_RE.search(title)
    code = code_m.group(1) if code_m else ""
    plan_m = PLAN_RE.search(title)
    plan = int(plan_m.group(1)) if plan_m else None

    low = title.lower()
    form = ("очно-заочная" if "очно-заоч" in low
            else "заочная" if "заочн" in low
            else "очная" if "очн" in low else "")
    basis = ("платно" if ("оплат" in low or "договор" in low or "платн" in low)
             else "бюджет" if "бюджет" in low else "")
    quota = ("основные места" if "основн" in low
             else "особая" if "особ" in low
             else "отдельная" if "отдельн" in low
             else "целевая" if "целев" in low else "")

    # название: убрать код и «План приема N» и хвосты с формой/основой
    name = title
    if code:
        name = name.replace(code, "")
    name = PLAN_RE.sub("", name)
    name = re.split(r"\s+[-–]\s+", name)[0]  # до первого « - Очная - Бюджет…»
    name = re.split(r"Программа подготовки", name)[0]  # убрать «Программа подготовки …»
    name = _norm(name)
    return dict(code=code, name=name, form=form, basis=basis, quota=quota, plan=plan)


def _header_index(cells: list[str]) -> Optional[dict]:
    """Если строка похожа на шапку (есть колонка с УИД) — вернуть индексы колонок."""
    low = [c.lower() for c in cells]
    id_col = next((i for i, c in enumerate(low)
                   if any(k in c for k in ID_HEADER_KEYS)), None)
    if id_col is None:
        return None
    def find(*keys):
        return next((i for i, c in enumerate(low)
                     if any(k in c for k in keys)), None)
    return {
        "id": id_col,
        "pos": find("№", "место", "поз") or 0,
        "score": find("балл", "сумма"),
        "priority": find("приоритет"),
        "consent": find("согласие"),
    }


def parse_lists_html(html: str, url: str, vuz: str) -> list[Record]:
    """
    Универсальный парсер страницы с конкурсными списками.
    Идёт по таблицам, в каждой определяет шапку (по колонке УИД),
    собирает все строки группы, находит нашего абитуриента и считает позиции.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Record] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        # текст до таблицы / первые строки = заголовок группы
        meta_text = " ".join(
            _norm(table.find_previous(h).get_text(" ")) for h in ()  # placeholder
        )
        # чаще заголовок — внутри первых строк самой таблицы или в caption
        cap = table.find("caption")
        head_text = _norm(cap.get_text(" ")) if cap else ""

        header = None
        body_rows = []
        for tr in rows:
            cells = [_norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            joined = " ".join(cells)
            if CODE_RE.search(joined) and PLAN_RE.search(joined) and header is None:
                head_text = joined  # строка-заголовок группы
            h = _header_index(cells)
            if h and header is None:
                header = h
                continue
            if header:
                body_rows.append(cells)

        if not header:
            continue

        # если заголовок не нашли внутри — посмотреть на предыдущий элемент
        if not CODE_RE.search(head_text):
            prev = table.find_previous(string=CODE_RE)
            if prev:
                head_text = _norm(str(prev))

        meta = parse_group_meta(head_text)

        # отфильтровать «технические» строки (примечания и т.п.)
        data = []
        for cells in body_rows:
            if header["id"] >= len(cells):
                continue
            uid = re.sub(r"\D", "", cells[header["id"]])
            if not uid:
                continue
            data.append(cells)

        # найти нашего
        for idx, cells in enumerate(data):
            uid = re.sub(r"\D", "", cells[header["id"]])
            if uid != APPLICANT_ID:
                continue

            def cell(key):
                i = header.get(key)
                return cells[i] if (i is not None and i < len(cells)) else ""

            raw_pos = _to_int(cell("pos")) or (idx + 1)
            rec = Record(
                vuz=vuz, url=url,
                code=meta["code"], name=meta["name"], form=meta["form"],
                basis=meta["basis"], quota=meta["quota"], plan_places=meta["plan"],
                raw_position=raw_pos,
                score=_to_int(cell("score")),
                priority=_to_int(cell("priority")),
                consent=("соглас" in cell("consent").lower()) if header.get("consent") else None,
            )
            _add_real_position(rec, data, header, idx)
            out.append(rec)

    return out


def _add_real_position(rec: Record, data, header, our_idx: int) -> None:
    """
    Реальная позиция (оценка): среди тех, кто ВЫШЕ нас по списку, считаем
    «реальными конкурентами» только тех, кто:
       • подал согласие на эту группу,  ИЛИ
       • поставил эту группу приоритетом №1 (она для них «высший приоритет»).
    Остальные выше, скорее всего, уйдут на другой вуз/приоритет и освободят место.
    Это приближение: точная картина требует расчёта по всем вузам сразу.
    """
    ci = header.get("consent")
    pi = header.get("priority")
    competitors = 0
    for cells in data[:our_idx]:
        consent = ("соглас" in cells[ci].lower()) if (ci is not None and ci < len(cells)) else False
        prio = _to_int(cells[pi]) if (pi is not None and pi < len(cells)) else None
        if consent or prio == 1:
            competitors += 1
    rec.competitors_above = competitors
    rec.real_position = competitors + 1


# ──────────────────────────────────────────────────────────────────────────
#  АДАПТЕРЫ ПО ВУЗАМ
# ──────────────────────────────────────────────────────────────────────────

def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


# ---- Волгатех: одна статическая страница со всеми группами ----------------
def adapter_volgatech() -> list[Record]:
    url = "https://dod.volgatech.net/rating/bachelor.html"
    log.info("Волгатех: %s", url)
    return parse_lists_html(fetch(url), url, "Волгатех")


# ---- ГУАП: оглавление → формы → таблицы направлений → списки групп --------
def adapter_guap() -> list[Record]:
    base = "https://priem.guap.ru"
    # во время конкурса использовать /bach/rating ; до него — /bach/lists
    root = base + "/bach/lists"
    log.info("ГУАП: %s", root)
    recs: list[Record] = []
    soup = BeautifulSoup(fetch(root), "html.parser")
    form_links = {a["href"] for a in soup.select("a[href*='/bach/lists/list_']")}
    for href in sorted(form_links):
        form_url = href if href.startswith("http") else base + "/" + href.lstrip("/")
        try:
            fsoup = BeautifulSoup(fetch(form_url), "html.parser")
        except Exception as e:
            log.warning("ГУАП форма %s: %s", form_url, e)
            continue
        # ссылки на конкретные конкурсные группы (числа-ссылки в таблице)
        group_links = {a["href"].replace("\\", "/")
                       for a in fsoup.select("a[href*='/bach/lists/list_']")
                       if a["href"].replace("\\", "/") != form_url}
        for g in sorted(group_links):
            gurl = g if g.startswith("http") else base + "/" + g.lstrip("/")
            try:
                recs += parse_lists_html(fetch(gurl), gurl, "ГУАП")
            except Exception as e:
                log.warning("ГУАП группа %s: %s", gurl, e)
            time.sleep(0.2)
    return recs


# ---- СПбГУ: индекс конкурсов → файлы list_<guid>.html ---------------------
def adapter_spbu() -> list[Record]:
    # Общие рейтинговые списки граждан РФ. ВНИМАНИЕ: открываются в конце июля.
    index = "https://cabinet.spbu.ru/Lists/AG_Rating/index_comp_groups.html"
    log.info("СПбГУ: %s", index)
    try:
        html = fetch(index)
    except Exception as e:
        log.warning("СПбГУ индекс недоступен (вероятно, списки ещё не открыты): %s", e)
        return []
    soup = BeautifulSoup(html, "html.parser")
    base = index.rsplit("/", 1)[0] + "/"
    recs: list[Record] = []
    for a in soup.select("a[href*='list_']"):
        href = a["href"]
        lurl = href if href.startswith("http") else base + href
        try:
            recs += parse_lists_html(fetch(lurl), lurl, "СПбГУ")
        except Exception as e:
            log.warning("СПбГУ список %s: %s", lurl, e)
        time.sleep(0.2)
    return recs


# ---- Бонч: форма + AJAX. Нужно один раз подсмотреть запрос (см. README) ---
def adapter_bonch() -> list[Record]:
    """
    Страница priem.sut.ru/spisok-abiturientov отдаёт список через AJAX.
    Откройте список в браузере → DevTools → вкладка Network → найдите запрос,
    который возвращает таблицу, и впишите его сюда (URL + параметры).
    Ниже — заготовка под типовой POST. Если эндпоинт другой — поправьте.
    """
    endpoint = os.environ.get("BONCH_ENDPOINT", "")  # напр. https://priem.sut.ru/api/...
    if not endpoint:
        log.warning("Бонч: BONCH_ENDPOINT не задан — пропускаю (см. README, раздел Бонч).")
        return []
    recs: list[Record] = []
    # перебор конкурсных групп; список group_ids тоже снимается из Network один раз
    group_ids = json.loads(os.environ.get("BONCH_GROUPS", "[]"))
    for gid in group_ids:
        try:
            r = requests.get(endpoint, params={"group": gid}, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            # ответ может быть HTML или JSON — пробуем оба
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                recs += _bonch_from_json(r.json(), endpoint, gid)
            else:
                recs += parse_lists_html(r.text, endpoint, "Бонч")
        except Exception as e:
            log.warning("Бонч группа %s: %s", gid, e)
        time.sleep(0.2)
    return recs


def _bonch_from_json(payload, url, gid) -> list[Record]:
    # Заглушка-пример: подгоните под реальные имена полей ответа Бонча.
    rows = payload.get("rows") or payload.get("data") or []
    plan = payload.get("plan") or payload.get("kcp")
    data = []
    for row in rows:
        uid = str(row.get("uid") or row.get("code") or "")
        data.append(dict(uid=re.sub(r"\D", "", uid), raw=row))
    out = []
    for idx, item in enumerate(data):
        if item["uid"] != APPLICANT_ID:
            continue
        row = item["raw"]
        rec = Record(
            vuz="Бонч", url=url, plan_places=plan,
            code=str(row.get("specCode", "")), name=str(row.get("specName", "")),
            raw_position=int(row.get("position", idx + 1)),
            score=row.get("score"), priority=row.get("priority"),
            consent=bool(row.get("consent")),
        )
        # реальную позицию по JSON считаем аналогично
        competitors = sum(
            1 for j in data[:idx]
            if j["raw"].get("consent") or j["raw"].get("priority") == 1
        )
        rec.competitors_above = competitors
        rec.real_position = competitors + 1
        out.append(rec)
    return out


# ---- ЛЭТИ: SPA + robots → headless-браузер (Playwright) -------------------
# Конкурсные списки открываются 27 июля 2026. Мониторим 4 кода.
LETI_CODES = ["09.03.01", "09.03.02", "10.05.01", "27.03.03"]
# code -> список id конкурсных групп (снимаются из URL один раз, см. README)
LETI_GROUPS = json.loads(os.environ.get("LETI_GROUPS", json.dumps({
    # пример (id из вашей ссылки — подставьте под нужный код):
    # "09.03.01": ["019ee529-454f-7e45-aced-7f2361797e11"],
})))


def adapter_leti() -> list[Record]:
    if not LETI_GROUPS:
        log.warning("ЛЭТИ: LETI_GROUPS пуст — пропускаю (см. README, раздел ЛЭТИ).")
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("ЛЭТИ: не установлен playwright. pip install playwright && playwright install chromium")
        return []

    recs: list[Record] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        for code, ids in LETI_GROUPS.items():
            for gid in ids:
                url = f"https://abit.etu.ru/ru/postupayushhim/lists/page/#/?id={gid}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=60000)
                    page.wait_for_selector("table", timeout=30000)
                    html = page.content()
                    part = parse_lists_html(html, url, "ЛЭТИ")
                    for r in part:
                        if not r.code:
                            r.code = code
                    recs += part
                except Exception as e:
                    log.warning("ЛЭТИ %s/%s: %s", code, gid, e)
        browser.close()
    return recs


ADAPTERS = [
    adapter_volgatech,
    adapter_guap,
    adapter_spbu,
    adapter_bonch,
    adapter_leti,
]


# ──────────────────────────────────────────────────────────────────────────
#  СБОРКА И ВЫВОД
# ──────────────────────────────────────────────────────────────────────────

def collect() -> list[Record]:
    all_recs: list[Record] = []
    for adapter in ADAPTERS:
        try:
            recs = adapter()
            log.info("%s → найдено записей: %d", adapter.__name__, len(recs))
            all_recs += recs
        except Exception as e:
            log.error("%s упал: %s", adapter.__name__, e)
    if ONLY_BUDGET_MAIN:
        all_recs = [r for r in all_recs
                    if r.basis in ("бюджет", "") and r.quota in ("основные места", "")]
    return all_recs


def to_table(recs: list[Record]) -> list[list[str]]:
    head = ["Вуз", "Код", "Специальность", "Сырая позиция",
            "Реальная позиция", "Бюджетных мест", "Балл", "Приоритет", "Согласие"]
    body = []
    for r in sorted(recs, key=lambda x: (x.vuz, x.code)):
        body.append([
            r.vuz, r.code, r.name,
            str(r.raw_position or "—"),
            str(r.real_position or "—"),
            str(r.plan_places or "—"),
            str(r.score or "—"),
            str(r.priority or "—"),
            "да" if r.consent else ("нет" if r.consent is False else "—"),
        ])
    return [head] + body


def save_csv(recs: list[Record], path="report.csv") -> str:
    rows = to_table(recs)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    return path


def send_telegram(recs: list[Record]) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        log.warning("Telegram не настроен (TG_TOKEN / TG_CHAT_ID) — пропускаю отправку.")
        return
    today = datetime.now().strftime("%d.%m.%Y %H:%M")
    if not recs:
        text = f"📋 {today}\nАбитуриент {APPLICANT_ID} пока не найден ни в одном открытом списке."
    else:
        lines = [f"📋 Рейтинг абитуриента {APPLICANT_ID} — {today}", ""]
        for r in sorted(recs, key=lambda x: (x.vuz, x.code)):
            plan = r.plan_places or "—"
            lines.append(
                f"🏛 <b>{r.vuz}</b> · {r.code} {r.name}\n"
                f"    позиция {r.raw_position or '—'} "
                f"(реальная ~{r.real_position or '—'}) из {plan} мест"
                + (f" · приоритет {r.priority}" if r.priority else "")
            )
        lines.append("\n«реальная» = с учётом согласий и приоритетов выше (оценка).")
        text = "\n".join(lines)

    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    resp = requests.post(api, data={
        "chat_id": TG_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }, timeout=TIMEOUT)
    if resp.status_code != 200:
        log.error("Telegram error: %s", resp.text)
    else:
        log.info("Отчёт отправлен в Telegram.")


def write_sheets(recs: list[Record]) -> None:
    if not SHEETS_ENABLED:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.error("Sheets: нет gspread/google-auth. pip install gspread google-auth")
        return
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEETS_ID)
    try:
        ws = sh.worksheet(SHEETS_TAB)
    except Exception:
        ws = sh.add_worksheet(SHEETS_TAB, rows=200, cols=12)
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    rows = to_table(recs)
    rows[0] = [f"Обновлено: {stamp}"] + [""] * (len(rows[0]) - 1)
    body = [to_table(recs)[0]] + rows[1:]
    ws.clear()
    ws.update([[f"Обновлено: {stamp}"]] + body, "A1")
    log.info("Записано в Google-таблицу, вкладка «%s».", SHEETS_TAB)


def main() -> int:
    log.info("Старт. Ищем УИД %s", APPLICANT_ID)
    recs = collect()
    save_csv(recs)
    log.info("\n" + "\n".join("  ".join(row) for row in to_table(recs)))
    send_telegram(recs)
    write_sheets(recs)
    log.info("Готово. Найдено записей: %d", len(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
