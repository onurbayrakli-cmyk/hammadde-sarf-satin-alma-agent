import copy
import json
import unittest
from datetime import datetime, timezone

from lfx.schema.message import Message
from manager_package import ProcurementManagerPackage
from procurement_research import INTAKE_MARKER, incoming_material_profile, normalize_profile
from quote_comparison import MARKER, RESULT_MARKER, SupplierQuoteComparisonComponent
from test_quote_comparison import sample_case

DUAL_NOW = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)

class DualInputResearchTests(unittest.TestCase):
    def context(self):
        return {'status':'ready','request':{'deadline_at':'2026-10-14T17:00:00+03:00','state':'collecting'},
                'materials':[{'material_code':'001','name':'Reçine A','quantity':'123456'},
                             {'material_code':'002','name':'Reçine B'}]}
    def source(self,ctx):return INTAKE_MARKER+json.dumps(ctx)+'\n\nKESİN YEREL HESAP (JSON)\n{}'
    def test_all_incoming_materials_and_exclusions_preserved(self):
        raw=incoming_material_profile(self.source(self.context()),{'selected_excluded_suppliers':['Mevcut Firma']},now=DUAL_NOW)
        profile=normalize_profile(raw)
        self.assertEqual([r['material_code'] for r in profile['materials']],['001','002'])
        self.assertEqual(profile['materials'][0]['existing_suppliers'],['Mevcut Firma'])
        self.assertNotIn('quantity',profile['materials'][0])
    def test_missing_deadline_blocks_search(self):
        ctx=self.context();ctx['request']['deadline_at']=None
        with self.assertRaises(ValueError):incoming_material_profile(self.source(ctx),{},now=DUAL_NOW)
    def test_awaiting_owner_blocks_search(self):
        ctx=self.context();ctx['request']['state']='awaiting_deadline'
        with self.assertRaises(ValueError):incoming_material_profile(self.source(ctx),{},now=DUAL_NOW)
    def test_invalid_context_blocks_search(self):
        ctx=self.context();ctx['status']='invalid'
        with self.assertRaises(ValueError):incoming_material_profile(self.source(ctx),{},now=DUAL_NOW)
    def test_no_context_manual_monitor_profile_unchanged(self):
        raw={'materials':[{'name':'PP'}]}
        self.assertEqual(incoming_material_profile('plain source',raw),raw)
    def test_more_than_ten_not_silently_trimmed(self):
        ctx=self.context();ctx['materials']*=6
        with self.assertRaises(ValueError):incoming_material_profile(self.source(ctx),{},now=DUAL_NOW)
    def test_two_sap_cases_remain_separate(self):
        first=sample_case();second=copy.deepcopy(first);second['request']['id']='SECOND';second['offers']=[]
        component=SupplierQuoteComparisonComponent();component.set(case_data=MARKER+json.dumps({'cases':[first,second]}))
        output=component.build_report().text
        result=json.loads(output.split(RESULT_MARKER,1)[1]);self.assertEqual(len(result['cases']),2)
        self.assertFalse(result['sent'])
    def test_duplicate_case_id_rejected(self):
        case=sample_case();component=SupplierQuoteComparisonComponent();component.set(case_data=MARKER+json.dumps({'cases':[case,case]}))
        self.assertIn('ayrı talep kimliği',component.build_report().text)
    def test_manager_report_hides_machine_json(self):
        component=ProcurementManagerPackage();component.set(quote_report=Message(text='İnsan raporu\n\n'+RESULT_MARKER+'{"sent": false}'),research_report=Message(text='Kaynaklı araştırma'))
        output=component.build_report().text
        self.assertIn('İnsan raporu',output);self.assertIn('Kaynaklı araştırma',output);self.assertNotIn(RESULT_MARKER,output)
