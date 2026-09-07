"""Документация Python как книги: адрес части, версия-как-метрика, разрез epub.

Сети здесь нет: мастер-архив собирается синтетический, версия не спрашивается.
Проверяется то, что ломается молча — единицы измерения обновления и ссылки,
ведущие за пределы части.
"""
from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from pathlib import Path

import pytest

from backend.accounts.monitor import _metric_kind
from backend.downloaders import pythondocs as pd
from backend.downloaders.base import UnsupportedURL

BASE = "https://docs.python.org/3/"


def test_part_of_maps_urls():
    assert pd.part_of(BASE + "tutorial/") == "tutorial"
    assert pd.part_of(BASE + "howto/descriptor.html") == "howto"
    assert pd.part_of(BASE + "library/functions.html") == "library"
    assert pd.part_of(BASE + "glossary.html") == "misc"
    # distributing — один документ, живёт в книге про установку модулей
    assert pd.part_of(BASE + "distributing/index.html") == "installing"
    # версия в пути не меняет раздела: документацию всё равно берём актуальную
    assert pd.part_of("https://docs.python.org/3.14/faq/general.html") == "faq"


def test_part_of_rejects_foreign_and_unknown():
    with pytest.raises(UnsupportedURL):
        pd.part_of("https://example.com/3/tutorial/")
    with pytest.raises(UnsupportedURL):
        pd.part_of(BASE + "no-such-section/index.html")


def test_version_int_is_monotonic_across_releases():
    assert pd.version_int(3, 14, 7) == 31407
    assert pd.version_int(3, 14, 7) < pd.version_int(3, 14, 10)
    assert pd.version_int(3, 14, 99) < pd.version_int(3, 15, 0)
    assert pd.version_int(3, 99, 0) < pd.version_int(4, 0, 0)


def test_metric_kind_knows_version_units():
    """Главная ловушка: единица обязана узнаваться и в URL, и в ГОЛОМ хосте.

    `Monitored.last_seen_source` хранит хост без схемы, и распознавание через
    urlparse().hostname вернуло бы там None → «chapters» → сравнение номера
    версии с числом глав → вечная перекачка.
    """
    assert _metric_kind(BASE + "tutorial/") == "version"
    assert _metric_kind("docs.python.org") == "version"
    assert _metric_kind("https://readli.net/chitat-online/?b=1") == "pages"
    assert _metric_kind("https://ficbook.net/readfic/123") == "chapters"


def _master(tmp_path: Path) -> Path:
    """Мини-копия официального архива: два раздела, картинка, перекрёстные ссылки."""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">
  <metadata/>
  <manifest>
    <item id="a" href="tutorial/index.xhtml" media-type="application/xhtml+xml"/>
    <item id="b" href="tutorial/intro.xhtml" media-type="application/xhtml+xml"/>
    <item id="c" href="library/functions.xhtml" media-type="application/xhtml+xml"/>
    <item id="d" href="glossary.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="a"/><itemref idref="b"/><itemref idref="c"/><itemref idref="d"/>
  </spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
  <navPoint id="n1" playOrder="1"><navLabel><text>The Python Tutorial</text></navLabel>
    <content src="tutorial/index.xhtml"/></navPoint>
  <navPoint id="n2" playOrder="2"><navLabel><text>Standard Library</text></navLabel>
    <content src="library/functions.xhtml"/></navPoint>
  <navPoint id="n3" playOrder="3"><navLabel><text>Glossary</text></navLabel>
    <content src="glossary.xhtml"/></navPoint>
</navMap></ncx>"""
    path = tmp_path / "master.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("_static/pygments.css", "body{}")
        z.writestr("_images/pic.png", b"\x89PNG")
        z.writestr(
            "tutorial/index.xhtml",
            '<html><head><title>The Python Tutorial — Python 3.14.7 documentation'
            "</title></head><body>"
            '<a href="intro.xhtml">внутрь части</a>'
            '<a href="../library/functions.xhtml#abs">в другую часть</a>'
            '<a href="https://peps.python.org/">наружу</a>'
            '<img src="../_images/pic.png"/></body></html>',
        )
        z.writestr(
            "tutorial/intro.xhtml",
            "<html><head><title>Whetting Your Appetite — Python 3.14.7 documentation"
            '</title></head><body><a href="../glossary.xhtml">глоссарий</a></body></html>',
        )
        z.writestr("library/functions.xhtml", "<html><body>lib</body></html>")
        z.writestr("glossary.xhtml", "<html><body>glossary</body></html>")
    return path


def test_build_part_keeps_only_its_own_documents(tmp_path):
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "tutorial/index.xhtml" in names and "tutorial/intro.xhtml" in names
        assert "library/functions.xhtml" not in names
        assert "glossary.xhtml" not in names
        # общие ресурсы и картинки, на которые часть ссылается, едут с ней
        assert "_static/pygments.css" in names and "_images/pic.png" in names
        # mimetype первым и без сжатия — иначе часть читалок не опознаёт epub
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_build_part_rewrites_only_outside_links(tmp_path):
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        html = z.read("tutorial/index.xhtml").decode()
        intro = z.read("tutorial/intro.xhtml").decode()
    # внутри части ссылка остаётся относительной
    assert 'href="intro.xhtml"' in html
    # за пределы части — абсолютной и на .html, иначе она ведёт в никуда
    assert 'href="https://docs.python.org/3/library/functions.html#abs"' in html
    assert 'href="https://docs.python.org/3/glossary.html"' in intro
    # чужие адреса не трогаем
    assert 'href="https://peps.python.org/"' in html


def test_toc_covers_every_document(tmp_path):
    """Раздел, представленный в оригинале ОДНОЙ точкой оглавления (howto), не
    должен превращаться в книгу с одним пунктом — по ней нельзя навигировать."""
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        ncx = z.read("toc.ncx").decode()
    assert "tutorial/index.xhtml" in ncx
    assert "tutorial/intro.xhtml" in ncx
    # заголовок взят из документа и очищен от хвоста «— Python 3.14.7 …»
    assert "<text>Whetting Your Appetite</text>" in ncx


def test_part_has_embedded_cover(tmp_path):
    """Обложка вшита В КНИГУ, а не проставлена в БД отдельным шагом.

    Так она переживает каждое обновление версии сама: после замены файла
    `_apply_file` достаёт её из epub тем же путём, что и у обычных книг, и
    ленивая ИИ-генерация обложек к этим книгам не подключается.
    """
    from backend.app import covers

    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        opf = z.read("content.opf").decode()
        assert "cover.png" in z.namelist()
        # объявлена и по-epub3, и по-epub2: читалки ищут по-разному
        assert 'properties="cover-image"' in opf
        assert '<meta name="cover" content="cover-img"/>' in opf
        # первой страницей книги, иначе обложку никто не увидит при чтении
        assert opf.index('idref="cover-page"') < opf.index('idref="i0"')
    # извлекатель обложек самой читалки обязан её найти
    assert covers.extract_cover(out, "epub", "pytest-pythondocs") is not None


def test_download_result_carries_version_metric(tmp_path, monkeypatch):
    master = _master(tmp_path)
    monkeypatch.setattr(pd, "current_version", lambda: ("3.14.7", 31407))
    monkeypatch.setattr(pd, "fetch_master", lambda ver, vint: master)
    res = pd.download(BASE + "tutorial/")
    assert res.site == "pythondocs"
    assert res.source_url == BASE + "tutorial/"
    # название БЕЗ версии: иначе следующий релиз заведёт вторую книгу-дубль
    assert "3.14" not in res.title
    assert res.extra["update_metric"] == 31407
    assert res.extra["authoritative"] is True
    Path(res.file_path).unlink(missing_ok=True)


# --- официальный русский перевод как засев кэша -------------------------------


def _lang_master(tmp_path, name: str, docs: dict[str, str]):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for href, body in docs.items():
            z.writestr(href, f"<html><body>{body}</body></html>")
    return path


def test_ru_pairs_align_by_position(tmp_path):
    from backend.app.translation_cache import key as cache_key
    from backend.downloaders import pythondocs_ru as pr

    en = _lang_master(tmp_path, "en.epub", {
        "tutorial/a.xhtml": "<p>Hello world of Python</p><p>Not translated yet</p>",
        # второй документ: в русской сборке блоков БОЛЬШЕ — выравнивать нельзя
        "tutorial/b.xhtml": "<p>Some paragraph about modules</p>",
    })
    ru = _lang_master(tmp_path, "ru.epub", {
        "tutorial/a.xhtml": "<p>Привет, мир Python</p><p>Not translated yet</p>",
        "tutorial/b.xhtml": "<p>Какой-то абзац о модулях</p><p>лишний блок</p>",
    })
    rows, stats = pr.pairs(en, ru)
    assert stats["aligned"] == 1 and stats["mismatch"] == 1
    # непереведённый абзац в кэш не кладём: он вернулся бы как «перевод» самого себя
    assert stats["untranslated"] == 1
    assert rows == [(cache_key("Hello world of Python", "other", "ru"), "Привет, мир Python")]


def test_ru_blocks_match_browser_text_content(tmp_path):
    """Ключ кэша считается по тексту блока — ровно как его отдаёт textContent.

    Внутри абзаца документации почти всегда есть <code>; склей потомков через
    пробел — и хэш разойдётся с тем, что пришлёт читалка, а засеянный перевод
    молча перестанет находиться.
    """
    from backend.downloaders import pythondocs_ru as pr

    got = pr._blocks(b"<html><body><p>Use <code>print()</code> here</p></body></html>")
    assert got == ["Use print() here"]


def test_generated_xml_escapes_paths_from_foreign_archive(tmp_path):
    """Пути приезжают из ЧУЖОГО архива и попадают в наш XML как есть.

    Экранировался только заголовок; `&` в имени файла делал content.opf
    неразбираемым — книга становилась битой, причём молча и только на тех
    версиях документации, где такой файл появится.
    """
    import xml.etree.ElementTree as ET

    from backend.downloaders import pythondocs as _pd

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">
  <metadata/>
  <manifest><item id="a" href="tutorial/a&amp;b.xhtml"
    media-type="application/xhtml+xml"/></manifest>
  <spine toc="ncx"><itemref idref="a"/></spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
  <navPoint id="n1" playOrder="1"><navLabel><text>A &amp; B</text></navLabel>
    <content src="tutorial/a&amp;b.xhtml"/></navPoint>
</navMap></ncx>"""
    master = tmp_path / "master.epub"
    with zipfile.ZipFile(master, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("tutorial/a&b.xhtml", "<html><body><p>x</p></body></html>")

    out = _pd.build_part(master, "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        # разбирается парсером — единственная проверка, которая тут что-то значит
        ET.fromstring(z.read("content.opf"))
        ET.fromstring(z.read("toc.ncx"))
        ET.fromstring(z.read("nav.xhtml"))


# --------------------------------------------------------------------------
# нарезка переросших документов
# --------------------------------------------------------------------------
def _release(series: str, micro: int, filler: int) -> str:
    """Секция одного выпуска: заголовок задаёт серию, тело — вес."""
    rid = f"python-{series.replace('.', '-')}-{micro}-final"
    return (
        f'<section id="{rid}"><h2>Python {series}.{micro} final</h2>'
        f'<section id="{rid}-lib"><h3>Library</h3><p>{"x" * filler}</p></section>'
        "</section>"
    )


def _master_with_fat_changelog(tmp_path: Path, releases: list[tuple[str, int]]) -> Path:
    """Мастер с одним переросшим документом — как whatsnew/changelog.xhtml."""
    body = "".join(_release(s, m, 200_000) for s, m in releases)
    changelog = (
        "<html><head><title>Changelog — Python 3.14.7 documentation</title></head>"
        '<body><section id="changelog"><h1>Changelog</h1>'
        f"{body}</section></body></html>"
    )
    first = f"python-{releases[0][0].replace('.', '-')}-{releases[0][1]}-final"
    last = f"python-{releases[-1][0].replace('.', '-')}-{releases[-1][1]}-final"
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">
  <metadata/>
  <manifest>
    <item id="a" href="whatsnew/index.xhtml" media-type="application/xhtml+xml"/>
    <item id="b" href="whatsnew/changelog.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="a"/><itemref idref="b"/></spine>
</package>"""
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
  <navPoint id="n1" playOrder="1"><navLabel><text>What's New</text></navLabel>
    <content src="whatsnew/index.xhtml"/></navPoint>
  <navPoint id="n2" playOrder="2"><navLabel><text>Changelog</text></navLabel>
    <content src="whatsnew/changelog.xhtml"/>
    <navPoint id="n3" playOrder="3"><navLabel><text>first</text></navLabel>
      <content src="whatsnew/changelog.xhtml#{first}"/></navPoint>
    <navPoint id="n4" playOrder="4"><navLabel><text>last</text></navLabel>
      <content src="whatsnew/changelog.xhtml#{last}"/></navPoint>
  </navPoint>
</navMap></ncx>"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "master-big.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr(
            "whatsnew/index.xhtml",
            "<html><head><title>What's New</title></head><body>"
            f'<a href="changelog.xhtml#{last}">последний выпуск</a></body></html>',
        )
        z.writestr("whatsnew/changelog.xhtml", changelog)
    return path


def _docs_of(epub: Path) -> dict[str, str]:
    with zipfile.ZipFile(epub) as z:
        return {
            n: z.read(n).decode()
            for n in z.namelist()
            if n.endswith(".xhtml") and n != "cover.xhtml"
        }


def _anchors_of(docs: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, html in docs.items():
        for m in re.finditer(r'\bid="([^"]+)"', html):
            out.setdefault(m.group(1), name)
    return out


RELEASES = [("3.14", 2), ("3.14", 1), ("3.14", 0), ("3.13", 1), ("3.13", 0), ("3.12", 0)]


def test_oversized_document_is_split_into_chapters(tmp_path):
    """Глава на мегабайты вешает вкладку читалки: пагинатор обходит Range по
    сотням тысяч узлов. Ни один документ книги не должен быть толще потолка."""
    master = _master_with_fat_changelog(tmp_path, RELEASES)
    out = pd.build_part(master, "whatsnew", "3.14.7", tmp_path / "part.epub")
    docs = _docs_of(out)
    assert len(docs) > 2, "переросший документ не нарезан"
    for name, html in docs.items():
        assert len(html.encode()) <= pd.MAX_DOC_BYTES * 1.05, f"{name} толще потолка"
    # первый кусок сохраняет исходное имя — на него ведут ссылки и закладки
    assert "whatsnew/changelog.xhtml" in docs


def test_split_keeps_every_anchor_reachable(tmp_path):
    """Ссылка на выпуск обязана вести в тот кусок, где выпуск оказался.

    Это ломается молча: ссылка `changelog.xhtml#python-3-12-0-final` ведёт в
    первый кусок, якоря там нет, читалка просто ничего не делает."""
    master = _master_with_fat_changelog(tmp_path, RELEASES)
    out = pd.build_part(master, "whatsnew", "3.14.7", tmp_path / "part.epub")
    docs = _docs_of(out)
    anchors = _anchors_of(docs)
    with zipfile.ZipFile(out) as z:
        ncx = z.read("toc.ncx").decode()

    broken = []
    for name, html in docs.items():
        base = posixpath.dirname(name)
        for m in re.finditer(r'href="([^"#]*)#([^"]+)"', html):
            path, anchor = m.group(1), m.group(2)
            if "://" in path:
                continue
            target = posixpath.normpath(posixpath.join(base, path)) if path else name
            if anchors.get(anchor) != target:
                broken.append((name, m.group(0)))
    assert not broken, f"ссылки ведут не в тот кусок: {broken[:3]}"

    for m in re.finditer(r'src="([^"#]+)#([^"]+)"', ncx):
        assert anchors.get(m.group(2)) == m.group(1), f"оглавление: {m.group(0)}"


def test_split_boundaries_survive_a_new_release(tmp_path):
    """Границы нарезки привязаны к минорной серии, а не к счётчику байтов.

    Иначе новый выпуск сдвигает все границы, каждое ↻ перекраивает книгу, и
    сохранённая позиция чтения перестаёт что-либо значить."""
    before = pd.build_part(
        _master_with_fat_changelog(tmp_path / "a", RELEASES),
        "whatsnew", "3.14.7", tmp_path / "before.epub",
    )
    after = pd.build_part(
        _master_with_fat_changelog(tmp_path / "b", [("3.14", 3), *RELEASES]),
        "whatsnew", "3.14.8", tmp_path / "after.epub",
    )
    old, new = _docs_of(before), _docs_of(after)
    frozen = [n for n in old if "3.12" in n or "3.13" in n]
    assert frozen, "нечего проверять: старые серии не выделились в куски"
    for name in frozen:
        assert name in new, f"кусок {name} исчез после нового выпуска"
        assert old[name] == new[name], f"кусок {name} перекроен новым выпуском"


def test_unsplittable_document_passes_through_loudly(tmp_path, caplog):
    """Резать не по чему — отдаём целиком, но в лог, а не молча: книга с такой
    главой открывается десятки секунд, и это должно быть видно."""
    fat = "<html><head><title>Fat</title></head><body><p>" + "y" * (
        pd.MAX_DOC_BYTES + 1000
    ) + "</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="reader.pythondocs"):
        order, bodies, anchors = pd._split_doc("whatsnew/fat.xhtml", fat)
    assert order == ["whatsnew/fat.xhtml"] and not anchors
    assert bodies["whatsnew/fat.xhtml"] == fat
    assert any("нарезки" in r.getMessage() for r in caplog.records)
