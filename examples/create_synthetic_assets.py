"""Create synthetic demo INPUTS only; never manufacture classifier results.

Run from any directory: python -B examples/create_synthetic_assets.py
Existing assets are not overwritten unless --replace is explicitly supplied.
All prose and identities below are fictional and authored for this example.
"""
import argparse
from io import BytesIO
from pathlib import Path

import openpyxl
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
TITLE = "DemoBook Aurora 101"
AUTHOR = "Avery Demo"
FOREIGN_TITLE = "DemoBook Boreal 202"
FOREIGN_AUTHOR = "Robin Sample"
HEADERS = ["Правообладатель", "Автор", "Название", "Тип контента", "url", "Сайт"]


def synthetic_workbook():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Records"
    sheet.append(HEADERS)
    rows = [
        (AUTHOR, TITLE, "ebook", "https://library.example/aurora-101", "library.example"),
        (AUTHOR, TITLE, "ebook", "https://library.example/aurora-101", "library.example"),
        (AUTHOR, TITLE, "ebook", "https://s4.library.example/aurora-101", "s4.library.example"),
        (FOREIGN_AUTHOR, TITLE, "ebook", "https://library.example/aurora-101", "library.example"),
        (AUTHOR, TITLE, "audio", "https://library.example/aurora-101-audio", "library.example"),
        ("Мирон Макет", "Учебная карта. Том 3", "ebook", "https://library.example/uchebnaya-karta-tom-4", "library.example"),
        ("Мирон Макет", "Учебная карта. Том 4", "ebook", "https://library.example/uchebnaya-karta-tom-4", "library.example"),
        ("Мирон Пример", "Картаветра", "ebook", "https://library.example/karta-vetra", "library.example"),
        ("Пример Мирон", "Картаветра", "ebook", "https://library.example/author-variant", "library.example"),
        ("Мирон Пример", "Карта ветра", "ebook", "https://library.example/spaced-title", "library.example"),
        (None, "Demo Missing", "ebook", None, "library.example"),
        (AUTHOR, "Demo Index", "ebook", "https://library.example/authors/avery", "library.example"),
        (FOREIGN_AUTHOR, "Demo Mismatch", "ebook", "https://library.example/mismatch", "mirror.example"),
        ("Мирон Макет", "Картасинегогорода", "ebook", "https://library.example/karta-sinego-goroda", "library.example"),
        ("Мирон Макет", "Шаг", "ebook", "https://library.example/shag-po-maketu-goroda-proba-zapis", "library.example"),
        (AUTHOR, "Demo Aurora Room", "ebook", "https://library.example/copper-river-notes", "library.example"),
        (AUTHOR + ";", " ;Demo Compass; ", "ebook", "https://library.example/demo-compass", "library.example"),
        (FOREIGN_AUTHOR, "Demo Invalid URL", "ebook", "not-a-url", "library.example"),
    ]
    for row in rows:
        sheet.append(("Demo Rights",) + row)
    catalog = workbook.create_sheet("Catalog")
    catalog.append(HEADERS)
    for name in ("own.html", "foreign.html", "uncertain.html", "own.pdf", "foreign.pdf", "review.pdf", "dynamic.html"):
        catalog.append(("Demo Rights", AUTHOR, TITLE, "ebook", "https://library.example/" + name, "library.example"))
    for current in workbook:
        current.freeze_panes = "A2"
        current.auto_filter.ref = current.dimensions
        for row in current:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    workbook.properties.creator = "Book Content Monitor synthetic demo"
    workbook.properties.lastModifiedBy = "Book Content Monitor synthetic demo"
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def card(title, author, filename):
    return ('<article class="book-card"><h1>' + title + '</h1><p class="book-author">' + author + '</p>'
            '<p>Fictional training edition. Synthetic library catalog. Available reading formats are listed below.</p>'
            '<a href="/files/' + filename + '">Download EPUB</a></article>')


def document(body, title="Synthetic library"):
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + title + '</title></head><body><main>' + body + '</main></body></html>\n'


def pdf_document(title, author, review=False):
    writer = PdfWriter()
    metadata = {"/Title": title, "/Author": author, "/Creator": "Synthetic demo generator"}
    if review:
        metadata["/Subject"] = "Book review"
    writer.add_metadata(metadata)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for page_number in range(2):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
        if review:
            lines = [title, author, 'Book review', 'This review discusses the plot and characters of the novel.']
            lines += ['The reviewer examines the imaginary journey and gives an opinion on its themes.'] * 26
        else:
            lines = [title, author, 'Chapter ' + str(page_number + 1)]
            lines += ['The traveler crossed the quiet bridge and opened a small notebook beside the river.',
                      'A lantern marked the path ahead while distant bells announced the evening arrival.'] * 15
        stream = DecodedStreamObject()
        payload = 'BT /F1 10 Tf 40 750 Td 14 TL\n'
        for line in lines:
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            payload += '(' + escaped + ') Tj T*\n'
        stream.set_data((payload + 'ET\n').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def create_assets(replace=False):
    own = card(TITLE, AUTHOR, 'aurora.epub')
    dynamic = document('<div id="app"></div><p id="loading">Loading. Please enable JavaScript.</p>')
    import json
    dynamic = dynamic.replace('</body>', '<script>setTimeout(() => {document.title="Book details"; document.querySelector("main").innerHTML=' + json.dumps(own) + ';}, 350);</script></body>')
    assets = {
        'examples/synthetic_input.xlsx': synthetic_workbook(),
        'examples/fixtures/own.html': document(own),
        'examples/fixtures/foreign.html': document(card(FOREIGN_TITLE, FOREIGN_AUTHOR, 'boreal.epub')),
        'examples/fixtures/uncertain.html': document('<h1>Loading</h1><p>Please enable JavaScript. Book content is loading.</p><div id="app"></div><script src="/unavailable-app.js"></script>', 'Loading'),
        'examples/fixtures/dynamic.html': dynamic,
        'examples/fixtures/own.pdf': pdf_document(TITLE, AUTHOR),
        'examples/fixtures/foreign.pdf': pdf_document(FOREIGN_TITLE, FOREIGN_AUTHOR),
        'examples/fixtures/review.pdf': pdf_document(TITLE, AUTHOR, review=True),
    }
    targets = [ROOT / relative for relative in assets]
    existing = [p.relative_to(ROOT).as_posix() for p in targets if p.exists()]
    if existing and not replace:
        raise FileExistsError('Refusing to replace existing assets: ' + ', '.join(existing))
    for relative, value in assets.items():
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode('utf-8') if isinstance(value, str) else value)
    print('Created 8 synthetic INPUT assets; no classification or export was run.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true')
    create_assets(parser.parse_args().replace)
