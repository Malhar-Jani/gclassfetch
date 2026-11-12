import copy
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(app.state)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        app.STOP.clear()
        app.state.update(total_files=0, done_files=0, failed_files=0, skipped_files=0,
                         total_bytes=0, done_bytes=0, issues=[], linked_files=0,
                         courses={'c': dict(name='Course', status='scanning', total=0, done=0, failed=0)})
        self.dl = app.Downloader(None, workers=4)

    def tearDown(self):
        self.dl.dl_pool.shutdown(wait=True)
        app.state.clear()
        app.state.update(self.original)
        app.STOP.clear()
        self.tmp.cleanup()

    def item(self, fid='a', body=b'abc', name='notes.pdf'):
        return dict(fid=fid, dir=self.root, title=name, cid='c', meta=dict(
            id=fid, name=name, mimeType='application/pdf', size=str(len(body)),
            modifiedTime='v1', md5Checksum=hashlib.md5(body).hexdigest()))

    def transfer(self, factory, fh):
        req = factory()
        fh.write(req.body)
        self.dl.bump_bytes(len(req.body))
        return len(req.body)

    def setup_media(self, contents):
        drive = MagicMock()
        drive.files.return_value.get_media.side_effect = lambda fileId, **kw: SimpleNamespace(body=contents[fileId], headers={})
        self.dl.api = SimpleNamespace(drive=drive)
        self.dl.transfer = self.transfer

    def scan_setup(self, materials):
        classroom = MagicMock()
        classroom.courses.return_value.topics.return_value.list.return_value.execute.return_value = {'topic': []}
        classroom.courses.return_value.courseWorkMaterials.return_value.list.return_value.execute.return_value = {'courseWorkMaterial': materials}
        classroom.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {'courseWork': []}
        classroom.courses.return_value.announcements.return_value.list.return_value.execute.return_value = {'announcements': []}
        self.dl.api = SimpleNamespace(classroom=classroom)
        return classroom

    def test_drive_url_variants(self):
        urls = [
            ('https://drive.google.com/uc?export=download&id=abc', ('abc', 'file')),
            ('https://drive.google.com/open?usp=sharing&id=abc', ('abc', 'file')),
            ('https://docs.google.com/presentation/u/1/d/abc/edit', ('abc', 'file')),
            ('https://drive.google.com/drive/u/2/folders/abc?resourcekey=key', ('abc', 'folder')),
            ('https://drive.google.com/folderview?usp=sharing&id=abc', ('abc', 'folder')),
            ('https://www.google.com/url?q=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2Fabc%2Fview', ('abc', 'file')),
            ('https://drive.google.com.evil.test/file/d/abc', (None, None)),
            ('https://example.com/?url=drive.google.com/file/d/abc', (None, None)),
            ('https://docs.google.com/forms/d/e/published-id/viewform', (None, None)),
            ('https://docs.google.com/presentation/d/e/published-id/pub', (None, None)),
        ]
        for url, expected in urls:
            with self.subTest(url=url):
                self.assertEqual(app.extract_drive_ref(url), expected)

    def test_pagination_and_stop_does_not_break_course_refresh(self):
        method = MagicMock()
        method.return_value.execute.side_effect = [{'files': [1], 'nextPageToken': 'next'}, {'files': [2]}]
        app.STOP.set()
        self.assertEqual(app.paginate(method, 'files', cancel=False), [1, 2])
        self.assertEqual(method.call_args.kwargs['pageToken'], 'next')

    def test_batch_item_failure_is_retried_and_reported(self):
        api = MagicMock()
        def make_batch(callback):
            batch = MagicMock()
            batch.execute.side_effect = lambda: [callback('ok', {'id': 'ok'}, None), callback('bad', None, ValueError('denied'))]
            return batch
        api.drive.new_batch_http_request.side_effect = make_batch
        api.drive.files.return_value.get.return_value.execute.side_effect = ValueError('still denied')
        failures = []
        result = app.batch_metadata(api, ['ok', 'bad'], {'bad': 'key'}, lambda fid, exc: failures.append(fid))
        self.assertEqual(set(result), {'ok'})
        self.assertEqual(failures, ['bad'])
        self.assertEqual(api.drive.files.return_value.get.return_value.headers.__setitem__.call_args.args,
                         ('X-Goog-Drive-Resource-Keys', 'bad/key'))

    def test_different_files_same_name_and_size_are_both_saved(self):
        self.setup_media({'a': b'abc', 'b': b'xyz'})
        app.state['courses']['c']['total'] = 2
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.dl.download_one, [self.item('a', b'abc'), self.item('b', b'xyz')]))
        files = list(self.root.glob('*.pdf'))
        self.assertEqual({path.read_bytes() for path in files}, {b'abc', b'xyz'})
        self.assertEqual(app.state['done_files'], 2)
        self.assertEqual(app.state['skipped_files'], 0)

    def test_repeat_and_changed_file_and_local_corruption(self):
        self.setup_media({'a': b'abc'})
        item = self.item()
        self.dl.download_one(item)
        self.dl.download_one(item)
        self.assertEqual(app.state['skipped_files'], 1)
        (self.root / 'notes.pdf').write_bytes(b'bad')
        self.dl.download_one(item)
        self.assertEqual(app.state['skipped_files'], 1)
        changed = self.item('a', b'xyz')
        changed['meta']['modifiedTime'] = 'v2'
        self.setup_media({'a': b'xyz'})
        self.dl.download_one(changed)
        self.assertEqual(app.state['failed_files'], 0)
        self.assertIn(b'xyz', [p.read_bytes() for p in self.root.glob('*.pdf')])

    def test_empty_file_and_active_partial_are_safe(self):
        partial = self.root / 'notes.pdf.part'
        partial.write_bytes(b'active download')
        self.setup_media({'a': b''})
        self.dl.download_one(self.item(body=b''))
        self.dl.download_one(self.item(body=b''))
        self.assertEqual(partial.read_bytes(), b'active download')
        self.assertEqual((self.root / 'notes.pdf').read_bytes(), b'')
        self.assertEqual(app.state['skipped_files'], 1)

    def test_wrong_content_is_not_committed(self):
        self.setup_media({'a': b'bad'})
        self.dl.download_one(self.item(body=b'abc'))
        self.assertFalse(list(self.root.glob('*.pdf')))
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertEqual(app.state['failed_files'], 1)
        self.assertEqual(len(app.state['issues']), 1)
        self.assertEqual(app.state['done_bytes'], 0)

    def test_user_stop_is_not_reported_as_download_failure(self):
        self.setup_media({'a':b'abc'})
        def stop(factory, fh):
            app.STOP.set()
            raise app.DownloadFailed(RuntimeError('Stopped'),0)
        self.dl.transfer = stop
        self.dl.download_one(self.item())
        self.assertEqual(app.state['failed_files'],0)
        self.assertEqual(app.state['issues'],[])
        self.assertEqual(self.dl.report[0]['status'],'stopped')
        self.assertFalse(list(self.root.glob('*.part')))

    def test_deep_folders_and_cycle_and_repeated_attachments(self):
        self.scan_setup([{'id': 'post1', 'title': 'Notes', 'materials': [{'driveFile': {'driveFile': {'id': 'f0'}}}]},
                         {'id': 'post2', 'title': 'Revision', 'materials': [{'driveFile': {'driveFile': {'id': 'leaf'}}}]}])
        metas = {f'f{i}': dict(id=f'f{i}', name=f'Level {i}', mimeType='application/vnd.google-apps.folder') for i in range(11)}
        metas['leaf'] = self.item('leaf')['meta']
        metas['loop'] = dict(id='loop', name='Loop', mimeType='application/vnd.google-apps.shortcut', shortcutDetails={'targetId': 'f0'})
        def folder(fid, *args):
            return [metas[f'f{int(fid[1:]) + 1}']] if fid != 'f10' else [metas['leaf'], metas['loop']]
        self.dl.list_folder = folder
        with patch.object(app, 'batch_metadata', side_effect=lambda api, ids, *args: {fid: metas[fid] for fid in ids}):
            result = self.dl.scan_course({'id': 'c', 'name': 'Course'}, self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['dir'], self.root / 'Course' / 'General')
        self.assertFalse(any(p.is_dir() for p in result[0]['dir'].iterdir()))
        self.assertTrue(any('ancestor' in issue['reason'] for issue in app.state['issues']))

    def test_repeated_attachments_save_once_and_post_text_is_aggregated(self):
        posts = [{'id': id, 'title': 'Lecture', 'description': 'Read this', 'materials': [{'driveFile': {'id': 'file'}}]} for id in ['p1', 'p2']]
        self.scan_setup(posts)
        with patch.object(app, 'batch_metadata', return_value={'file': self.item('file')['meta']}):
            first = self.dl.scan_course({'id': 'c', 'name': 'Course'}, self.root)
            self.dl.scan_course({'id': 'c', 'name': 'Course'}, self.root)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['dir'], self.root / 'Course' / 'General')
        self.assertEqual((first[0]['dir'] / 'Classroom notes.txt').read_text().count('Read this'), 1)

    def test_announcements_share_one_folder_without_id_folders(self):
        classroom = self.scan_setup([])
        classroom.courses.return_value.announcements.return_value.list.return_value.execute.return_value = {'announcements':[
            {'id':'12345','creationTime':'2026-11-12T10:00:00Z','text':'Read chapter 1', 'materials':[{'driveFile':{'id':'a'}}]},
            {'id':'67890','creationTime':'2026-11-13T10:00:00Z','text':'Read chapter 2', 'materials':[{'driveFile':{'id':'b'}}]}]}
        with patch.object(app,'batch_metadata',return_value={'a':self.item('a')['meta'],'b':self.item('b')['meta']}):
            result = self.dl.scan_course({'id':'c','name':'Course'},self.root)
        self.assertEqual(len(result),2)
        self.assertTrue(all(item['dir']==self.root/'Course'/'Announcements' for item in result))
        text=(self.root/'Course'/'Announcements'/'Classroom announcements.txt').read_text()
        self.assertIn('Read chapter 1',text)
        self.assertIn('Read chapter 2',text)
        self.assertFalse(any(p.is_dir() for p in (self.root/'Course'/'Announcements').iterdir()))

    def test_flatten_legacy_folders_preserves_files_and_repeat_download_records(self):
        course=self.root/'Course'
        old=course/'_announcements'/'2026-11-12 123456789'
        old.mkdir(parents=True)
        (old/'notes.pdf').write_bytes(b'old pdf')
        (course/'notes.pdf').write_bytes(b'different pdf')
        (old/'description.txt').write_text('Read this chapter',encoding='utf-8')
        (old/'announcements.txt').write_text('Exam on Monday',encoding='utf-8')
        (old/'links.txt').write_text('https://example.com',encoding='utf-8')
        (old/'.gclassfetch.json').write_text(json.dumps({'a':{'filename':'notes.pdf','version':['v1'],'sha256':app.file_digest(old/'notes.pdf')}}),encoding='utf-8')
        self.assertEqual(app.flatten_course_folder(course),1)
        self.assertEqual((course/'notes.pdf').read_bytes(),b'different pdf')
        self.assertEqual((course/'notes (1).pdf').read_bytes(),b'old pdf')
        self.assertFalse((course/'_announcements').exists())
        self.assertIn('Read this chapter',(course/'Classroom notes.txt').read_text())
        self.assertIn('Exam on Monday',(course/'Classroom announcements.txt').read_text())
        record=json.loads((course/'.gclassfetch.json').read_text())
        self.assertEqual(record['a']['filename'],'notes (1).pdf')
        archives=list((self.root/'.gclassfetch-backups').rglob('original-text.zip'))
        self.assertEqual(len(archives),1)
        with app.zipfile.ZipFile(archives[0]) as archive:
            self.assertEqual(archive.read('_announcements/2026-11-12 123456789/description.txt'),b'Read this chapter')
        self.assertEqual(app.flatten_course_folder(course),0)

    def test_flatten_preserves_an_attachment_named_like_a_support_file(self):
        course=self.root/'Course'; old=course/'Topic'/'Post';old.mkdir(parents=True)
        (old/'links.txt').write_bytes(b'actual attachment')
        (old/'.gclassfetch.json').write_text(json.dumps({'a':{'filename':'links.txt','sha256':app.file_digest(old/'links.txt')}}))
        app.flatten_course_folder(course)
        self.assertEqual((course/'links.txt').read_bytes(),b'actual attachment')

    def test_cleanup_problem_does_not_prevent_finding_new_materials(self):
        self.scan_setup([{'id':'p','title':'Notes','materials':[{'driveFile':{'id':'a'}}]}])
        with patch.object(app,'prepare_course_folder',side_effect=OSError('Unfinished old download')), patch.object(app,'batch_metadata',return_value={'a':self.item()['meta']}):
            result=self.dl.scan_course({'id':'c','name':'Course'},self.root)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['dir'],self.root/'Course'/'General')
        self.assertTrue(any('old folders' in issue['reason'] for issue in app.state['issues']))

    def test_classroom_topics_are_kept_without_post_folders(self):
        classroom=self.scan_setup([
            {'id':'p1','title':'Lecture 1','topicId':'t1','materials':[{'driveFile':{'id':'a'}}]},
            {'id':'p2','title':'Lecture 2','topicId':'t1','materials':[{'driveFile':{'id':'b'}}]},
            {'id':'p3','title':'Practice','topicId':'t2','materials':[{'driveFile':{'id':'d'}}]}])
        classroom.courses.return_value.topics.return_value.list.return_value.execute.return_value={'topic':[
            {'topicId':'t1','name':'Class Notes'},{'topicId':'t2','name':'Tutorial Sheets'}]}
        classroom.courses.return_value.courseWork.return_value.list.return_value.execute.return_value={'courseWork':[
            {'id':'a1','title':'Assignment 1','materials':[{'driveFile':{'id':'e'}}]}]}
        with patch.object(app,'batch_metadata',return_value={fid:self.item(fid)['meta'] for fid in ['a','b','d','e']}):
            result=self.dl.scan_course({'id':'c','name':'Course'},self.root)
        paths={item['fid']:item['dir'].relative_to(self.root).as_posix() for item in result}
        self.assertEqual(paths,{'a':'Course/Class Notes','b':'Course/Class Notes','d':'Course/Tutorial Sheets','e':'Course/Assignments'})
        self.assertFalse(any('Lecture' in str(item['dir']) for item in result))

    def test_old_post_nesting_is_removed_but_topic_folders_are_kept(self):
        course=self.root/'Course';post=course/'Class Notes'/'Lecture 1';post.mkdir(parents=True)
        (post/'lecture.pdf').write_bytes(b'lecture')
        app.prepare_course_folder(course)
        self.assertEqual((course/'Class Notes'/'lecture.pdf').read_bytes(),b'lecture')
        self.assertFalse(post.exists())
        app.prepare_course_folder(course)
        self.assertTrue((course/'Class Notes'/'lecture.pdf').is_file())

    def test_flat_release_file_moves_to_topic_without_another_download(self):
        self.setup_media({'a':b'abc'})
        item=self.item()
        self.dl.download_one(item)
        grouped={**item,'course_dir':self.root,'dir':self.root/'Class Notes'}
        self.dl.download_one(grouped)
        self.assertFalse((self.root/'notes.pdf').exists())
        self.assertEqual((self.root/'Class Notes'/'notes.pdf').read_bytes(),b'abc')
        self.assertEqual(app.state['skipped_files'],1)
        self.assertEqual(app.state['failed_files'],0)
        self.assertEqual(json.loads((self.root/'.gclassfetch-moves.json').read_text())[0]['to'],'Class Notes/notes.pdf')

    def test_metadata_and_scan_failures_are_visible(self):
        classroom = self.scan_setup([{'id': 'p', 'title': 'Notes', 'materials': [{'driveFile': {'id': 'missing'}}]}])
        classroom.courses.return_value.announcements.return_value.list.return_value.execute.side_effect = ValueError('no access')
        def missing(api, ids, keys, on_error):
            on_error('missing', ValueError('file denied'))
            return {}
        with patch.object(app, 'batch_metadata', side_effect=missing):
            result = self.dl.scan_course({'id': 'c', 'name': 'Course'}, self.root)
        self.assertEqual(result, [])
        self.assertEqual(len(app.state['issues']), 2)
        self.assertEqual(app.state['courses']['c']['status'], 'done-warnings')

    def test_resource_key_reaches_metadata_and_download(self):
        self.scan_setup([{'id': 'p', 'title': 'Notes', 'description': 'https://drive.google.com/open?usp=sharing&id=a&resourcekey=secret'}])
        with patch.object(app, 'batch_metadata', return_value={'a': self.item()['meta']}) as batch:
            self.dl.scan_course({'id': 'c', 'name': 'Course'}, self.root)
            self.assertEqual(batch.call_args.args[2]['a'], 'secret')
        self.setup_media({'a': b'abc'})
        self.dl.download_one(self.item())
        self.assertEqual(self.dl.headers('a'), {'X-Goog-Drive-Resource-Keys': 'a/secret'})

    def test_offline_index_escapes_names_and_encodes_file_paths(self):
        directory = self.root / 'Course & one' / 'Topic' / 'Post'
        directory.mkdir(parents=True)
        (directory / 'a #1.pdf').write_bytes(b'pdf')
        (directory / '.gclassfetch.json').write_text('{}')
        app.generate_offline_index(self.root)
        output = (self.root / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Course &amp; one', output)
        self.assertIn('a%20%231.pdf', output)
        self.assertNotIn('.gclassfetch.json', output)

    def test_restricted_and_online_only_items_have_visible_outcomes(self):
        self.scan_setup([{'id': 'p', 'title': 'Notes', 'materials': [
            {'driveFile': {'id': 'restricted'}}, {'driveFile': {'id': 'form'}},
            {'youtubeVideo': {'id': 'video-id', 'title': 'Recording'}}]}])
        metas = {'restricted': dict(id='restricted', name='Read only.pdf', mimeType='application/pdf', capabilities={'canDownload':False}),
                 'form': dict(id='form', name='Quiz', mimeType='application/vnd.google-apps.form')}
        with patch.object(app, 'batch_metadata', return_value=metas):
            items = self.dl.scan_course({'id':'c','name':'Course'},self.root)
        self.assertEqual(items,[])
        self.assertEqual(app.state['linked_files'],2)
        self.assertEqual(len([i for i in app.state['issues'] if i['kind'] != 'link']),1)
        self.assertEqual(app.state['courses']['c']['status'],'done-warnings')

    def test_run_scan_failure_finishes_with_warning_and_report(self):
        classroom = self.scan_setup([])
        classroom.courses.return_value.list.return_value.execute.return_value = {'courses':[{'id':'c','name':'Course'}]}
        app.state['running'] = True
        with patch.object(self.dl,'scan_course',side_effect=ValueError('scan failed')), patch.object(app,'save_config'):
            self.dl.run('all',str(self.root))
        self.assertFalse(app.state['running'])
        self.assertEqual(app.state['phase'],'done-warnings')
        self.assertEqual(app.state['scan_done'],1)
        report = json.loads((self.root/'download-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['courses']['c']['status'],'error')
        self.assertEqual(len(report['issues']),1)

    def test_routes_validate_requests_and_keep_stopped_courses_refreshable(self):
        client = app.app.test_client()
        with patch.object(app,'get_current_credentials',return_value=object()), patch.object(app,'save_config'):
            response = client.post('/start/c',json={'workers':'bad'})
            self.assertEqual(response.status_code,400)
            response = client.post('/start/c',json={'options':{'include_materials':False,'include_coursework':False,'include_announcements':False}})
            self.assertEqual(response.status_code,400)
            self.assertEqual(client.post('/config',json={'workers':'bad'}).status_code,400)
            app.state['running'] = True
            self.assertEqual(client.post('/start/c',json={}).status_code,409)
        app.STOP.set()
        classroom = MagicMock()
        classroom.courses.return_value.list.return_value.execute.return_value={'courses':[{'id':'archived','name':'Past course','courseState':'ARCHIVED'}]}
        with patch.object(app,'get_current_credentials',return_value=object()), patch.object(app,'build',return_value=classroom):
            response = client.get('/courses?archived=true')
            self.assertEqual(response.get_json()[0]['id'],'archived')
            self.assertEqual(classroom.courses.return_value.list.call_args.kwargs['courseStates'],['ACTIVE','ARCHIVED'])


if __name__ == '__main__':
    unittest.main()
