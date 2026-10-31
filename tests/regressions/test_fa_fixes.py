"""Scoped regressions for FA-01/02/03; all HTTP and browser data are fixtures."""
from pathlib import Path
import json
import pytest
from pages import FetchResult, content_signals, extract_page
from part2 import Book, evaluate_page
from browser import BrowserProbe

URL='https://fixture.invalid/books/1234'
B=Book('target','Alice North','Silent Harbor')
V=Book('volume','Alice North','Silent Harbor. Volume 3')
FILE='<a href="/files/1234.epub">Download EPUB</a>'

def card(body='',book=B,tag='main'):
 return f'<{tag}><h1>{book.title}</h1><p class="book-author">{book.author}</p><p>Publisher: Harbor Press. Edition 2026. English language.</p>{body}</{tag}>'

def doc(html):return FetchResult(URL,URL,200,{'Content-Type':'text/html; charset=utf-8'},html.encode())
def result(html,book=B):return evaluate_page(doc(html),[book],book.id)
def script(data):return '<script type="application/ld+json">'+json.dumps(data)+'</script>'
def metadata(author=B.author,title=B.title):return {'@type':'Book','name':title,'author':{'@type':'Person','name':author}}
def foreign_links(n):return ''.join(f'<section><h2>Foreign Work {i}</h2><a href="/files/{i}.epub">Download EPUB</a></section>' for i in range(n))

@pytest.mark.parametrize('ext',['mp3','m4a','ogg','wav'])
@pytest.mark.parametrize('name,expected',[
 ('silent-harbor-volume-3',1),('silent-harbor-volume-III',1),('silent-harbor-volume-03',1),
 ('silent-harbor-volume-4',2),('silent-harbor-volume-IV',2),
 ('silent-harbor',1),('silent-harbor-2026-44',1),('silent-harbor-volume-3-2026-44',1),
 ('9999',1),('a2b3c4d5e6f71234a2b3c4d5e6f71234',1)])
def test_fa01_audio_file_ownership(ext,name,expected):
 r=result(card(f'<a href="/files/{name}.{ext}">Download {ext.upper()}</a>',V),V)
 assert r['status']==expected
 if expected==1:
  assert r['observed_formats']==['audio'];assert r['catalog_format']=='ebook';assert r['format_difference'] is True
 else:assert r['reason_code']=='content_scope_unresolved' and not r['content_signals']

@pytest.mark.parametrize('ext',['epub','mp3','m4a','ogg','wav'])
def test_fa01_book_id_conflict_is_not_a_storage_number(ext):
 conflict=result(card(f'<a href="/books/9999/file.{ext}">Download {ext.upper()}</a>'))
 storage=result(card(f'<a href="/files/9999.{ext}">Download {ext.upper()}</a>'))
 assert conflict['status']==2 and storage['status']==1

@pytest.mark.parametrize('ext',['mp3','m4a','ogg','wav'])
def test_fa01_independent_foreign_audio_cannot_confirm_target(ext):
 foreign=Book('foreign','Bob West','Foreign Work')
 html=card()+card(f'<a href="/files/9999.{ext}">Download {ext.upper()}</a>',foreign,tag='article')
 assert result(html)['status']==0

@pytest.mark.parametrize('count',[29,30,31])
@pytest.mark.parametrize('position',['before','after'])
@pytest.mark.parametrize('own',[True,False])
def test_fa02_unrelated_signals_do_not_displace_own_evidence(count,position,own):
 foreign=foreign_links(count);file=FILE if own else ''
 html=card(file+foreign if position=='before' else foreign+file)
 r=result(html);page=extract_page(doc(html))
 assert r['status']==(1 if own else 0)
 assert len(page.signals)<=30 and len(r['extracted']['observed_content_signals'])<=30
 assert r['extracted']['partial'] is False
 if own:assert {s['url'] for s in r['content_signals']}=={'https://fixture.invalid/files/1234.epub'}
 else:assert not r['content_signals']

@pytest.mark.parametrize('count',[29,30,31])
def test_fa02_unresolved_ownership_survives_foreign_observations(count):
 html=card(foreign_links(count)+'<a href="/files/silent-harbor-volume-4.epub">Download EPUB</a>',V)
 r=result(html,V)
 assert (r['status'],r['reason_code'])==(2,'content_scope_unresolved')
 assert not r['content_signals']

@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('own',[False,True])
def test_fa02_independent_cards_keep_target_and_observation_limit(reverse,own):
 target=card(FILE if own else '',tag='article');foreign=''.join(card(f'<a href="/files/{i}.epub">Download EPUB</a>',Book(str(i),'Bob West',f'Foreign Work {i}'),tag='article') for i in range(31))
 r=result('<main><h1>Library</h1>'+ (foreign+target if reverse else target+foreign)+'</main>')
 assert r['status']==(1 if own else 0)
 if own:assert all(s['scope_title']==B.title for s in r['content_signals'])
 else:assert not r['content_signals']
 assert len(r['extracted']['observed_content_signals'])<=30

@pytest.mark.parametrize('count',[29,30,31])
@pytest.mark.parametrize('position',['first','last'])
@pytest.mark.parametrize('filler',['empty','unrelated','agreeing_book'])
@pytest.mark.parametrize('conflict',[False,True])
def test_fa03_all_script_positions_contribute_to_the_final_decision(count,position,filler,conflict):
 # Count includes the meaningful final/first Book; exercise both script and Book truncation.
 fills=script({} if filler=='empty' else {'@type':'Organization','name':'Publisher'} if filler=='unrelated' else metadata())*(count-1)
 book=script(metadata('Bob West' if conflict else B.author))
 r=result(card(FILE)+(book+fills if position=='first' else fills+book))
 assert r['status']==(2 if conflict else 1)
 if conflict:assert r['reason_code']=='metadata_author_conflict'
 assert len(r['extracted']['subjects'])<=30

@pytest.mark.parametrize('count',[29,30,31])
def test_fa03_empty_nodes_do_not_create_uncertainty(count):
 fills=''.join(script([{},[],None,0][i%4]) for i in range(count))
 assert result(card(FILE)+fills)['status']==1
 assert result(card()+fills)['status']==0

@pytest.mark.parametrize('count',[29,30,31])
@pytest.mark.parametrize('author',[B.author,'Bob West',None])
def test_fa03_author_definition_after_padding_is_resolved_or_explicitly_unknown(count,author):
 book=script({'@type':'Book','name':B.title,'author':{'@id':'#person'}})
 definition=script({'@type':'Person','@id':'#person','name':author}) if author else ''
 r=result(card(FILE)+book+script({})*count+definition)
 assert r['status']==(1 if author==B.author else 2)
 if author=='Bob West':assert r['reason_code']=='metadata_author_conflict'
 if author is None:assert r['reason_code']=='author_missing'

@pytest.mark.parametrize('count',[29,30,31])
@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('own',[False,True])
def test_fa03_late_conflict_in_independent_card_cannot_cancel_target(count,reverse,own):
 target=card((FILE if own else '')+script(metadata()),tag='article')
 foreign=Book('foreign','Bob West','Foreign Work')
 neighbor=card(FILE+script(metadata('Carol East',foreign.title)),foreign,tag='article')
 html='<main><h1>Library</h1>'+script({})*count+(neighbor+target if reverse else target+neighbor)+'</main>'
 r=result(html)
 assert r['status']==(1 if own else 0)
 assert 'metadata_author_conflict' in r['extracted']['issues']
 if own:assert all(s['scope_title']==B.title for s in r['content_signals'])

@pytest.fixture
def real_browser():
 probe=BrowserProbe(timeout_ms=2500,fixture_routes={})
 assert probe.start(),probe.failure
 yield probe
 probe.close()

@pytest.mark.parametrize('case',['audio_foreign','audio_own','own_last','own_first','metadata_conflict_last','metadata_agreeing_last'])
def test_real_chromium_fa_decisions(real_browser,case):
 if case.startswith('audio'):
  book=V;volume=4 if case=='audio_foreign' else 3;html=card(f'<a href="/files/silent-harbor-volume-{volume}.mp3">Download MP3</a>',book);expected=2 if volume==4 else 1
 elif case.startswith('own'):
  book=B;html=card(foreign_links(31)+FILE if case=='own_last' else FILE+foreign_links(31));expected=1
 else:
  book=B;conflict=case=='metadata_conflict_last';html=card(FILE)+script(metadata())*30+script(metadata('Bob West' if conflict else B.author));expected=2 if conflict else 1
 real_browser.fixture_routes={URL:(200,html)}
 document=real_browser.check(URL);assert not document.error_code;assert document.visibility_checked
 r=evaluate_page(document,[book],book.id);assert r['status']==expected
