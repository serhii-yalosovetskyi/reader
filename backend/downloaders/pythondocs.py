"""Официальная документация Python как книги читалки.

Документация публикуется ОДНИМ epub (`archives/python-<M.m>-docs.epub`, ~9 МБ,
567 документов). Читать её так нельзя: «Учебник» и «Стандартная библиотека» —
разные книги с разным прогрессом, закладками и переводом. Поэтому адаптер режет
официальный файл на ЧАСТИ по верхнему уровню оглавления, по одной книге на
часть, и собирает из каждой самостоятельный epub.

Второе отличие от фанфиков: у документации не бывает «новых глав». Она
обновляется ВЕРСИЯМИ (3.14.6 → 3.14.7 → 3.15.0), и признак обновления —
НОМЕР ВЕРСИИ, закодированный целым (`major*10000 + minor*100 + micro`).
Монитор сравнивает это число как «главы», поэтому единица объявлена явно
(`monitor._metric_kind` → "version"); подробности и грабли — в
spec.reader.python-docs.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
# defusedxml защищает от XXE/billion-laughs: XML приезжает из сети, пусть и
# с python.org. Тот же выбор, что в calibre/client.py.
from defusedxml import ElementTree as ET

from ..app.config import TMP_DIR
from . import pythondocs_cover
from .base import DownloaderError, DownloadResult, UnsupportedURL

log = logging.getLogger("reader.pythondocs")

HOST = "docs.python.org"
BASE = "https://docs.python.org/3/"
AUTHOR = "Python Software Foundation"
TIMEOUT = 120.0
# Потолок на архив документации: он ~9 МБ, и 100 МБ — это уже не «версия
# потолстела», а сломавшийся upstream. Без потолка ответ, который не кончается,
# забивает диск VPS: скачивание идёт до конца, а «это вообще zip?» проверяется
# только после записи.
MAX_MASTER_BYTES = 100 * 1024 * 1024
# Собранные части складываем в свой каталог и подчищаем: register_download
# копирует книгу к себе по sha1, а исходный временный файл не удаляет — за
# двенадцать книг на каждую пересборку это десятки мегабайт в /tmp навсегда.
PARTS_TTL_SEC = 6 * 3600
# Потолок на ОДИН документ книги. Самая толстая глава, которая уже едет в этих
# книгах и открывается за 3-5 с, — library/stdtypes.xhtml (0.69 МБ); берём чуть
# выше неё. Число не с потолка: профилировщик фронта (задача #589) измерил
# whatsnew/changelog.xhtml на 7.5 МБ — открытие 38-44 с вместо 3-5, заморозка
# вкладки 15-18 с, 3.5 ГБ RSS, 74.6% времени в getBoundingClientRect ←
# expand@paginator.js. Дорога не multicol-раскладка, а обход Range по 110 тыс.
# узлов, поэтому лечится нарезкой документа, а не правкой пагинатора.
MAX_DOC_BYTES = 768 * 1024

# Часть документации = книга. `dirs` — каталоги внутри официального epub,
# `roots` — отдельные файлы в его корне. Порядок словаря = порядок оглавления
# оригинала, по нему же удобно заводить книги пачкой.
PARTS: dict[str, dict] = {
    "tutorial": {
        "title": "Python — Учебник",
        "path": "tutorial/",
        "dirs": ("tutorial",),
        "roots": (),
    },
    "library": {
        "title": "Python — Стандартная библиотека",
        "path": "library/",
        "dirs": ("library",),
        "roots": (),
    },
    "reference": {
        "title": "Python — Справочник по языку",
        "path": "reference/",
        "dirs": ("reference",),
        "roots": (),
    },
    "howto": {
        "title": "Python — HOWTO, практические руководства",
        "path": "howto/",
        "dirs": ("howto",),
        "roots": (),
    },
    "using": {
        "title": "Python — Установка и запуск",
        "path": "using/",
        "dirs": ("using",),
        "roots": (),
    },
    "installing": {
        "title": "Python — Установка и публикация модулей",
        "path": "installing/",
        "dirs": ("installing", "distributing"),
        "roots": (),
    },
    "extending": {
        "title": "Python — Расширение и встраивание",
        "path": "extending/",
        "dirs": ("extending",),
        "roots": (),
    },
    "c-api": {
        "title": "Python — Справочник Python/C API",
        "path": "c-api/",
        "dirs": ("c-api",),
        "roots": (),
    },
    "faq": {
        "title": "Python — Частые вопросы (FAQ)",
        "path": "faq/",
        "dirs": ("faq",),
        "roots": (),
    },
    "whatsnew": {
        "title": "Python — Что нового в каждой версии",
        "path": "whatsnew/",
        "dirs": ("whatsnew",),
        "roots": (),
    },
    "deprecations": {
        "title": "Python — Устаревшее и удаляемое",
        "path": "deprecations/",
        "dirs": ("deprecations",),
        "roots": (),
    },
    # Корневые страницы оригинала: глоссарий и служебные разделы. Отдельной
    # книгой, потому что глоссарий читают, а не листают из оглавления.
    "misc": {
        "title": "Python — Глоссарий и о документации",
        "path": "glossary.html",
        "dirs": (),
        "roots": (
            "glossary.xhtml",
            "about.xhtml",
            "bugs.xhtml",
            "copyright.xhtml",
            "license.xhtml",
        ),
    },
}

# Логотип Python внутри официального архива (он же og:image документации).
LOGO_ASSET = "_static/og-image.png"

MEDIA = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
OPF_NS = "http://www.idpf.org/2007/opf"

_VER_JS = re.compile(r"VERSION:\s*'(\d+)\.(\d+)\.(\d+)'")
_VER_HTML = re.compile(r"Python (\d+)\.(\d+)\.(\d+)")
_IMG_REF = re.compile(r"(?:\.\./)*_images/([A-Za-z0-9_.\-]+)")


# --------------------------------------------------------------------------
# адрес части
# --------------------------------------------------------------------------
def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith(HOST)


def part_url(key: str) -> str:
    return BASE + PARTS[key]["path"]


def part_of(url: str) -> str:
    """Ключ части по адресу. `https://docs.python.org/3/tutorial/` → tutorial.

    Корневые страницы (glossary/about/bugs/copyright/license) — часть `misc`.
    """
    if not supports(url):
        raise UnsupportedURL(f"не docs.python.org: {url}")
    path = (urlparse(url).path or "").lstrip("/")
    # /3/… и /3.14/… — одна и та же документация, версия берётся с сайта.
    path = re.sub(r"^(3(\.\d+)?|dev|latest)/", "", path)
    head = path.split("/", 1)[0]
    if head in PARTS:
        return head
    if head in ("distributing",):
        return "installing"
    stem = head.split(".", 1)[0]
    if f"{stem}.xhtml" in PARTS["misc"]["roots"] or head == "":
        return "misc"
    raise UnsupportedURL(
        f"раздел документации не поддерживается: {url}. "
        f"Известные: {', '.join(part_url(k) for k in PARTS)}"
    )


# --------------------------------------------------------------------------
# версия и мастер-файл
# --------------------------------------------------------------------------
def current_version() -> tuple[str, int]:
    """Версия документации на сайте: ('3.14.7', 31407).

    Спрашиваем `_static/documentation_options.js` — это несколько сотен байт,
    а не главная страница на сотни килобайт: функция вызывается монитором на
    КАЖДОМ тике по каждой из книг.
    """
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            r = c.get(BASE + "_static/documentation_options.js")
            m = _VER_JS.search(r.text) if r.status_code < 400 else None
            if not m:
                r = c.get(BASE)
                m = _VER_HTML.search(r.text)
    except httpx.HTTPError as e:
        raise DownloaderError(f"docs.python.org недоступен: {e}") from e
    if not m:
        raise DownloaderError("не удалось определить версию документации Python")
    major, minor, micro = (int(x) for x in m.groups())
    return f"{major}.{minor}.{micro}", version_int(major, minor, micro)


def version_int(major: int, minor: int, micro: int) -> int:
    """Версия одним монотонным числом: 3.14.7 → 31407, 3.15.0 → 31500.

    Монитор умеет сравнивать только «больше/меньше», поэтому кодировка обязана
    расти вместе с релизом. micro ограничен 99 — за всю историю CPython столько
    патч-релизов у одной ветки не выходило.
    """
    return major * 10000 + minor * 100 + micro


def count_chapters(url: str) -> int | None:
    """Метрика обновления для монитора — НОМЕР ВЕРСИИ, а не число глав.

    Единица объявлена в `monitor._metric_kind` как "version": сравнивать это
    число с количеством секций в файле нельзя (см. spec.reader.python-docs).
    """
    part_of(url)  # неизвестный раздел — не наше дело
    return current_version()[1]


def _master_path(ver: str, vint: int, lang: str = "en") -> Path:
    minor = ".".join(ver.split(".")[:2])
    suffix = "" if lang == "en" else f"-{lang}"
    return TMP_DIR / "pythondocs" / f"python-{minor}-docs{suffix}-{vint}.epub"


def archive_url(ver: str, lang: str = "en") -> str:
    """Адрес официального архива. Русская сборка живёт под /ru/ той же версии."""
    minor = ".".join(ver.split(".")[:2])
    root = BASE if lang == "en" else f"https://docs.python.org/{lang}/3/"
    return f"{root}archives/python-{minor}-docs.epub"


def fetch_master(ver: str, vint: int, lang: str = "en") -> Path:
    """Официальный epub целиком, с кэшем по версии.

    12 книг = 12 подписок, и каждая на своём тике попросила бы 9 МБ. Скачиваем
    во временный файл рядом и переименовываем: `check_all` обходит подписки в
    потоках, и половинчатый файл не должен стать «кэшем».
    """
    dest = _master_path(ver, vint, lang)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = archive_url(ver, lang)
    fd, tmp_name = tempfile.mkstemp(suffix=".epub", dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        import os

        os.close(fd)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise DownloaderError(
                        f"архив документации не отдан ({r.status_code}): {url}"
                    )
                size = 0
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > MAX_MASTER_BYTES:
                            raise DownloaderError(
                                f"архив документации больше {MAX_MASTER_BYTES // 1024 // 1024} МБ "
                                f"— похоже на сбой источника: {url}"
                            )
                        f.write(chunk)
        if not zipfile.is_zipfile(tmp):
            raise DownloaderError(f"архив документации не похож на epub: {url}")
        tmp.replace(dest)
    except httpx.HTTPError as e:
        raise DownloaderError(f"не удалось скачать {url}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    # Старые ВЕРСИИ в кэше не нужны: место дороже повторной загрузки раз в месяц.
    # Именно версии, а не «все прочие файлы»: рядом лежит русская сборка той же
    # версии, и удалять её здесь означало бы качать 9 МБ на каждую часть.
    for stale in dest.parent.glob("python-*-docs*.epub"):
        if not stale.name.endswith(f"-{vint}.epub"):
            stale.unlink(missing_ok=True)
    return dest


# --------------------------------------------------------------------------
# сборка части
# --------------------------------------------------------------------------
def _spine_hrefs(opf_xml: str) -> list[str]:
    """Документы мастера в порядке чтения (spine), href'ы как в манифесте."""
    root = ET.fromstring(opf_xml)
    manifest = {}
    for item in root.iter(f"{{{OPF_NS}}}item"):
        manifest[item.get("id")] = item.get("href")
    out = []
    for ref in root.iter(f"{{{OPF_NS}}}itemref"):
        href = manifest.get(ref.get("idref"))
        if href:
            out.append(href)
    return out


def _in_part(href: str, part: dict) -> bool:
    if href in part["roots"]:
        return True
    return any(href.startswith(d + "/") for d in part["dirs"])


def _nav_tree(ncx_xml: str, part: dict) -> list[dict]:
    """Поддерево оглавления мастера, относящееся к части.

    Берём ТОЛЬКО верхнеуровневые точки части: у документации это ровно один
    раздел (или несколько корневых страниц у `misc`), а вложенность внутри
    сохраняется как есть.
    """
    root = ET.fromstring(ncx_xml)

    def walk(node) -> list[dict]:
        out = []
        for np in node.findall(f"{{{NCX_NS}}}navPoint"):
            label = np.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
            content = np.find(f"{{{NCX_NS}}}content")
            src = (content.get("src") or "") if content is not None else ""
            out.append(
                {
                    "title": (label.text or "").strip() if label is not None else "",
                    "src": src,
                    "children": walk(np),
                }
            )
        return out

    top = walk(root.find(f"{{{NCX_NS}}}navMap"))
    return [n for n in top if _in_part(n["src"].split("#", 1)[0], part)]


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_TITLE_TAIL = re.compile(r"\s*[—–-]\s*Python\s+[\d.]+.*$", re.S)


def _doc_title(html: str, fallback: str) -> str:
    """Заголовок документа из <title>, без хвоста «— Python 3.14.7 documentation»."""
    m = _TITLE_RE.search(html)
    if not m:
        return fallback
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    t = _TITLE_TAIL.sub("", t).strip()
    return t or fallback


def _complete_tree(tree: list[dict], docs: list[str], bodies: dict[str, str]) -> list[dict]:
    """Дополнить оглавление документами, которых в нём нет.

    В оглавлении оригинала целые разделы представлены ОДНОЙ точкой: у «Python
    HOWTOs» вложенных пунктов нет вовсе, хотя документов 29. Книга с одним
    пунктом оглавления нечитаема — навигация по ней невозможна, и `count_sections`
    честно показывает «1 глава». Недостающие документы добавляются в порядке
    spine, заголовок берётся из самого документа.
    """
    covered: set[str] = set()

    def collect(nodes: list[dict]) -> None:
        for n in nodes:
            covered.add(n["src"].split("#", 1)[0])
            collect(n["children"])

    collect(tree)
    top = {n["src"].split("#", 1)[0]: n for n in tree}
    out: list[dict] = []
    for href in docs:
        if href in top:
            out.append(top[href])
        elif href not in covered:
            out.append(
                {
                    "title": _doc_title(bodies.get(href, ""), href),
                    "src": href,
                    "children": [],
                }
            )
    return out or tree


# --------------------------------------------------------------------------
# нарезка переросших документов
# --------------------------------------------------------------------------
_SECTION_TOKEN = re.compile(r"<section\b[^>]*>|</section>", re.I)
_ANY_ID = re.compile(r'\bid="([^"]+)"')
_H_ANY = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.S | re.I)
_H1_TEXT = re.compile(r"(<h1\b[^>]*>)(.*?)(</h1>)", re.S | re.I)
_SERIES = re.compile(r"Python\s+(\d+)\.(\d+)\b")
_SLUG_BAD = re.compile(r"[^A-Za-z0-9.]+")


def _top_sections(html: str) -> list[tuple[int, int]]:
    """Границы секций второго уровня вложенности (главы внутри документа).

    Считаем по тегам, а не парсером: документ на 7.5 МБ через ElementTree стоит
    сотен мегабайт памяти, а нам нужны только смещения.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    for m in _SECTION_TOKEN.finditer(html):
        if m.group(0).startswith("</"):
            depth -= 1
            if start is not None and depth == 1:
                spans.append((start, m.end()))
                start = None
        else:
            depth += 1
            if depth == 2:
                start = m.start()
    return spans


def _group_key(segment: str, index: int) -> str:
    """Ключ группировки секции — устойчивый, а не порядковый.

    Для журнала изменений это минорная серия («3.14.x»): новый выпуск 3.14.8
    попадает в ту же группу, что и остальные 3.14.*, и границы нарезки не
    съезжают на каждом обновлении. Иначе бы каждое ↻ перекраивало книгу и
    сохранённая позиция чтения теряла смысл.
    """
    m = _H_ANY.search(segment)
    title = re.sub(r"<[^>]+>", "", m.group(2)).strip() if m else ""
    ser = _SERIES.search(title)
    if ser:
        return f"{ser.group(1)}.{ser.group(2)}.x"
    return title or f"#{index}"


def _slug(key: str) -> str:
    """Имя куска из ключа группы. Пустой результат — отдельный случай, не «part».

    `_SLUG_BAD` схлопывает всё не-ASCII, поэтому у заголовка на кириллице от
    ключа не остаётся ничего. Одинаковое имя для разных групп означало бы
    позиционную нумерацию — ровно тот режим, который нарезка по устойчивому
    ключу и должна исключать. Поэтому запасное имя выводится из ключа хэшем:
    оно нечитаемо, но стабильно между пересборками, а вызывающий предупреждён.
    """
    slug = _SLUG_BAD.sub("-", key).strip("-.").lower()
    if slug:
        return slug
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    log.warning(
        "pythondocs: из ключа группы %r не получилось читаемое имя куска, "
        "беру хэш %s — имя стабильно, но по нему не видно, что внутри",
        key,
        digest,
    )
    return digest


def _retitle(html: str, title: str) -> str:
    """Заменить <title> и первый <h1> — так глава подписана в оглавлении."""
    esc = _esc(title)
    if _TITLE_RE.search(html):
        html = _TITLE_RE.sub(lambda _m: f"<title>{esc}</title>", html, count=1)
    return _H1_TEXT.sub(lambda m: f"{m.group(1)}{esc}{m.group(3)}", html, count=1)


def _split_doc(href: str, html: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Разрезать переросший документ на главы по границам секций.

    Возвращает (порядок файлов, тела, карта «якорь → файл»). Первый кусок
    сохраняет исходное имя: на него ведут ссылки и оглавление, и позиция
    чтения в начале книги переживает нарезку.

    Имя куска — по ключу группы, а не по номеру: появление новой серии сверху
    сдвинуло бы все номера и обесценило закладки.
    """
    if len(html.encode("utf-8")) <= MAX_DOC_BYTES:
        return [href], {href: html}, {}

    spans = _top_sections(html)
    if len(spans) < 2:
        # Резать не по чему: у документа нет внутренних секций. Отдаём целиком,
        # но громко — книга с такой главой будет открываться десятки секунд.
        log.warning(
            "pythondocs: %s весит %.1f МБ и не имеет секций для нарезки — "
            "глава уедет в книгу целиком, открытие будет медленным",
            href,
            len(html) / 1024 / 1024,
        )
        return [href], {href: html}, {}

    prefix = html[: spans[0][0]]
    suffix = html[spans[-1][1] :]
    base_title = _doc_title(html, href)

    groups: list[tuple[str, list[str]]] = []
    for i, (a, b) in enumerate(spans):
        seg = html[a:b]
        key = _group_key(seg, i)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(seg)
        else:
            groups.append((key, [seg]))

    # Мелкие группы склеиваем: у документа без осмысленных ключей (каждая
    # секция — своя группа) иначе вышли бы десятки крошечных файлов. Порог
    # намеренно низкий: группа размером с настоящую главу должна стоять
    # отдельным файлом, иначе рост соседа сдвинет её границу и обесценит
    # сохранённую позицию чтения.
    glue = MAX_DOC_BYTES // 8
    merged: list[tuple[str, list[str]]] = []
    for key, segs in groups:
        size = sum(len(x) for x in segs)
        if (
            merged
            and size <= glue
            and sum(len(x) for x in merged[-1][1]) + size <= MAX_DOC_BYTES
        ):
            merged[-1][1].extend(segs)
        else:
            merged.append((key, list(segs)))

    # Группа, которая сама не влезает, режется по своим секциям.
    chunks: list[tuple[str, str, list[str]]] = []  # (ключ, подпись, секции)
    for key, segs in merged:
        if sum(len(x) for x in segs) <= MAX_DOC_BYTES:
            chunks.append((key, key, segs))
            continue
        cur: list[str] = []
        part_no = 1
        for seg in segs:
            if cur and sum(len(x) for x in cur) + len(seg) > MAX_DOC_BYTES:
                chunks.append((key if part_no == 1 else f"{key}-{part_no}",
                               key if part_no == 1 else f"{key} ({part_no})", cur))
                part_no += 1
                cur = []
            cur.append(seg)
        if cur:
            chunks.append((key if part_no == 1 else f"{key}-{part_no}",
                           key if part_no == 1 else f"{key} ({part_no})", cur))

    stem, ext = (href[: -len(".xhtml")], ".xhtml") if href.endswith(".xhtml") else (href, "")
    order: list[str] = []
    bodies: dict[str, str] = {}
    anchors: dict[str, str] = {}
    used: set[str] = set()
    for i, (key, label, segs) in enumerate(chunks):
        if i == 0:
            name = href
        else:
            slug = _slug(key)
            name = f"{stem}-{slug}{ext}"
            n = 2
            while name in used:
                name = f"{stem}-{slug}-{n}{ext}"
                n += 1
        used.add(name)
        body = prefix + "".join(segs) + suffix
        title = base_title if len(chunks) == 1 else f"{base_title} — {label}"
        body = _retitle(body, title)
        order.append(name)
        bodies[name] = body
        for m in _ANY_ID.finditer(body):
            anchors.setdefault(m.group(1), name)

    log.info(
        "pythondocs: %s (%.1f МБ) нарезан на %d глав, самая толстая %.2f МБ",
        href,
        len(html) / 1024 / 1024,
        len(order),
        max(len(b) for b in bodies.values()) / 1024 / 1024,
    )
    return order, bodies, anchors


def _retarget(src: str, moved: dict[str, dict[str, str]]) -> str:
    """Точка оглавления `doc.xhtml#anchor` → тот кусок, где якорь оказался."""
    path, sep, anchor = src.partition("#")
    table = moved.get(path)
    if not table or not sep:
        return src
    return f"{table.get(anchor, path)}#{anchor}"


def _retarget_tree(tree: list[dict], moved: dict[str, dict[str, str]]) -> list[dict]:
    return [
        {
            "title": n["title"],
            "src": _retarget(n["src"], moved),
            "children": _retarget_tree(n["children"], moved),
        }
        for n in tree
    ]


def _rewrite_links(
    html: str,
    self_path: str,
    keep: set[str],
    known: set[str],
    moved: dict[str, dict[str, str]] | None = None,
    origin: str | None = None,
) -> str:
    """Ссылки ЗА пределы части — на сайт, внутри части — как есть.

    После разреза половина перекрёстных ссылок документации ведёт в файлы,
    которых в этой книге нет. Оставить их — значит отдать читателю мёртвую
    ссылку; переписываем в абсолютный `https://docs.python.org/3/…`.

    `moved` — карта нарезанных документов («якорь → файл, где он оказался»),
    `origin` — исходное имя документа, из которого получен этот кусок. Без них
    ссылка `changelog.xhtml#python-3-9-0-final` вела бы в первый кусок, где
    такого якоря уже нет, и молча не срабатывала.
    """
    base_dir = posixpath.dirname(self_path)
    moved = moved or {}
    origin = origin or self_path

    def relative(target: str) -> str:
        rel = posixpath.relpath(target, base_dir) if base_dir else target
        return rel

    def sub(m: re.Match) -> str:
        attr, value = m.group(1), m.group(2)
        if not value or value[0] == "?" or "://" in value or value.startswith("mailto:"):
            return m.group(0)
        if value[0] == "#":
            # Внутренняя ссылка куска: якорь мог уехать в соседний кусок.
            table = moved.get(origin)
            if not table:
                return m.group(0)
            chunk = table.get(value[1:])
            if not chunk or chunk == self_path:
                return m.group(0)
            return f'{attr}="{relative(chunk)}{value}"'
        path, _, anchor = value.partition("#")
        target = posixpath.normpath(posixpath.join(base_dir, path)) if path else self_path
        table = moved.get(target)
        if table is not None and anchor:
            chunk = table.get(anchor, target)
            return f'{attr}="{relative(chunk)}#{anchor}"'
        if target in keep:
            return m.group(0)
        if target in known:
            site = target[:-6] + ".html" if target.endswith(".xhtml") else target
            url = BASE + site + (("#" + anchor) if anchor else "")
            return f'{attr}="{url}"'
        return m.group(0)

    return re.sub(r'\b(href)="([^"]*)"', sub, html)


def _opf(key: str, ver: str, files: list[str], spine: list[str], cover: bool) -> str:
    part = PARTS[key]
    items = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
    ]
    if cover:
        # Обложку объявляем ОБОИМИ способами: `properties="cover-image"` (epub3) и
        # `<meta name="cover">` (epub2). Читалки и каталогизаторы ищут по-разному,
        # а извлекатель обложек читалки (covers._epub_cover) начинает со второго.
        items += [
            '<item id="cover-img" href="cover.png" media-type="image/png" '
            'properties="cover-image"/>',
            '<item id="cover-page" href="cover.xhtml" '
            'media-type="application/xhtml+xml"/>',
        ]
    ids = {}
    for i, href in enumerate(files):
        ext = posixpath.splitext(href)[1].lower()
        ids[href] = f"i{i}"
        items.append(
            f'<item id="i{i}" href="{_esc(href)}" '
            f'media-type="{MEDIA.get(ext, "application/octet-stream")}"/>'
        )
    refs_list = ['<itemref idref="cover-page"/>'] if cover else []
    refs_list += [f'<itemref idref="{ids[h]}"/>' for h in spine if h in ids]
    refs = "\n    ".join(refs_list)
    desc = (
        f"Официальная документация Python {ver}, раздел «{part['title']}». "
        f"Источник: {part_url(key)}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid" xml:lang="en">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">pythondocs-{key}</dc:identifier>\n'
        f"    <dc:title>{_esc(part['title'])}</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        f"    <dc:creator>{AUTHOR}</dc:creator>\n"
        f"    <dc:description>{_esc(desc)}</dc:description>\n"
        f"    <dc:source>{part_url(key)}</dc:source>\n"
        f'    <meta property="dcterms:modified">{ver}</meta>\n'
        + ('    <meta name="cover" content="cover-img"/>\n' if cover else "")
        + "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(items) + "\n  </manifest>\n"
        f'  <spine toc="ncx">\n    {refs}\n  </spine>\n'
        "</package>\n"
    )


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _ncx(key: str, tree: list[dict]) -> str:
    counter = [0]

    def render(nodes: list[dict], depth: int) -> str:
        out = []
        for n in nodes:
            counter[0] += 1
            i = counter[0]
            pad = "  " * depth
            kids = render(n["children"], depth + 1)
            out.append(
                f'{pad}<navPoint id="n{i}" playOrder="{i}">'
                f"<navLabel><text>{_esc(n['title'])}</text></navLabel>"
                f'<content src="{_esc(n["src"])}"/>{kids}</navPoint>'
            )
        return "\n".join(out)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content="pythondocs-{key}"/></head>\n'
        f"  <docTitle><text>{_esc(PARTS[key]['title'])}</text></docTitle>\n"
        "  <navMap>\n" + render(tree, 2) + "\n  </navMap>\n</ncx>\n"
    )


def _nav(key: str, tree: list[dict]) -> str:
    def render(nodes: list[dict]) -> str:
        if not nodes:
            return ""
        li = "".join(
            f'<li><a href="{_esc(n["src"])}">{_esc(n["title"])}</a>{render(n["children"])}</li>'
            for n in nodes
        )
        return f"<ol>{li}</ol>"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f"<title>{_esc(PARTS[key]['title'])}</title></head><body>"
        f'<nav epub:type="toc" id="toc"><h1>{_esc(PARTS[key]["title"])}</h1>'
        f"{render(tree)}</nav></body></html>\n"
    )


def _prune(parts_dir: Path) -> None:
    """Убрать собранные части старше PARTS_TTL_SEC.

    Чистим ПЕРЕД сборкой, а не после: свежесобранную часть ещё копирует
    вызывающий код, и удалять её здесь — значит гоняться с ним наперегонки.
    """
    import time

    deadline = time.time() - PARTS_TTL_SEC
    for stale in parts_dir.glob("pydocs-*.epub"):
        try:
            if stale.stat().st_mtime < deadline:
                stale.unlink(missing_ok=True)
        except OSError:  # чужой файл/гонка — не повод ронять сборку книги
            pass


def _cover_page(title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f"<title>{_esc(title)}</title><style>"
        "html,body{margin:0;padding:0;height:100%;text-align:center;background:#0e1620}"
        "img{max-width:100%;max-height:100%}</style></head>"
        f'<body><img src="cover.png" alt="{_esc(title)}"/></body></html>\n'
    )


def build_part(master: Path, key: str, ver: str, out_path: Path | None = None) -> Path:
    """Собрать самостоятельный epub одной части из официального архива."""
    if key not in PARTS:
        raise UnsupportedURL(f"неизвестный раздел документации: {key}")
    part = PARTS[key]
    with zipfile.ZipFile(master) as z:
        names = set(z.namelist())
        opf_xml = z.read("content.opf").decode("utf-8")
        spine = [h for h in _spine_hrefs(opf_xml) if h in names]
        docs = [h for h in spine if _in_part(h, part)]
        if not docs:
            raise DownloaderError(f"в архиве нет раздела «{key}»")
        tree = _nav_tree(z.read("toc.ncx").decode("utf-8"), part)

        # Сначала нарезка, только потом переписывание ссылок: иначе ссылки
        # проставляются на документы, которых после нарезки не существует,
        # и ломаются молча — ни ошибки, ни следа в логе.
        raw: dict[str, str] = {}
        images: set[str] = set()
        chunked: list[str] = []
        moved: dict[str, dict[str, str]] = {}
        origin_of: dict[str, str] = {}
        for href in docs:
            html = z.read(href).decode("utf-8")
            for m in _IMG_REF.finditer(html):
                cand = f"_images/{m.group(1)}"
                if cand in names:
                    images.add(cand)
            order, parts_html, anchors = _split_doc(href, html)
            if anchors:
                moved[href] = anchors
            for name in order:
                chunked.append(name)
                raw[name] = parts_html[name]
                origin_of[name] = href

        docs = chunked
        keep = set(docs)
        bodies = {
            name: _rewrite_links(html, name, keep, names, moved, origin_of[name])
            for name, html in raw.items()
        }

        tree = _complete_tree(_retarget_tree(tree, moved), docs, bodies)
        # Логотип берём из самого архива документации — официальный, и он уже
        # скачан; отдельного запроса за картинкой не нужно.
        logo = z.read(LOGO_ASSET) if LOGO_ASSET in names else None
        cover_png = pythondocs_cover.render(part["title"], ver, key, logo)
        assets = sorted(n for n in names if n.startswith("_static/")) + sorted(images)
        files = docs + assets

        if out_path:
            out = Path(out_path)
        else:
            parts_dir = TMP_DIR / "pythondocs" / "parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
            _prune(parts_dir)
            out = Path(
                tempfile.mkstemp(suffix=".epub", prefix=f"pydocs-{key}-", dir=str(parts_dir))[1]
            )
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            # mimetype обязан лежать первым и БЕЗ сжатия — иначе часть читалок
            # не опознаёт архив как epub.
            w.writestr(
                zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED
            )
            w.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            w.writestr("content.opf", _opf(key, ver, files, docs, bool(cover_png)))
            if cover_png:
                w.writestr("cover.png", cover_png)
                w.writestr("cover.xhtml", _cover_page(part["title"]))
            w.writestr("toc.ncx", _ncx(key, tree))
            w.writestr("nav.xhtml", _nav(key, tree))
            for href, html in bodies.items():
                w.writestr(href, html)
            for asset in assets:
                w.writestr(asset, z.read(asset))
    return out


# --------------------------------------------------------------------------
# интерфейс загрузчика
# --------------------------------------------------------------------------
def download(url: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    key = part_of(url)
    ver, vint = current_version()
    master = fetch_master(ver, vint)
    path = build_part(master, key, ver)
    part = PARTS[key]
    return DownloadResult(
        file_path=path,
        file_format="epub",
        title=part["title"],
        author=AUTHOR,
        site="pythondocs",
        source_url=part_url(key),
        # Число «глав» книги считает register_download по самому файлу; сюда
        # кладём ВЕРСИЮ отдельным полем, чтобы подписка завелась в правильных
        # единицах (см. extra["update_metric"]).
        num_chapters=0,
        extra={
            "annotation": (
                f"Официальная документация Python, раздел «{part['title']}». "
                f"Версия {ver}. Источник: {part_url(key)}"
            ),
            "docs_version": ver,
            # Метрика подписки: монитор сравнивает именно это число.
            "update_metric": vint,
            # Источник версионирован: новый файл актуальнее по определению,
            # без сравнения объёмов текста (spec.reader.python-docs).
            "authoritative": True,
            "status": "обновляется",
        },
    )
