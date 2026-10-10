"""Attested real emissions: loss is fidelity-valid, never verified coverage."""
import copy,json,unittest,os
from pathlib import Path
from fidelity.recorded import check_case
ROOT=Path(os.environ.get('BEHAVIOR_RECORDED_FIXTURES','/nonexistent-recorded-fixtures'))


class RecordedFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ROOT.exists():raise unittest.SkipTest('set BEHAVIOR_RECORDED_FIXTURES to the attested integration archive')
        cls.catalog=json.loads((ROOT/'recorded-base-catalog-amendment-2.json').read_text())

    def inputs(self, ident):
        entry=next(v for v in self.catalog.values() if v['id']==ident)
        case=dict(id=ident,trace=json.loads(Path(entry['base_path']).read_text()),
            capture=json.loads((Path(entry['base_path']).parent/'capture.json').read_text()),
            derivation={**{k:entry[k] for k in ('base_path','base_sha256','receipt_path','receipt_sha256','source_identity','recorder_identity')},'transforms':[]})
        return case,json.loads(Path(entry['bundle_path']).read_text())

    def test_actual_complete_and_lossy_bases(self):
        for ident in ['s01','s03','s06','s12','s13','s14','s18']:
            with self.subTest(ident=ident):
                case,bundle=self.inputs(ident);row=check_case(case,self.catalog,bundle)
                self.assertTrue(row['pass'],row)
                self.assertEqual(row['coverage_status'],'UNVERIFIED' if int(ident[1:])>=13 else 'VERIFIED')

    def test_loss_cannot_claim_complete_capture(self):
        case,bundle=self.inputs('s13');case['capture']['loss']['gaps']=[]
        row=check_case(case,self.catalog,bundle)
        self.assertFalse(row['pass']);self.assertIn('capture claims differ',str(row['errors']))

    def test_bundle_cannot_restore_missing_occurrence(self):
        case,bundle=self.inputs('s14');bundle['retained']['loss']['gaps']=[]
        self.assertFalse(check_case(case,self.catalog,bundle)['pass'])

    def test_case_cannot_change_model_response(self):
        case,bundle=self.inputs('s01');case['trace']['events'][-1]['output']='invented'
        self.assertFalse(check_case(case,self.catalog,bundle)['pass'])

    def test_no_transform_or_unenrolled_base(self):
        case,bundle=self.inputs('s01');case['derivation']['transforms']=[{'op':'undeclare_scope'}]
        self.assertFalse(check_case(case,self.catalog,bundle)['pass'])
        case,bundle=self.inputs('s01');self.assertFalse(check_case(case,{},bundle)['pass'])

    def test_legacy_r12_still_rejected(self):
        from fidelity import check_case as legacy
        prior=ROOT.parent/'behavior-megan-automatic-qualification2-20261010'
        cases=json.loads((prior/'sealed/cases.json').read_text());bundles=json.loads((prior/'sealed/provenance-bundles.json').read_text())
        row=legacy(next(c for c in cases if c['id']=='r12'),{},bundles['r12'])
        self.assertFalse(row['pass']);self.assertIn('tool_result missing tool_use_id',str(row))

if __name__=='__main__':unittest.main()
