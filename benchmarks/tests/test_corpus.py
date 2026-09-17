import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare import PreparationError, SOURCE_LIMIT, extract, prepare, safe_path, exclusive
from validate import ValidationError, validate, load
from coverage import private_cache

CORPUS = Path(__file__).resolve().parents[1]


class CorpusTests(unittest.TestCase):
    @contextlib.contextmanager
    def corpus(self):
        with tempfile.TemporaryDirectory(prefix='corpus-validation-') as temp:
            root=Path(temp)/'corpus'
            shutil.copytree(CORPUS,root,ignore=lambda directory, names:
                            ['__pycache__'] + (['tests'] if Path(directory) == CORPUS else []))
            yield root

    def change(self,root,fn):
        manifest=load(root,'manifest.json');fn(manifest)
        (root/'manifest.json').write_text(json.dumps(manifest))

    def test_complete_authoring_corpus(self):
        self.assertEqual(validate(CORPUS,allow_pending=True)['tasks'],40)

    def test_duplicate_ids(self):
        with self.corpus() as root:
            self.change(root,lambda m:m['tasks'][1].update(id=m['tasks'][0]['id']))
            with self.assertRaisesRegex(ValidationError,'duplicate task ID'):validate(root,True)

    def test_invalid_ranges(self):
        for start,end in [(0,1),(10,9),(1,999999),(True,2)]:
            with self.subTest(start=start,end=end),self.corpus() as root:
                self.change(root,lambda m:m['tasks'][0]['evidence_groups'][0]['alternatives'][0].update(start=start,end=end))
                with self.assertRaisesRegex(ValidationError,'invalid source range'):validate(root,True)

    def test_unresolved_pin_and_missing_notice(self):
        with self.corpus() as root:
            sources=load(root,'sources.json');sources[0]['commit']='HEAD'
            (root/'sources.json').write_text(json.dumps(sources))
            with self.assertRaisesRegex(ValidationError,'Unresolved source'):validate(root,True)
        with self.corpus() as root:
            sources=load(root,'sources.json');(root/sources[0]['licenses'][0]['path']).unlink()
            with self.assertRaisesRegex(ValidationError,'Artifact unavailable'):validate(root,True)

    def test_partition_leakage(self):
        with self.corpus() as root:
            self.change(root,lambda m:m['tasks'][0].update(split='development' if m['tasks'][0]['split']=='held_out' else 'held_out'))
            with self.assertRaisesRegex(ValidationError,'Partition differs'):validate(root,True)

    def test_unavailable_source(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValidationError,'Artifact unavailable'):validate(CORPUS,True,Path(temp))

    def test_alternative_groups(self):
        for field,value,message in [('alternatives',[],'alternative-evidence'),('required','yes','alternative-evidence')]:
            with self.subTest(field=field),self.corpus() as root:
                self.change(root,lambda m:m['tasks'][0]['evidence_groups'][0].update({field:value}))
                with self.assertRaisesRegex(ValidationError,message):validate(root,True)
        with self.corpus() as root:
            self.change(root,lambda m:m['tasks'][0]['claims'][0].update(evidence_groups=['absent']))
            with self.assertRaisesRegex(ValidationError,'absent or optional evidence'):validate(root,True)

    def test_review_evidence_required(self):
        with self.corpus() as root:
            self.change(root,lambda m:m['tasks'][0].update(review={'status':'pending'}))
            with self.assertRaisesRegex(ValidationError,'review pending'):validate(root)

    def test_missing_data_and_duplicate_json_keys(self):
        with self.corpus() as root:
            self.change(root,lambda m:m['tasks'][0].update(question=''))
            with self.assertRaisesRegex(ValidationError,'missing question'):validate(root,True)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'bad.json').write_text('{"id":1,"id":2}')
            with self.assertRaisesRegex(ValidationError,'Duplicate JSON field'):load(root,'bad.json')

    def test_path_traversal(self):
        for path in ['../outside','/tmp/outside','a/../../outside','a\\outside','.git/config','a/./b']:
            with self.subTest(path=path),self.assertRaises(PreparationError):safe_path(path)

    def archive(self,root,name,content=b'x',link=None):
        path=root/'input.tar'
        with tarfile.open(path,'w') as tar:
            item=tarfile.TarInfo(name)
            if link is not None:item.type=tarfile.SYMTYPE;item.linkname=link;tar.addfile(item)
            else:item.size=len(content);tar.addfile(item,io.BytesIO(content))
        return path

    def test_archive_paths_links_and_bytes(self):
        for name,link in [('source/../escape',None),('source/link','../../escape'),('source/.git/config',None)]:
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();archive=self.archive(root,name,link=link)
                with self.assertRaises(PreparationError):extract(archive,root/'out',100)
                self.assertFalse((root/'escape').exists())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();archive=self.archive(root,'source/file',b'oversized')
            with self.assertRaisesRegex(PreparationError,'byte limit'):extract(archive,root/'out',2)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();archive=self.archive(root,'source/file',b'kept\r\n')
            extract(archive,root/'out',6);self.assertEqual((root/'out/file').read_bytes(),b'kept\r\n')

    def test_oversized_source_rejected_before_fetch(self):
        source=copy.deepcopy(load(CORPUS,'sources.json')[0]);source['source_bytes']=SOURCE_LIMIT+1
        with tempfile.TemporaryDirectory() as temp,self.assertRaisesRegex(PreparationError,'corpus limit'):
            prepare(source,Path(temp))

    def test_preparation_serialization(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with exclusive(root),self.assertRaisesRegex(PreparationError,'Another source'):
                with exclusive(root):pass

    def test_cache_cleanup_retains_active_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            with private_cache(Path(temp)) as cache:
                (cache/'index.sqlite').write_bytes(b'owned')
            self.assertFalse(cache.exists())
            with private_cache(Path(temp)) as cache:
                (cache/'daemon.sock').touch()
            self.assertTrue(cache.exists())


if __name__=='__main__':
    unittest.main()
