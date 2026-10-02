"""Synthetic fixtures only; no accounts, personal contacts, Gloo or delivery."""
import io
import json
import zipfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.admin_setup.imports import parse_file, preview, normalize_phone, MAX_BYTES
from app.admin_setup.models import Workspace, StagedContact, ImportBatch
from app.config import Settings
from app.db import models as m
from app.main import create_app
from app.web.texty import admin

OWNER_A = '11111111-1111-4111-8111-111111111111'
OWNER_B = '22222222-2222-4222-8222-222222222222'
DETAILS = {'church_name':'Example Community Church', 'affiliation':'Independent', 'address':'100 Example Way',
           'city':'Example City','region':'CO','postal_code':'80000','country':'US','timezone':'America/Denver',
           'coordinator_name':'Alex Sample','coordinator_role':'Volunteer coordinator'}
ROWS = [['Full name','Mobile','Email','Team'],['Alex Sample','(202) 555-0111','alex@example.test','Welcome'],
        ['Casey Example','+12025550112','casey@example.test','Production'],['Duplicate Sample','2025550111','',''],
        ['Fix Sample','abc','','']]
IMPORT = {'rows':ROWS,'mapping':{'name':0,'phone':1,'email':2,'ministry':3},'country':'US','source':'Synthetic fixture'}


@pytest.fixture
def setup_client():
    app = create_app(Settings(database_url='sqlite://', sms_provider='mock', demo_mode=True,
                              automation_enabled=False, competition_confirmation_required=True))
    user = {'id':OWNER_A,'email':'admin@example.test','email_confirmed_at':'2026-10-02'}
    app.dependency_overrides[admin] = lambda: user
    with TestClient(app) as client:
        yield client, app, user


def save(client, details=None, complete=False, revision=0):
    return client.post('/api/setup',json={'details':details or DETAILS,'revision':revision,'complete':complete})


def stage(client, data=None):
    data = data or IMPORT
    preview_response=client.post('/api/setup/preview',json=data)
    assert preview_response.status_code == 200, preview_response.text
    request={**data,'preview_hash':preview_response.json()['preview_hash'],'submission_id':str(uuid4())}
    result=client.post('/api/setup/import',json=request)
    return result, request


def test_draft_resume_complete_and_preferences_do_not_change_live_rules(setup_client):
    client, app, _ = setup_client
    assert client.get('/api/setup').json()['revision'] == 0
    assert save(client, {'church_name':'Example Church'}).status_code == 200
    resumed=client.get('/api/setup').json()
    assert resumed['details']['church_name']=='Example Church' and not resumed['completed']
    bad=save(client, {'church_name':'Example Church'}, complete=True, revision=1)
    assert bad.status_code == 422
    assert save(client, {**DETAILS,'monthly_ask_limit':2,'quiet_start':'20:00'}, True,1).json()['completed']
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Policy)) is None
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(m.Volunteer)) is None


def test_stale_setup_and_untrusted_owner_fields_rejected(setup_client):
    client, _, _ = setup_client
    assert save(client).status_code == 200
    assert save(client,revision=0).status_code == 409
    body={'details':DETAILS,'revision':1,'owner_id':OWNER_B}
    assert client.post('/api/setup',json=body).status_code == 422
    assert save(client,{**DETAILS,'owner_id':OWNER_B},revision=1).status_code == 422


@pytest.mark.parametrize('field,value', [('timezone','invalid/timezone'),('monthly_ask_limit',0),('monthly_ask_limit',True),
                                         ('quiet_start','25:00'),('quiet_end','21:00'),('country','XX'),
                                         ('website','javascript:alert(1)'),('church_name','x'*161)])
def test_profile_validation(setup_client,field,value):
    assert save(setup_client[0],{**DETAILS,field:value}).status_code == 422


def test_import_requires_workspace_and_exact_current_preview(setup_client):
    client, _, _ = setup_client
    assert client.post('/api/setup/preview',json=IMPORT).status_code == 409
    save(client)
    p=client.post('/api/setup/preview',json=IMPORT).json()
    assert p['counts']=={'ready':2,'duplicate':1,'invalid':1}
    altered={**IMPORT,'rows':[ROWS[0],['Altered Sample','2025550119','','']],
             'submission_id':str(uuid4()),'preview_hash':p['preview_hash']}
    assert client.post('/api/setup/import',json=altered).status_code == 409
    assert client.get('/api/setup/contacts').json()['contacts']==[]


def test_save_import_idempotent_no_consent_no_outreach_and_no_overwrite(setup_client):
    client, app, _ = setup_client
    save(client)
    result, request=stage(client)
    assert result.status_code==200, result.text
    assert result.json()['imported']==2 and result.json()['texts_sent']==0
    assert client.post('/api/setup/import',json=request).json()==result.json()
    contacts=client.get('/api/setup/contacts').json()['contacts']
    assert all(not c['can_text'] and c['consent']=='not_recorded' for c in contacts)
    p=client.post('/api/setup/preview',json=IMPORT).json()
    assert p['counts']=={'ready':0,'duplicate':3,'invalid':1}
    with app.state.session_factory() as session:
        assert len(session.scalars(select(StagedContact)).all())==2
        assert len(session.scalars(select(ImportBatch)).all())==1
        for cls in (m.Volunteer,m.Assignment,m.Message,m.Outreach,m.Approval):
            assert session.scalar(select(cls)) is None


def test_two_owners_cannot_read_change_remove_or_dedup_each_others_contacts(setup_client):
    client, app, user=setup_client
    save(client)
    result,_=stage(client)
    a=client.get('/api/setup/contacts').json()['contacts'][0]
    user['id']=OWNER_B
    assert client.get('/api/setup').json()['revision']==0
    assert client.get('/api/setup/contacts').json()['contacts']==[]
    assert client.delete('/api/setup/contacts/'+a['id']).status_code==404
    save(client,{**DETAILS,'church_name':'Another Example Church'})
    assert client.post('/api/setup/preview',json=IMPORT).json()['counts']['ready']==2
    stage(client)
    user['id']=OWNER_A
    assert client.get('/api/setup').json()['details']['church_name']==DETAILS['church_name']
    assert len(client.get('/api/setup/contacts').json()['contacts'])==2
    assert client.delete('/api/setup/contacts/'+a['id']).status_code==200
    assert len(client.get('/api/setup/contacts').json()['contacts'])==1


def test_import_cannot_claim_consent_or_cross_tenant(setup_client):
    client,_,_=setup_client; save(client)
    for field in ('owner_id','workspace_id','sms_opt_in','consent'):
        assert client.post('/api/setup/preview',json={**IMPORT,field:True}).status_code==422
    p=client.post('/api/setup/preview',json={**IMPORT,'rows':[['Name','Phone','Consent'],['Example Sample','2025550111','YES']],
                                          'mapping':{'name':0,'phone':1}})
    assert p.json()['rows'][0]['consent']=='not_recorded'


def test_setup_keeps_existing_confirmed_allowlist_auth():
    app=create_app(Settings(database_url='sqlite://',supabase_url='https://example.test',
                            supabase_publishable_key='synthetic',admin_email_allowlist='admin@example.test'))
    with TestClient(app) as client:
        for method,path in [('GET','/api/setup'),('GET','/api/setup/contacts'),('POST','/api/setup/preview')]:
            response=client.request(method,path,json={})
            assert response.status_code==401
        assert client.post('/api/setup/parse',files={'file':('test.csv',b'Name,Phone\nSample,2025550111')}).status_code==401


def workbook(sheet, shared=None):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Volunteers" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+sheet+'</sheetData></worksheet>')
        if shared: z.writestr('xl/sharedStrings.xml',shared)
    return output.getvalue()


def test_csv_quotes_unicode_empty_rows_and_malformed():
    data='\ufeffName,Phone\r\n"Alex, Sample",2025550111\r\n\r\n"Casey\nExample",+442079460123\r\n'.encode()
    rows=parse_file('list.csv',data)[0]['rows']
    assert rows[1][0]=='Alex, Sample' and rows[2][0]=='Casey\nExample'
    with pytest.raises(ValueError): parse_file('list.csv',b'Name,Phone\n"broken,2025550111')
    with pytest.raises(ValueError): parse_file('list.csv',b'x'*(MAX_BYTES+1))
    with pytest.raises(ValueError): parse_file('list.xls',b'not xlsx')


def test_xlsx_inline_shared_numeric_phone_and_formula_rejected(setup_client):
    sheet='<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Phone</t></is></c></row><row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2"><v>2025550111</v></c></row><row r="3"><c r="A3"><f>CMD()</f><v>0</v></c><c r="B3"><v>2025550112</v></c></row>'
    data=workbook(sheet,'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Alex Sample</t></si></sst>')
    client,_,_=setup_client; save(client)
    response=client.post('/api/setup/parse',files={'file':('synthetic.xlsx',data)})
    assert response.status_code==200, response.text
    parsed=response.json()['sheets'][0]
    assert parsed['name']=='Volunteers' and parsed['rows'][1]==['Alex Sample','2025550111']
    report,_=preview(parsed['rows'],{'name':0,'phone':1},'US','Fixture')
    assert report['counts']=={'ready':1,'duplicate':0,'invalid':1}


def test_malformed_xlsx_and_xml_entities_fail_safely():
    with pytest.raises(ValueError): parse_file('fixture.xlsx',b'not a zip')
    with pytest.raises(ValueError): parse_file('fixture.xlsx',workbook('<!DOCTYPE x [<!ENTITY a "x">]>'))


def test_vcard_export_unfolding_multiple_phones_and_no_contact_access():
    data=b'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alex Sam\r\n ple\r\nTEL;TYPE=CELL:(202) 555-0111\r\nTEL;TYPE=WORK:+12025550112\r\nEMAIL:alex@example.test\r\nEND:VCARD\r\n'
    rows=parse_file('fixture.vcf',data)[0]['rows']
    assert len(rows)==3 and rows[1][0]=='Alex Sample'
    report,_=preview(rows,{'name':0,'phone':1,'email':2},'US','Synthetic export')
    assert report['counts']['ready']==2
    with pytest.raises(ValueError): parse_file('bad.vcf',b'BEGIN:VCARD\nFN:Unfinished')


@pytest.mark.parametrize('value,country,want', [('(202) 555-0111','US','+12025550111'),
                                               ('+44 20 7946 0123','international','+442079460123')])
def test_phone_normalization(value,country,want):
    assert normalize_phone(value,country)==want


@pytest.mark.parametrize('phone', ['2025550111 ext 2','02079460123','+0123456789','123','+1201555011'])
def test_phone_invalid_without_guessing_country(phone):
    with pytest.raises(ValueError): normalize_phone(phone,'international')


def test_mapping_limits_and_headers_rejected():
    for mapping in ({'name':0},{'name':0,'phone':0},{'name':0,'phone':50},{'name':0,'phone':'1'},{'name':0,'phone':True}):
        with pytest.raises(ValueError): preview(ROWS,mapping,'US','Fixture')
    with pytest.raises(ValueError): preview([ROWS[0]]+[['x','2025550111','','']]*2001,IMPORT['mapping'],'US','Fixture')


def test_xlsx_trailing_empty_cells_are_preserved():
    sheet='<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Phone</t></is></c><c r="C1" t="inlineStr"><is><t>Email</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Alex Sample</t></is></c><c r="B2"><v>2025550111.0</v></c></row>'
    rows=parse_file('synthetic.xlsx',workbook(sheet))[0]['rows']
    assert rows[1]==['Alex Sample','2025550111','']
    report,_=preview(rows,{'name':0,'phone':1,'email':2},'US','Fixture')
    assert report['counts']['ready']==1


def test_setup_metadata_never_enters_live_create_all():
    from app.db.models import Base
    from app.admin_setup.models import SetupBase
    assert not set(Base.metadata.tables) & set(SetupBase.metadata.tables)


def test_missing_setup_migration_returns_clear_503_without_breaking_existing_routes(setup_client):
    from app.admin_setup.models import SetupBase
    client,app,_=setup_client
    SetupBase.metadata.drop_all(app.state.engine)
    response=client.get('/api/setup')
    assert response.status_code==503 and 'reviewed migration' in response.json()['detail']
    assert client.get('/healthz').status_code==200
    assert client.get('/api/state').status_code==200


def test_large_empty_and_row_mismatch_imports_fail_safely(setup_client):
    client,_,_=setup_client; save(client)
    assert client.post('/api/setup/parse',files={'file':('empty.csv',b'')}).status_code==422
    for data in ({**IMPORT,'rows':[]},{**IMPORT,'mapping':None},{**IMPORT,'source':''},{**IMPORT,'country':'XX'}):
        assert client.post('/api/setup/preview',json=data).status_code==422
    data={**IMPORT,'rows':[ROWS[0],['Alex Sample','2025550111']]}
    assert client.post('/api/setup/preview',json=data).json()['counts']['invalid']==1
