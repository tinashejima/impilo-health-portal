import os
import sys
from datetime import date
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).with_name(".env"))

app = FastAPI(title="Attendance Reporting API")

@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response

# Sign-in check. Registered before CORS so CORS wraps it and "please sign in" replies reach the browser.
from auth import auth_middleware, router as auth_router  # noqa: E402
app.middleware('http')(auth_middleware)
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"]
    + [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # POST/PUT/DELETE for the AI assistant
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # so downloads keep their file names
)


def connect():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3307")),
        user=os.getenv("DB_USER", "attendance_api"),
        password=os.environ["DB_PASSWORD"],
        connection_timeout=10,
    )


@app.get("/api/health")
def health():
    connection = None
    try:
        connection = connect()
        connection.ping()
        return {"status": "ok", "database": "connected"}
    except mysql.connector.Error:
        raise HTTPException(503, "Database connection failed.")
    finally:
        if connection is not None:
            connection.close()


@app.get("/api/attendance")
def attendance(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100),facility: str | None=None,opd_only: bool=False):
    kind='opd-general' if opd_only else 'attendance'
    rows,site=corrected_data(kind,from_date,to_date,facility)
    offset=(page-1)*page_size;data=[{k:v for k,v in r.items() if k!='personId'} for r in rows[offset:offset+page_size]]
    return {'data':data,'pagination':{'page':page,'pageSize':page_size,'total':len(rows),'totalPages':(len(rows)+page_size-1)//page_size},'meta':{'definition':'OUTPATIENT, repeat recorded, age >=5' if opd_only else 'latest admission per person per date','facility':site}}

# Reporting extensions. This restored dataset belongs entirely to Avondale.
import csv
from io import BytesIO, StringIO
from fastapi.responses import Response
from typing import Literal


def report_filter(from_date,to_date,facility):
    clause,args=_dates('r.date',from_date,to_date)
    if facility not in (None,'','all'):_check_site(facility_metadata(),facility)
    return ('WHERE '+clause),args


@app.get('/api/attendance/summary')
def summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None,opd_only: bool=False):
    return corrected_summary('opd-general' if opd_only else 'attendance',from_date,to_date,facility)


EXPORT_FIELDS = [
 ('organisationUnit','Organisation unit'),('eventDate','Event date'),('sex','Sex'),('age','Age at visit'),
 ('medication','Dispensed medication'),('bloodPressure','Blood pressure'),('complaint','Complaint'),
 ('diagnosis','Diagnosis'),('height','Height'),('temperature','Temperature'),('weight','Weight'),('investigation','Investigation')]


def export_rows(from_date,to_date,opd_only=False,hts_only=False,malaria_only=False):
    kind='malaria' if malaria_only else 'hts' if hts_only else 'opd-general' if opd_only else 'attendance'
    rows,_=corrected_data(kind,from_date,to_date)
    yield from rows


def cell_value(value):
    if value is None: return ''
    if isinstance(value,date): return value.isoformat()
    return value


@app.get('/api/attendance/export')
def export_attendance(
    format: Literal['csv','xlsx','pdf'],
    from_date: date | None = Query(None, alias='from'),
    to_date: date | None = Query(None, alias='to'),
    facility: str | None = None,
    opd_only: bool = False,
    hts_only: bool = False,
    malaria_only: bool = False,
):
    report_filter(from_date,to_date,facility)
    report_name = 'Malaria Register' if malaria_only else 'HTS Register' if hts_only else 'OPD General Register' if opd_only else 'Attendance Register'
    export_fields = MALARIA_EXPORT_FIELDS if malaria_only else HTS_EXPORT_FIELDS if hts_only else OPD_EXPORT_FIELDS if opd_only else EXPORT_FIELDS
    prefix = 'malaria' if malaria_only else 'hts' if hts_only else 'opd-general' if opd_only else 'attendance'
    filename = f'{prefix}-{from_date or "all"}-{to_date or "all"}.{format}'
    headers = {'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'}
    if format == 'csv':
        output = StringIO(newline=''); writer = csv.writer(output)
        writer.writerow([label for _,label in export_fields])
        for row in export_rows(from_date,to_date,opd_only,hts_only,malaria_only):
            values=[]
            for key,_ in export_fields:
                value=cell_value((row.get('ageDisplay') if key=='age' and row.get('ageDisplay') else row.get(key)))
                # Prevent spreadsheet formula execution, including leading whitespace.
                if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')): value="'"+value
                values.append(value)
            writer.writerow(values)
        return Response(('\ufeff'+output.getvalue()).encode('utf-8'),media_type='text/csv',headers=headers)
    if format == 'xlsx':
        try:
            from openpyxl import Workbook
            from openpyxl.cell import WriteOnlyCell
            from openpyxl.styles import Font, PatternFill
        except ImportError: raise HTTPException(503,'Install openpyxl to enable Excel exports.')
        output=BytesIO(); workbook=Workbook(write_only=True); sheet=workbook.create_sheet(prefix[:31])
        sheet.freeze_panes='A2'
        for i,(key,label) in enumerate(export_fields,1):
            from openpyxl.utils import get_column_letter
            sheet.column_dimensions[get_column_letter(i)].width=32 if key in ('medication','complaint','diagnosis','investigation') else 20
        title=[]
        for _,label in export_fields:
            cell=WriteOnlyCell(sheet,value=label);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='067C61');title.append(cell)
        sheet.append(title)
        row_count=1
        for row in export_rows(from_date,to_date,opd_only,hts_only,malaria_only):
            row_count+=1
            cells=[]
            for key,_ in export_fields:
                value=cell_value((row.get('ageDisplay') if key=='age' and row.get('ageDisplay') else row.get(key)));cell=WriteOnlyCell(sheet,value=value)
                if isinstance(value,str): cell.data_type='s'
                cells.append(cell)
            sheet.append(cells)
        sheet.auto_filter.ref=f'A1:{get_column_letter(len(export_fields))}{row_count}'
        workbook.save(output)
        return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers=headers)
    if malaria_only:
        return hts_pdf(from_date,to_date,headers,malaria_only=True)
    if hts_only:
        return hts_pdf(from_date,to_date,headers)
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A3, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
        from xml.sax.saxutils import escape
    except ImportError: raise HTTPException(503,'Install reportlab to enable PDF exports.')
    output=BytesIO(); styles=getSampleStyleSheet()
    text_style=ParagraphStyle('Cell',fontName='Helvetica',fontSize=6.5,leading=8,wordWrap='CJK')
    document=SimpleDocTemplate(output,pagesize=landscape(A3),rightMargin=24,leftMargin=24,topMargin=32,bottomMargin=32)
    content=[Paragraph(f'{report_name} | {escape(facility_metadata()['name'])}',styles['Title']),
      Paragraph(f'Period: {from_date or "All dates"} to {to_date or "All dates"} | Full filtered report',styles['Normal']),Spacer(1,14)]
    table_data=[[Paragraph(escape(label),text_style) for _,label in export_fields]]
    for row in export_rows(from_date,to_date,opd_only,hts_only,malaria_only):
        table_data.append([Paragraph(escape(str(cell_value((row.get('ageDisplay') if key=='age' and row.get('ageDisplay') else row.get(key))))) or '-',text_style) for key,_ in export_fields])
    count=len(table_data)-1
    content.insert(2,Paragraph(f'{count:,} {'OPD' if opd_only else 'attendance'} records. Age is measured at attendance. Missing values are shown as a dash.',styles['Normal']))
    if count:
        widths=([100,65,45,35,110,65,120,120,40,110,70,40,70,40,55] if opd_only else [100,65,45,35,150,65,135,145,40,55,40,167])
        scale=(landscape(A3)[0]-48)/sum(widths)
        table=LongTable(table_data,colWidths=[w*scale for w in widths],repeatRows=1,splitByRow=1,splitInRow=1)
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e4f6ee')),('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.25,colors.HexColor('#d7e3e4')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f6f9f9')]),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]))
        content.append(table)
    else: content.append(Paragraph('No attendance records found for these filters.',styles['Normal']))
    def footer(canvas,doc):
        canvas.setFont('Helvetica',8);canvas.drawString(24,16,f'National Health Portal | {report_name}');canvas.drawRightString(landscape(A3)[0]-24,16,f'Page {doc.page}')
    document.build(content,onFirstPage=footer,onLaterPages=footer)
    return Response(output.getvalue(),media_type='application/pdf',headers=headers)


OPD_EXPORT_FIELDS = [
 ('organisationUnit','Organisation unit'),('eventDate','Event date'),('sex','Sex'),('age','Age at visit'),
 ('investigation','Investigations'),('bloodPressure','Blood pressure'),('complaint','Complaints'),
 ('diagnosis','Diagnosis'),('height','Height'),('management','Management'),('nextReviewDate','Next review date'),
 ('pulse','Pulse'),('tbScreening','TB screening'),('weight','Weight'),('temperature','Temperature')]

@app.get('/api/opd-general')
def opd_general(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100),facility: str | None=None):
    return attendance(from_date,to_date,page,page_size,facility,opd_only=True)

@app.get('/api/opd-general/summary')
def opd_summary(from_date: date | None = Query(None,alias='from'), to_date: date | None = Query(None,alias='to'), facility: str | None = None):
    return summary(from_date,to_date,facility,opd_only=True)

@app.get('/api/opd-general/export')
def opd_export(format: Literal['csv','xlsx','pdf'],from_date: date | None = Query(None,alias='from'),
               to_date: date | None = Query(None,alias='to'), facility: str | None = None):
    return export_attendance(format,from_date,to_date,facility,opd_only=True)

HTS_EXPORT_FIELDS = [
 ('organisationUnit','Organisation unit name'),('eventDate','Event date'),('age','Age at visit'),('sex','Sex'),
 ('selfIdentifiedGender','Self identified gender'),('birthdate','Birthdate'),('clientProfile','Client profile'),
 ('consentToIndexTesting','Consent to index testing'),('indexContact','Contact of index case'),('dateOfHivTest','Date of HIV test'),
 ('entryPoint','Entry point'),('firstTestEver','First test ever in life'),('firstTestForPregnancy','First test for this pregnancy'),
 ('htsSubModel','HTS sub model'),('htsType','HTS type'),('htsApproach','HTS approach'),('htsModel','HTS model'),
 ('htsNumber','HTS number'),('testResult','HTS test result'),('testA1','HTS test A1'),('testA2','HTS test A2'),('testA3','HTS test A3'),('testA1Repeat','Repeat A1 result'),
 ('indexCaseHtsNumber','Index case HTS number'),('mobileClient','Mobile client'),('optedOut','Opted out of HIV testing'),
 ('preTestInformationGiven','Pre test information given'),('rtriResult','RTRI result'),('rtriResultTests','RTRI result tests'),
 ('rtriTestDone','RTRI test done'),('reasonForHivTesting','Reason for HIV testing'),('reasonForNotIssuingResult','Reason for not issuing result'),
 ('reasonForNotPerformingRecencyTest','Reason for not performing recency test'),('receivedResults','Received HIV test results'),
 ('postTestCounselling','Received post test counselling'),('retestingBeforeArt','Retesting before ART initiation'),
 ('timingOfHivDiagnosis','Timing of HIV diagnosis'),('verificationTestDone','Verification test done')]
HTS_UNMAPPED = ['firstTestEver','mobileClient','rtriResultTests','retestingBeforeArt']


def hts_flags(value):
    return None if value is None else ('Yes' if int(value) else 'No')


def hts_data(from_date,to_date,facility,page=None,page_size=None):
    rows,_=corrected_data('hts',from_date,to_date,facility)
    return (rows if page is None else rows[(page-1)*page_size:page*page_size]),len(rows)


@app.get('/api/hts')
def hts_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),
                 facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,total=hts_data(from_date,to_date,facility,page,page_size)
    for row in rows:row.pop('personId',None)
    return {'data':rows,'pagination':{'page':page,'pageSize':page_size,'total':total,'totalPages':(total+page_size-1)//page_size},
      'meta':{'source':'report.hts_register','definition':'RAPID with linked investigation and demographics','unmappedFields':HTS_UNMAPPED,
       'conditionalFields':'Tests ordered by time; third test with first-test kit is Repeat A1. Recency uses latest result on/before visit; verification uses latest person-level ART retest as in source.'}}


@app.get('/api/hts/summary')
def hts_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    return corrected_summary('hts',from_date,to_date,facility)


@app.get('/api/hts/export')
def hts_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    return export_attendance(format,from_date,to_date,facility,hts_only=True)


def hts_pdf(from_date,to_date,headers,malaria_only=False):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak
    from xml.sax.saxutils import escape
    rows,total=(malaria_data(from_date,to_date,None) if malaria_only else hts_data(from_date,to_date,None))
    report_name='Malaria Register' if malaria_only else 'HTS Register'
    report_fields=MALARIA_EXPORT_FIELDS if malaria_only else HTS_EXPORT_FIELDS
    output=BytesIO();styles=getSampleStyleSheet()
    cell=ParagraphStyle('HTSCell',fontSize=8,leading=10,wordWrap='CJK')
    doc=SimpleDocTemplate(output,pagesize=landscape(A4),leftMargin=24,rightMargin=24,topMargin=26,bottomMargin=26)
    content=[]
    for index,row in enumerate(rows):
        if index:content.append(PageBreak())
        content.append(Paragraph(f'{report_name} | {escape(facility_metadata()['name'])}',styles['Heading2']))
        content.append(Paragraph(f'{from_date or "All dates"} to {to_date or "All dates"} | Record {index+1} of {total}',styles['Normal']))
        content.append(Spacer(1,10))
        pairs=[]
        for key,label in report_fields:
            value=cell_value((row.get('ageDisplay') if key=='age' and row.get('ageDisplay') else row.get(key)))
            pairs.append((Paragraph(escape(label),cell),Paragraph(escape(str(value)) if value!='' else '-',cell)))
        half=(len(pairs)+1)//2;data=[]
        for i in range(half):
            right=pairs[i+half] if i+half<len(pairs) else ('','')
            data.append([*pairs[i],*right])
        table=Table(data,colWidths=[145,252,145,252],hAlign='LEFT')
        table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#eaf5eb')),('BACKGROUND',(2,0),(2,-1),colors.HexColor('#eaf5eb')),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#dceadd')),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        content.append(table)
    if not rows:content=[Paragraph(f'{report_name} | No records for these filters',styles['Heading2'])]
    def footer(canvas,document):
        canvas.saveState();canvas.setFillColor(colors.white);canvas.rect(0,0,*landscape(A4),fill=1,stroke=0);canvas.restoreState()
        canvas.setFont('Helvetica',8);canvas.drawString(24,12,f'{report_name} source-unavailable fields shown as a dash.');canvas.drawRightString(818,12,f'Page {document.page}')
    doc.build(content,onFirstPage=footer,onLaterPages=footer)
    return Response(output.getvalue(),media_type='application/pdf',headers=headers)

MALARIA_EXPORT_FIELDS=[('organisationUnit','Organisation unit name'),('eventDate','Event date'),('age','Age'),('sex','Sex'),
 ('malariaInvestigation','Malaria investigation'),('investigationResult','Investigation result'),('complaint','Complaint'),
 ('dateOfOnset','Date of onset'),('medications','Malaria medications'),('administered','Administered'),('chwReferral','CHW referral'),
 ('clinicalClassification','Clinical classification'),('malariaOutcome','Malaria outcome'),('microscopyAdministered','Microscopy administered'),
 ('microscopyResult','Microscopy result'),('pregnancyStatus','Pregnancy status'),('rdtResult','RDT result'),('recentTravelToRiskArea','Recent travel to risk area')]


def malaria_data(from_date,to_date,facility,page=None,page_size=None):
    rows,_=corrected_data('malaria',from_date,to_date,facility)
    return (rows if page is None else rows[(page-1)*page_size:page*page_size]),len(rows)


@app.get('/api/malaria')
def malaria_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),
                      facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,total=malaria_data(from_date,to_date,facility,page,page_size)
    for row in rows:row.pop('personId',None)
    return {'data':rows,'pagination':{'page':page,'pageSize':page_size,'total':total,'totalPages':(total+page_size-1)//page_size},
      'meta':{'source':'report.investigation_register','unit':'one selected malaria investigation per person in the period, not necessarily confirmed malaria',
      'unmappedFields':['administered','microscopyAdministered'],
      'conditionalFields':'Latest Malaria record for the selected visit; travel is retained only with one visit-linked history record. Method-specific results are latest per person in the selected period, as in source.'}}


@app.get('/api/malaria/summary')
def malaria_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    return corrected_summary('malaria',from_date,to_date,facility)


@app.get('/api/malaria/export')
def malaria_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    return export_attendance(format,from_date,to_date,facility,malaria_only=True)


# Shared source-aligned reporting. All endpoints and exports use the same cohorts.
from collections import defaultdict, Counter
from datetime import datetime


def _query(c, sql, args=()):
    q=c.cursor(dictionary=True)
    try:q.execute(sql,tuple(args));return q.fetchall()
    finally:q.close()


# ---------------------------------------------------------------- Facility registry
# registry.* holds the national hierarchy: 10 provinces, their districts, and every facility
# (id, name, province, district, type, latitude, longitude). DATA_FACILITY_IDS lists the
# facilities whose records are loaded in this database - today only Avondale Clinic. In the
# multi-tenant database every row will carry facility_id; the register queries will then filter
# on the reporting facilities in scope instead of reading the whole (single-tenant) database.
TENANT_FACILITY_ID=os.getenv('TENANT_FACILITY_ID','ZW000A01')
DATA_FACILITY_IDS={x.strip() for x in os.getenv('DATA_FACILITY_IDS',TENANT_FACILITY_ID).split(',') if x.strip()}
REGISTRY_SQL="""SELECT r.facility_id,r.facility_name,r.latitude,r.longitude,r.province_id,p.province_name,
  r.district_id,d.district_name,r.facility_type,t.type_label,t.type_group,r.facility_type_source
  FROM registry.facility_registry r
  LEFT JOIN registry.province p ON p.province_id=r.province_id
  LEFT JOIN registry.district d ON d.district_id=r.district_id
  LEFT JOIN registry.facility_type t ON t.type_code=r.facility_type"""
TYPE_GROUPS={'HOSPITAL':('RURAL_HOSPITAL','DISTRICT_HOSPITAL','PROVINCIAL_HOSPITAL','CENTRAL_HOSPITAL','OTHER_HOSPITAL')}


def _registry_row(r):
    return {'id':r['facility_id'],'name':r['facility_name'],
            'latitude':float(r['latitude']) if r['latitude'] is not None else None,
            'longitude':float(r['longitude']) if r['longitude'] is not None else None,
            'provinceId':r['province_id'],'province':r['province_name'],
            'districtId':r['district_id'],'district':r['district_name'] or 'District not recorded',
            'type':r['facility_type'],'typeLabel':r['type_label'],'typeGroup':r['type_group'],'typeSource':r['facility_type_source'],
            'hasData':r['facility_id'] in DATA_FACILITY_IDS}


def _registry(c,facility_id=None,province=None,district=None,ftype=None,q=None):
    where=[];args=[]
    if facility_id:where.append('r.facility_id=%s');args.append(facility_id)
    if province:where.append('r.province_id=%s');args.append(province)
    if district:where.append('r.district_id=%s');args.append(district)
    if ftype:
        codes=TYPE_GROUPS.get(ftype.upper(),(ftype.upper(),))
        where.append('r.facility_type IN ('+','.join(['%s']*len(codes))+')');args+=list(codes)
    if q:where.append('r.facility_name LIKE %s');args.append('%'+q.strip()+'%')
    sql=REGISTRY_SQL+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY p.province_name,d.district_name,r.facility_name'
    return [_registry_row(r) for r in _query(c,sql,args)]


def _facility(c):
    found=_registry(c,TENANT_FACILITY_ID)
    if not found:raise HTTPException(503,f'Facility {TENANT_FACILITY_ID} is not in the facility registry.')
    latest=_query(c,'SELECT MAX(date) AS latestDate,MAX(admission_time) AS lastUpdated FROM report.patient_register')
    return {**found[0],'latestDate':latest[0]['latestDate'] if latest else None,
            'lastUpdated':latest[0]['lastUpdated'] if latest else None,  # newest record in the warehouse
            'source':'registry.facility_registry'}


def _check_site(site,requested):
    # Accepts the tenant's ID; 'avondale' is kept for existing bookmarks.
    if requested in (None,'','all',site['id']) or (requested=='avondale' and site['id']=='ZW000A01'):return
    raise HTTPException(404 if requested else 400,f'No data is loaded for facility {requested!r}. This database holds {site["name"]} ({site["id"]}) only.')


def _with_registry(fn):
    c=None
    try:c=connect();return fn(c)
    except mysql.connector.Error:raise HTTPException(503,'Facility registry unavailable. Check read access to the registry tables (run update.cmd).')
    finally:
        if c is not None:c.close()


@app.get('/api/geography')
def geography():
    def build(c):
        provinces=_query(c,'SELECT province_id,province_name FROM registry.province ORDER BY province_name')
        districts=_query(c,'SELECT district_id,district_name,province_id FROM registry.district ORDER BY district_name')
        types=_query(c,'SELECT type_code,type_label,type_group FROM registry.facility_type ORDER BY sort_order')
        return {'country':'Zimbabwe',
                'provinces':[{'id':p['province_id'],'name':p['province_name'],
                              'districts':[{'id':d['district_id'],'name':d['district_name']} for d in districts if d['province_id']==p['province_id']]} for p in provinces],
                'facilityTypes':[{'code':t['type_code'],'label':t['type_label'],'group':t['type_group']} for t in types],
                'dataFacilityIds':sorted(DATA_FACILITY_IDS)}
    return _with_registry(build)


@app.get('/api/facilities')
def facilities_list(province: str | None=None,district: str | None=None,type: str | None=None,q: str | None=None):
    rows=_with_registry(lambda c:_registry(c,None,province,district,type,q))
    return {'data':rows,'total':len(rows),'reporting':sum(r['hasData'] for r in rows),'tenantFacilityId':TENANT_FACILITY_ID}


@app.get('/api/facilities/{facility_id}')
def facility_detail(facility_id:str):
    rows=_with_registry(lambda c:_registry(c,facility_id))
    if not rows:raise HTTPException(404,f'Facility {facility_id!r} is not in the facility registry.')
    return rows[0]


@app.get('/api/facility')
def facility_metadata():
    c=None
    try:c=connect();return _facility(c)
    except mysql.connector.Error:raise HTTPException(503,'Facility lookup failed. Check read access to registry.facility_registry.')
    finally:
        if c is not None:c.close()


def _dates(column,from_date,to_date):
    if from_date and to_date and from_date>to_date:raise HTTPException(400,"'from' must be on or before 'to'.")
    conditions=[];args=[]
    if from_date:conditions.append(column+' >= %s');args.append(from_date)
    if to_date:conditions.append(column+' <= %s');args.append(to_date)
    return (' AND '.join(conditions) or '1=1'),args


def _day(value):
    return value.date() if isinstance(value,datetime) else value


def _age_fields(born,event):
    if not isinstance(born,date) or not isinstance(event,date) or born>event:return None,None
    years=event.year-born.year-((event.month,event.day)<(born.month,born.day))
    if years:return years,f'{years} years'
    months=(event.year-born.year)*12+event.month-born.month-(event.day<born.day)
    return 0,(f'{months} months' if months else f'{(event-born).days} days')


def _base_row(source,site):
    age,display=_age_fields(source.get('birthdate'),source.get('eventDate'))
    return {'recordId':source['recordId'],'personId':source.get('personId'),
            'organisationUnit':site['name'],'eventDate':source.get('eventDate'),
            'sex':source.get('sex'),'age':age,'ageDisplay':display}


def _text(value):
    return value.replace('#',', ') if isinstance(value,str) else value


def _source_rows(c,table,column,from_date,to_date,extra=''):
    clause,args=_dates(column,from_date,to_date)
    return _query(c,f'SELECT * FROM {table} WHERE {clause} '+extra,args)


def _dispenses(c,from_date,to_date):
    result=defaultdict(list)
    for r in _source_rows(c,'report.dispense_register','date',from_date,to_date,'ORDER BY date,dispense_id'):
        if r['medicine']:result[(r['person_id'],r['date'])].append(r['medicine'])
    return {key:', '.join(v) for key,v in result.items()}


def _complaints(c,from_date,to_date):
    result=defaultdict(list)
    for r in _source_rows(c,'report.patient_complaint_report','date',from_date,to_date,'ORDER BY date,person_complaint_id'):
        result[(r['person_id'],r['date'])].append(r)
    return result


def _patient_rows(c,kind,from_date,to_date,site):
    clause,args=_dates('r.date',from_date,to_date)
    if kind=='attendance':
        sql=f'''WITH ranked AS (SELECT r.*,ROW_NUMBER() OVER(PARTITION BY r.person_id,r.date
          ORDER BY r.admission_time DESC,r.patient_id DESC) AS cohort_rank
          FROM report.patient_register r WHERE {clause})
          SELECT r.*,r.patient_id AS recordId,r.person_id AS personId,r.date AS eventDate,d.birthdate,d.sex
          FROM ranked r JOIN report.person_demographic d ON d.person_id=r.person_id
          WHERE r.cohort_rank=1 ORDER BY r.date ASC,r.patient_id ASC'''
    else:
        sql=f'''SELECT r.*,r.patient_id AS recordId,r.person_id AS personId,r.date AS eventDate,d.birthdate,d.sex
          FROM report.patient_register r JOIN report.person_demographic d ON d.person_id=r.person_id
          WHERE {clause} AND r.patient_type='OUTPATIENT' AND r.is_repeat IS NOT NULL
          AND YEAR(d.birthdate)>0 AND d.birthdate<=r.date AND TIMESTAMPDIFF(YEAR,d.birthdate,r.date)>=5
          ORDER BY r.date DESC,r.patient_id DESC'''
    raw=_query(c,sql,args)
    meds=_dispenses(c,from_date,to_date);complaints=_complaints(c,from_date,to_date)
    screens={}
    for r in _source_rows(c,'report.tb_screening_register','date',from_date,to_date,'ORDER BY date,patient_tb_screening_id'):
        screens[r['patient_id']]='Presumptive' if _flag(r['presumptive'])=='Yes' else 'Not Presumptive'
    appointments=defaultdict(list)
    for a in _source_rows(c,'report.appointment_register','DATE(visit_date)',from_date,to_date,'ORDER BY visit_date,appointment_id'):
        given=_day(a['visit_date']);future=_day(a['time'])
        if future and given and future>=given:appointments[(a['person_id'],given)].append(future.isoformat())
    rows=[]
    for r in raw:
        row=_base_row(r,site);key=(r['personId'],r['eventDate']);medicine=meds.get(key)
        cs=complaints.get(key,[])
        row.update(attendanceId=r['recordId'],medication=medicine,management=medicine,
          nextReviewDate=', '.join(appointments.get(key,[])) or None,
          bloodPressure=r['blood_pressure'],complaint=', '.join(f"{x['complaint']} for {x['duration']} day(s)" for x in cs if x['complaint']) or None,
          diagnosis=_text(r['diagnoses']),height=r['height'],temperature=r['temperature'],weight=r['weight'],
          investigation=_text(r['investigations']),pulse=r['pulse'],tbScreening=screens.get(r['recordId'],'ND'))
        rows.append(row)
    return rows


def _flag(value):
    if value is None:return None
    return 'Yes' if (int.from_bytes(value,'big') if isinstance(value,bytes) else int(value)) else 'No'


def _hts_rows(c,from_date,to_date,site):
    clause,args=_dates('r.date',from_date,to_date)
    raw=_query(c,f'''SELECT r.*,r.hts_id AS recordId,ir.person_id AS personId,r.date AS eventDate,
       d.birthdate,d.sex,d.self_identified_gender,h.date_of_hiv_test,h.entry_point,h.pre_test_information_given
       FROM report.hts_register r JOIN report.investigation_register ir ON ir.laboratory_investigation_id=r.laboratory_investigation_id
       JOIN report.person_demographic d ON d.person_id=ir.person_id LEFT JOIN consultation.hts h ON h.hts_id=r.hts_id
       WHERE {clause} AND r.test_type='RAPID' ORDER BY r.date DESC,r.hts_id DESC''',args)
    tests=defaultdict(list)
    for t in _query(c,f'''SELECT t.*,b.test_kit AS batch_kit,b.batch_number,b.expiry_date
        FROM report.hts_register_test t JOIN report.hts_register r ON r.hts_id=t.hts_id
        LEFT JOIN report.test_kit_batch_issue_register issue ON issue.batch_issue_id=t.batch_issue_id
        LEFT JOIN report.test_kit_batch_register b ON b.batch_id=issue.batch_id
        WHERE {clause} AND r.test_type='RAPID' ORDER BY t.time ASC,t.order_test_id ASC''',args):tests[t['hts_id']].append(t)
    results={}
    for r in _query(c,'SELECT laboratory_result_id,code,name FROM terminology.laboratory_result'):
        for key in [r['laboratory_result_id'],r['code']]:
            if key:results[key]=r['name']
    # Original verification uses latest ART retest per person; result existence is checked separately.
    verification={}
    for r in _query(c,'''SELECT a.person_id,a.date,a.art_re_test_id,ir.result FROM consultation.art_re_test a
        LEFT JOIN report.investigation_register ir ON ir.laboratory_investigation_id=a.investigation_order_id
        ORDER BY a.date ASC,a.art_re_test_id ASC'''):verification[r['person_id']]=r['result'] is not None
    recency=defaultdict(list)
    for r in _query(c,"SELECT person_id,date,result FROM report.investigation_register WHERE investigation_type='RECENCY_TEST' ORDER BY date DESC,laboratory_investigation_id DESC"):
        recency[r['person_id']].append(r)
    cbs={r['person_id']:r for r in _query(c,'SELECT person_id,hts_number,date_of_diagnosis,index_case_hts_number,timing_of_hiv_diagnosis FROM report.cbs_register')}
    rows=[]
    for r in raw:
        row=_base_row(r,site)
        row.update(selfIdentifiedGender=r['self_identified_gender'],birthdate=r['birthdate'],clientProfile=r['client_profile'],
          consentToIndexTesting=_flag(r['consent_to_index_testing']),indexContact=_flag(r['is_index_contact']),
          dateOfHivTest=r['date_of_hiv_test'],entryPoint=r['refered_service'],
          firstTestEver=None,firstTestForPregnancy=({'FIRST_TEST':'First Test','RETEST':'Retest','NONE':'None'}.get((r['pregnancytest'] or '').upper()) if r['sex']!='MALE' else 'N/A'),
          htsSubModel=r['hts_model_sub_type'],htsType=r['test_type'],htsApproach=r['approach'],htsModel=r['htc_model'],htsNumber=r['hts_number'],
          testResult=r['result'],testA1=None,testA2=None,testA3=None,testA1Repeat=None,indexCaseHtsNumber=None,mobileClient=None,
          optedOut=_flag(r['opt']),preTestInformationGiven=_flag(r['pre_test_information_given']),rtriResult=None,rtriResultTests=None,rtriTestDone=None,
          reasonForHivTesting=r['test_purpose'],reasonForNotIssuingResult=r['reason_for_not_issuing_result'],reasonForNotPerformingRecencyTest=r['reason_for_not_performing_test'],
          receivedResults=_flag(r['results_issued']),postTestCounselling=_flag(r['post_test_counselling']),retestingBeforeArt=None,
          timingOfHivDiagnosis=None,verificationTestDone='Yes' if verification.get(r['personId'],False) else 'No')
        context=cbs.get(r['personId'])
        if context and context['hts_number'] and context['hts_number']==r['hts_number'] and context['date_of_diagnosis']==(r['date_of_hiv_test'] or r['eventDate']):
            row['indexCaseHtsNumber']=context['index_case_hts_number'];row['timingOfHivDiagnosis']=context['timing_of_hiv_diagnosis']
        items=tests.get(r['recordId'],[])
        for i,t in enumerate(items[:3]):
            kit=t['batch_kit'] or t['test_kit'];key='testA'+str(i+1)
            if i==2 and kit and items[0] and str(kit).strip().casefold()==str(items[0]['batch_kit'] or items[0]['test_kit'] or '').strip().casefold():key='testA1Repeat'
            row[key]=results.get(t['result'],t['result']);row[key+'Kit']=kit
            row[key+'Batch']=t['batch_number'];row[key+'Expiry']=t['expiry_date']
        recent=next((v for v in recency.get(r['personId'],[]) if v['date'] and r['eventDate'] and v['date']<=r['eventDate']),None)
        if recent:row.update(rtriResult=recent['result'],rtriTestDone='Yes',reasonForNotPerformingRecencyTest=None)
        else:row['rtriTestDone']='N/A' if (r['result'] or '').upper() in ('NEGATIVE','INCONCLUSIVE') else 'No'
        rows.append(row)
    return rows


def _malaria_rows(c,from_date,to_date,site):
    clause,args=_dates('ir.date',from_date,to_date)
    raw=_query(c,f'''SELECT ir.*,ir.laboratory_investigation_id AS recordId,ir.person_id AS personId,ir.date AS eventDate,d.birthdate,d.sex
      FROM report.investigation_register ir LEFT JOIN report.person_demographic d ON d.person_id=ir.person_id
      WHERE ir.laboratory_investigation_id IN (SELECT MIN(ir.laboratory_investigation_id)
      FROM report.investigation_register ir WHERE {clause}
      AND ir.investigation_type IN ('MALARIA_RDT','MALARIA_BLOODSLIDE','MALARIA_MICROSCOPY') GROUP BY ir.person_id)
      ORDER BY ir.date ASC,ir.laboratory_investigation_id ASC''',args)
    meds=_dispenses(c,from_date,to_date);complaints=_complaints(c,from_date,to_date)
    visits={}
    for z in _query(c,'SELECT * FROM consultation.malaria ORDER BY date,malaria_id'):visits[z['patient_id']]=z
    travel=defaultdict(list)
    for z in _query(c,'SELECT patient_id,is_high_risk FROM consultation.malaria_travel_history'):travel[z['patient_id']].append(z)
    other=defaultdict(dict)
    for r in _query(c,f'''SELECT ir.person_id,ir.investigation_type,ir.result,ir.date
       FROM report.investigation_register ir WHERE {clause}
       AND ir.investigation_type IN ('MALARIA_RDT','MALARIA_MICROSCOPY') ORDER BY ir.date DESC,ir.laboratory_investigation_id DESC''',args):
        other[r['person_id']].setdefault(r['investigation_type'],r['result'])
    qualifying={'Fever','Headache','Nausea','Muscle Pain','Loss of appetite','Abdominal pain localized other','Weakness/tiredness general'}
    rows=[]
    from datetime import timedelta
    for r in raw:
        row=_base_row(r,site);key=(r['personId'],r['eventDate']);cs=complaints.get(key,[])
        onset=next((x for x in cs if x['complaint'] in qualifying and x['date'] and x['duration'] is not None),None)
        visit=visits.get(r['patient_id'],{})
        row.update(malariaInvestigation=r['test'] or r['investigation_type'],investigationResult=r['result'],
          complaint=', '.join(x['complaint'] for x in cs if x['complaint']) or None,
          dateOfOnset=onset['date']-timedelta(days=onset['duration']) if onset else None,
          medications=meds.get(key),administered=None,chwReferral=visit.get('refer_from_community_health_worker'),
          clinicalClassification=visit.get('malaria_classification'),malariaOutcome=visit.get('outcome'),
          microscopyAdministered=None,microscopyResult=other[r['personId']].get('MALARIA_MICROSCOPY'),
          pregnancyStatus=_flag(visit.get('is_pregnant')),rdtResult=other[r['personId']].get('MALARIA_RDT'),
          recentTravelToRiskArea=_flag(travel[r['patient_id']][0]['is_high_risk']) if len(travel[r['patient_id']])==1 else None,testingPlace=('COMMUNITY' if _flag(visit.get('tested_by_community_health_worker'))=='Yes' else 'FACILITY') if visit else None)
        rows.append(row)
    return rows


def corrected_data(kind,from_date,to_date,facility=None):
    _dates('date',from_date,to_date);c=None
    try:
        c=connect();site=_facility(c);_check_site(site,facility)
        if kind in ('attendance','opd-general'):rows=_patient_rows(c,kind,from_date,to_date,site)
        elif kind=='hts':rows=_hts_rows(c,from_date,to_date,site)
        else:rows=_malaria_rows(c,from_date,to_date,site)
        return rows,site
    except mysql.connector.Error as error:
        raise HTTPException(503,f'Report source unavailable (database error {error.errno}). Check read permissions.')
    finally:
        if c is not None:c.close()


def _monthly_sex(rows):
    """Records per month, split by sex, for the register charts."""
    out={}
    for r in rows:
        d=r.get('eventDate')
        if not d:continue
        m=out.setdefault(str(d)[:7],{'month':str(d)[:7],'total':0,'female':0,'male':0,'other':0})
        sx=(r.get('sex') or r.get('maternalSex') or '').upper()
        m['total']+=1;m['female' if sx=='FEMALE' else 'male' if sx=='MALE' else 'other']+=1
    return [out[k] for k in sorted(out)]


def corrected_summary(kind,from_date,to_date,facility):
    rows,site=corrected_data(kind,from_date,to_date,facility)
    daily=Counter(str(r['eventDate']) for r in rows if r['eventDate']);sex=Counter((r['sex'] or 'Unknown').upper() for r in rows)
    bands=Counter();result_counts=Counter()
    for r in rows:
        age=r['age'];bands['Unknown' if age is None else '0-4' if age<5 else '5-14' if age<15 else '15-24' if age<25 else '25-44' if age<45 else '45-64' if age<65 else '65+']+=1
        if kind in ('hts','malaria'):result_counts[r.get('testResult' if kind=='hts' else 'investigationResult') or 'Not recorded']+=1
    recorded={'diagnosis':sum(bool(r.get('diagnosis')) for r in rows),'bloodPressure':sum(bool(r.get('bloodPressure')) for r in rows),
      'medication':sum(bool(r.get('medication')) for r in rows),'tbScreening':sum(r.get('tbScreening') not in (None,'','ND') for r in rows),
      'resultsIssued':sum(r.get('receivedResults')=='Yes' for r in rows),'postTestCounselling':sum(r.get('postTestCounselling')=='Yes' for r in rows),
      'resultRecorded':sum((r.get('investigationResult') or '').upper() not in ('','MISSING','UNKNOWN','NOT DONE','NOT RECORDED') for r in rows),
      'medicationsRecorded':sum(bool(r.get('medications')) for r in rows)}
    return {'total':len(rows),'uniquePeople':len({r['personId'] for r in rows if r['personId']}),
      'monthlyBySex':_monthly_sex(rows),'daily':[{'date':k,'count':daily[k]} for k in sorted(daily)],'sex':[{'label':k,'count':v} for k,v in sex.most_common()],
      'ageBands':[{'label':k,'count':bands[k]} for k in ['0-4','5-14','15-24','25-44','45-64','65+','Unknown']],
      'results':[{'label':k,'count':v} for k,v in result_counts.most_common()], 'recorded':recorded,'facilities':[site]}


ANC_FIELDS=[('organisationUnit','Organisation unit'),('eventDate','Registration date'),('ancNumber','ANC number'),('fullName','Name'),('age','Age at registration'),('sex','Sex'),('firstBooking','First booking'),('gestationalAge','Gestational age at booking'),('gravida','Gravida'),('parity','Parity'),('lnmp','LNMP'),('edd','Recorded EDD'),('hivStatusPriorAnc','HIV status prior ANC'),('artInitiated','Already on ART at booking'),('artNumber','ART number'),('bloodGroup','Blood group'),('pregnancyTest','Pregnancy test'),('height','Height'),('pulse','Pulse'),('rbfNumber','RBF number'),('contactNumber','Contact number'),('address','Address'),('nextOfKin','Next of kin'),('nextOfKinPhone','Next of kin phone'),('nextOfKinAddress','Next of kin address')]
ANC_VISIT_FIELDS=[('visitDate','Visit date'),('visitNumber','Visit number'),('gestationalAge','Gestational age'),('weight','Weight'),('height','Height'),('pulse','Pulse'),('bloodPressure','Blood pressure'),('muac','MUAC'),('fetalHeartRate','Fetal heart rate'),('heightOfFundus','Height of fundus'),('presentation','Presentation'),('position','Position'),('pallor','Pallor'),('deliveryPlan','Delivery plan'),('hivResult','HIV result'),('partnerHivResult','Partner HIV result'),('partnerHivStatus','Partner HIV status'),('partnerAlreadyPositive','Partner already positive'),('syphilisResult','Syphilis result'),('syphilisDose','Syphilis treatment recorded'),('syphilisPartnerResult','Partner syphilis result'),('syphilisPartnerDose','Partner syphilis treatment recorded'),('artInitiated','ART initiated at visit'),('artNumber','ART number'),('artFollowupStatus','ART follow-up status'),('tbScreening','TB screening'),('cd4Result','CD4 result'),('viralLoadResult','Viral load result'),('hbResult','Haemoglobin'),('rhResult','Rhesus'),('glucoseResult','Glucose'),('hepatitisBResult','Hepatitis B result'),('bloodGroup','Blood group'),('urinalysisResult','Urinalysis'),('tetanusToxoid','Tetanus toxoid'),('malariaProphylaxis','Malaria prophylaxis'),('ironSupplements','Iron supplements'),('calciumSupplements','Calcium supplements'),('proteinSupplements','Protein supplements'),('nutritionCounselling','Nutrition counselling'),('pregnancyComplications','Pregnancy complications'),('postPartumCounselling','Postpartum counselling'),('prepDrugs','PrEP offered in registration filter period'),('ussDone','Ultrasound done'),('scanResult','Scan result'),('scanEdd','Scan EDD'),('noOfBabies','Number of babies'),('comments','Comments')]


def _anc_fetch(c,table,key,ids):
    ids=sorted(v for v in ids if v)
    return _query(c,f'SELECT * FROM {table} WHERE {key} IN ({",".join(["%s"]*len(ids))})',ids) if ids else []


def _anc_gestation(lnmp,when):
    if not isinstance(lnmp,date) or not isinstance(when,date) or lnmp>when:return None
    days=(when-lnmp).days;weeks,days=divmod(days,7)
    return f'{weeks} Weeks'+(f' and {days} Days' if days else '')


def anc_data(from_date,to_date,facility=None):
    clause,args=_dates('a.date',from_date,to_date);c=None
    try:
        c=connect();site=_facility(c);_check_site(site,facility)
        registrations=_query(c,f'''SELECT a.*,a.anc_id AS recordId,a.person_id AS personId,a.date AS eventDate,
          d.birthdate,d.sex,d.firstname,d.lastname,d.address,d.phone_number,
          kd.firstname AS kin_firstname,kd.lastname AS kin_lastname,kd.phone_number AS kin_phone,kd.address AS kin_address
          FROM report.anc_register a LEFT JOIN report.person_demographic d ON d.person_id=a.person_id
          LEFT JOIN report.next_of_kin_report k ON k.person_id=a.person_id
          LEFT JOIN report.person_demographic kd ON kd.person_id=k.next_of_kin_person_id
          WHERE {clause} ORDER BY a.date DESC,a.anc_id DESC''',args)
        if not registrations:return [],site
        anc_ids={a['anc_id'] for a in registrations};people={a['person_id'] for a in registrations};all_people=people|{a['partner_id'] for a in registrations if a['partner_id']}
        visits=_anc_fetch(c,'report.anc_visit_register','anc_id',anc_ids)
        visit_map=defaultdict(list)
        for v in sorted(visits,key=lambda v:(v['visit_number'],v['anc_visit_id'])):visit_map[v['anc_id']].append(v)
        patients=_anc_fetch(c,'report.patient_register','person_id',people)
        patient_ids={v['patient_id'] for v in visits if v['patient_id']}
        by_patient={p['patient_id']:p for p in patients};by_person_date=defaultdict(list)
        for p in sorted(patients,key=lambda p:p['patient_id']):by_person_date[(p['person_id'],p['date'])].append(p)
        investigations=_anc_fetch(c,'report.investigation_register','person_id',all_people)
        investigation_map=defaultdict(list)
        for i in sorted(investigations,key=lambda i:(i['date'] or date.min,i['laboratory_investigation_id']),reverse=True):investigation_map[i['person_id']].append(i)
        def lab(person,kind,when,prior=False,since=None):
            for i in investigation_map[person]:
                if i['investigation_type']==kind and i['date'] and when and (i['date']<=when if prior else i['date']==when) and (since is None or i['date']>=since):
                    return i['result'] if i['result'] is not None else 'PENDING'
            return 'ND'
        art=_anc_fetch(c,'report.art_register','person_id',all_people);arts=defaultdict(list)
        for a in sorted(art,key=lambda x:(x['date'] or date.min,x['art_id']),reverse=True):arts[a['person_id']].append(a)
        art_ids={a['art_id'] for a in art};states=_anc_fetch(c,'report.art_current_status_register','art_register_id',art_ids)
        stages=_anc_fetch(c,'report.art_who_stage_register','art_register_id',art_ids)
        def started(a,when,strict=True):
            if not a or not when:return False
            return any(s['art_register_id']==a['art_id'] and s['state']=='START_ARV' and (s['date']<when if strict else s['date']==when) for s in states)
        hts=_anc_fetch(c,'report.hts_register','person_id',all_people);hts_person=defaultdict(list);hts_patient=defaultdict(list)
        for h in sorted(hts,key=lambda h:(h['date'] or date.min,h['time'] or datetime.min,h['hts_id']),reverse=True):hts_person[h['person_id']].append(h);hts_patient[h['patient_id']].append(h)
        screens=_anc_fetch(c,'report.hts_screening_register','patient_id',{p['patient_id'] for p in patients})
        screen_people=defaultdict(list)
        for h in screens:
            p=by_patient.get(h['patient_id'])
            if p:screen_people[p['person_id']].append(h)
        def hiv_prior(person,when):
            if any(a['date_enrolled'] and when and a['date_enrolled']<when for a in arts[person]):return 'POSITIVE'
            if any(s['result']=='POSITIVE' and s['date_last_tested'] and when and s['date_last_tested']<=when for s in screen_people[person]):return 'POSITIVE'
            return next((i['result'] for i in investigation_map[person] if i['investigation_type'] in ('HIV','HIV_SYPHILIS_DUO','HIV_DNA_PCR') and i['date'] and when and i['date']<when and i['result'] is not None),'UNKNOWN')
        specials=_query(c,"SELECT investigation_id FROM terminology.special_investigation WHERE investigation_type IN ('HIV','HIV_DNA_PCR')")
        hiv_ids={x['investigation_id'] for x in specials};person_investigations=_anc_fetch(c,'report.person_investigation_report','person_id',people)
        def positive_at(person,when):
            if any(a['date'] and when and a['date']<=when for a in arts[person]):return True
            tests=sorted((i for i in person_investigations if i['person_id']==person and i['investigation_id'] in hiv_ids and i['date'] and when and i['date']<=when),key=lambda i:(i['date'],i['person_investigation_id']),reverse=True)
            return bool(tests and (tests[0]['result'] or '').upper()=='POSITIVE')
        rbf=_anc_fetch(c,'report.rbf_client_register','person_id',people)
        dispenses=_anc_fetch(c,'report.dispense_register','person_id',all_people)
        def medicine(person,names,when,since=None):
            return sum(x['person_id']==person and x['medicine'] in names and x['date'] and when and (x['date']==when if since is None else since<=x['date']<=when) for x in dispenses)
        vaccinations=_anc_fetch(c,'report.vaccination_report','person_id',people)
        education=_anc_fetch(c,'report.health_education_report','patient_id',patient_ids)
        screening=_anc_fetch(c,'report.tb_screening_register','patient_id',patient_ids);tb={s['patient_id']:('Presumptive' if _flag(s['presumptive'])=='Yes' else 'Not Presumptive') for s in screening}
        prep=_anc_fetch(c,'report.prep_register','person_id',people)
        complications=_anc_fetch(c,'report.anc_visit_register_complications','anc_visit_register_anc_visit_id',{v['anc_visit_id'] for v in visits})
        methods=_anc_fetch(c,'report.anc_visit_register_family_planning_methods','anc_visit_register_anc_visit_id',{v['anc_visit_id'] for v in visits})
        qs={r['question_id']:r['name'] for r in _anc_fetch(c,'terminology.question','question_id',{r['complications'] for r in complications})}
        fs={r['family_planning_status_id']:r['name'] for r in _anc_fetch(c,'terminology.family_planning_status','family_planning_status_id',{r['family_planning_methods'] for r in methods})}
        pregnancy=_anc_fetch(c,'report.pregnancy_report','person_id',people);preg_ids={r['pregnancy_id'] for r in pregnancy}
        delivery=_anc_fetch(c,'report.delivery_register','person_id',people)
        still=_anc_fetch(c,'report.still_birth_register','mother_person_id',people)
        preg_live=_anc_fetch(c,'report.pregnancy_live_birth_report','pregnancy_id',preg_ids)
        preg_still=_anc_fetch(c,'report.pregnancy_still_birth_report','pregnancy_id',preg_ids)
        preg_person={r['pregnancy_id']:r['person_id'] for r in pregnancy}
        def syphilis(person,when):
            result=lab(person,'SYPHILIS_TEST',when)
            if result!='ND':return result
            duo=lab(person,'HIV_SYPHILIS_DUO',when)
            return 'POSITIVE' if duo in ('HIV AND SYPHILIS POSITIVE','HIV NEGATIVE AND SYPHILIS POSITIVE') else 'NEGATIVE' if duo in ('HIV AND SYPHILIS NEGATIVE','HIV POSITIVE AND SYPHILIS NEGATIVE') else 'ND'
        rows=[]
        for a in registrations:
            row=_base_row(a,site);person=a['person_id'];booking=a['date'];partner=a['partner_id'];latest_art=arts[person][0] if arts[person] else None
            current_preg={p['pregnancy_id'] for p in pregnancy if p['person_id']==person}
            gravida=len(current_preg)+len({d['delivery_id'] for d in delivery if d['person_id']==person and d['anc_id'] not in current_preg})+1
            parity=sum(s['mother_person_id']==person for s in still)+sum(preg_person.get(p['pregnancy_id'])==person for p in preg_live)+sum(preg_person.get(p['pregnancy_id'])==person for p in preg_still)
            patient=next(iter(by_person_date[(person,booking)]),{})
            rbf_candidates=sorted((r for r in rbf if r['person_id']==person and r['date'] and booking and r['date']<=booking),key=lambda r:(r['date'],r['rbf_client_id']),reverse=True)
            art_date=(latest_art['date'] or latest_art['date_enrolled']) if latest_art else None
            already=bool(latest_art and (started(latest_art,booking) or (art_date and booking and art_date<booking)))
            row.update(ancNumber=a['anc_number'],fullName=' '.join(x for x in [a['firstname'],a['lastname']] if x) or None,
              contactNumber=a['phone_number'],address=a['address'],firstBooking=None if a['first_booking'] is None else ('Yes' if a['first_booking'].lower()=='true' else 'No'),
              gestationalAge=_anc_gestation(a['lnmp'],booking),gravida=gravida,parity=parity,lnmp=a['lnmp'],edd=a['edd'],
              hivStatusPriorAnc=hiv_prior(person,booking),artInitiated=('Yes' if already else 'No') if latest_art else 'N/A',artNumber=latest_art['art_number'] if latest_art else None,
              bloodGroup=lab(person,'BLOOD_GROUP',booking,True),pregnancyTest=lab(person,'PREGNANCY',booking,True) if patient else None,
              height=patient.get('height'),pulse=patient.get('pulse'),rbfNumber=rbf_candidates[0]['number'] if rbf_candidates else None,
              nextOfKin=' '.join(x for x in [a['kin_firstname'],a['kin_lastname']] if x) or None,nextOfKinPhone=a['kin_phone'],nextOfKinAddress=a['kin_address'],visits=[])
            td_count=0;ipt_count=0
            for v in visit_map[a['anc_id']]:
                p=by_patient.get(v['patient_id'],{});when=p.get('date') or v['date'];vi={'recordId':v['anc_visit_id'],'visitDate':when,'visitNumber':v['visit_number'],'gestationalAge':_anc_gestation(a['lnmp'],v['date'])}
                for target,source in [('weight','weight'),('height','height'),('pulse','pulse'),('bloodPressure','blood_pressure'),('muac','muac'),('fetalHeartRate','fetal_heart_rate')]:vi[target]=p.get(source)
                for target,source in [('heightOfFundus','height_of_fundus'),('presentation','presentation'),('position','position'),('pallor','pallor'),('deliveryPlan','delivery_plan'),('comments','comments'),('ussDone','uss_done'),('scanResult','scan_result'),('scanEdd','edd'),('noOfBabies','no_of_babies')]:vi[target]=v.get(source)
                vi.update(bloodGroup=row['bloodGroup'],urinalysisResult=None,syphilisResult=syphilis(person,when),
                  artInitiated='Yes' if started(latest_art,when,False) else 'No',artNumber=row['artNumber'],artFollowupStatus=next((s['follow_up_status'] for s in stages if latest_art and s['art_register_id']==latest_art['art_id'] and s['date']==when),None) if latest_art else 'NA',
                  tbScreening=tb.get(v['patient_id'],'ND'),calciumSupplements=_flag(v['calcium_supplement']),proteinSupplements=_flag(v['protein_supplement']),
                  ironSupplements='Yes' if medicine(person,['IRON AND FOLIC ACID','IRON IN COMBINATION WITH FOLIC ACID'],when) else 'No',
                  nutritionCounselling='Yes' if any(e['patient_id']==v['patient_id'] and e['topic_id'] in ('024','030','041') for e in education) else 'No',
                  pregnancyComplications=', '.join(qs.get(x['complications'],x['complications'] or '') for x in complications if x['anc_visit_register_anc_visit_id']==v['anc_visit_id']) or 'NIL',
                  postPartumCounselling=', '.join(fs.get(x['family_planning_methods'],x['family_planning_methods'] or '') for x in methods if x['anc_visit_register_anc_visit_id']==v['anc_visit_id']) or None,
                  prepDrugs='YES' if any(x['person_id']==person and _flag(x['client_offered_prep'])=='Yes' and x['date'] and (not from_date or x['date']>=from_date) and (not to_date or x['date']<=to_date) for x in prep) else 'NONE')
                for target,kind in [('cd4Result','CD4_COUNT'),('viralLoadResult','VIRAL_LOAD'),('hbResult','HAEMOGLOBIN'),('rhResult','RHESUS_RH'),('glucoseResult','SUGAR'),('hepatitisBResult','HEPATITIS_B_ANTIGEN')]:
                    vi[target]='N/A' if kind in ('CD4_COUNT','VIRAL_LOAD') and not positive_at(person,v['date']) else lab(person,kind,v['date'])
                if vi['glucoseResult']=='ND':vi['glucoseResult']=lab(person,'BLOOD_SUGAR',v['date'])
                hv=next(iter(hts_patient[v['patient_id']]),None)
                vi['hivResult']='OPTED OUT' if hv and _flag(hv['opt'])=='Yes' else hv['result'] if hv and hv['test_purpose'] in ('ANC','ANC 1st contact','ANC 2nd contact','ANC 2+ contact','ANC 2nd+ contact (Pregnant and L&D)','ANC 2nd+ contact (Breastfeeding)') else 'ND'
                vi['syphilisDose']='N/A' if (vi['syphilisResult'] or '').upper() in ('ND','NEGATIVE') else ('Yes' if medicine(person,['BENZATHINE PENICILLINE'],when,booking) else 'No')
                vaccinated=any(x['person_id']==person and x['drug_name']=='TETANUS TOXOID COMBINATIONS WITH DIPTHERIA TOXOID' and x['date_vaccinated']==when for x in vaccinations)
                td_count+=int(vaccinated);given=bool(medicine(person,['SULPHADOXINE(Pyrimethamine)'],when));ipt_count+=int(given)
                vi['tetanusToxoid']='TD'+str(td_count) if vaccinated else None;vi['malariaProphylaxis']='IPTp'+str(ipt_count) if given else None
                vi.update(partnerHivResult=None,partnerHivStatus=None,partnerAlreadyPositive=None,syphilisPartnerResult=None,syphilisPartnerDose=None)
                if partner:
                    ph=next((h for h in hts_person[partner] if h['date'] and booking and h['date']>=booking),None)
                    exact=next((h for h in hts_person[partner] if h['date']==when),None)
                    partner_s=lab(partner,'SYPHILIS_TEST',when,True,booking)
                    vi.update(partnerHivStatus=ph['result'] if ph else 'UNKNOWN',partnerHivResult='OPTED OUT' if exact and _flag(exact['opt'])=='Yes' else exact['result'] if exact and exact['result'] else 'ND',
                      partnerAlreadyPositive='Yes' if any(x['date'] and booking and x['date']<booking for x in arts[partner]) else 'No',
                      syphilisPartnerResult=partner_s,syphilisPartnerDose='Yes' if medicine(partner,['BENZATHINE PENICILLINE'],when,booking) else 'No')
                row['visits'].append(vi)
            rows.append(row)
        return rows,site
    except mysql.connector.Error as error:raise HTTPException(503,f'ANC source unavailable (database error {error.errno}). Check read permissions.')
    finally:
        if c is not None:c.close()


@app.get('/api/anc')
def anc_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,site=anc_data(from_date,to_date,facility);total=len(rows)
    data=[{k:v for k,v in r.items() if k!='personId'} for r in rows[(page-1)*page_size:page*page_size]]
    return {'data':data,'pagination':{'page':page,'pageSize':page_size,'total':total,'totalPages':(total+page_size-1)//page_size},
      'meta':{'source':'AncRegisterReportSource / AncReportUtil / AncVisitsProcessor','definition':'registration dates select ANC records; all linked visits included',
        'facility':site,'recordedFields':['edd'],'unmappedVisitFields':['urinalysisResult'],
        'notes':'EDD uses recorded source value because EddCalculateUtil is supplied outside this module. Urinalysis shared test-ID constants are not available here. Missing source values are not inferred.'}}


@app.get('/api/anc/summary')
def anc_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=anc_data(from_date,to_date,facility);daily=Counter(str(r['eventDate']) for r in rows if r['eventDate']);ages=Counter()
    for r in rows:
        age=r['age'];ages['Unknown' if age is None else 'Under 20' if age<20 else '20-24' if age<25 else '25-29' if age<30 else '30-34' if age<35 else '35+']+=1
    bookings=Counter(r['firstBooking'] or 'Unknown' for r in rows)
    return {'total':len(rows),'uniquePeople':len({r['personId'] for r in rows if r['personId']}),
      'monthlyBySex':_monthly_sex(rows),'daily':[{'date':k,'count':daily[k]} for k in sorted(daily)],'sex':[{'label':k,'count':v} for k,v in Counter(r['sex'] or 'Unknown' for r in rows).items()],
      'ageBands':[{'label':k,'count':ages[k]} for k in ['Under 20','20-24','25-29','30-34','35+','Unknown']],
      'bookingTypes':[{'label':k,'count':v} for k,v in bookings.items()],
      'recorded':{'firstBookings':sum(r['firstBooking']=='Yes' for r in rows),'linkedVisits':sum(len(r['visits']) for r in rows)},'facilities':[site]}


def _anc_cell(row,key):
    value=row.get('ageDisplay') if key=='age' and row.get('ageDisplay') else row.get(key)
    return cell_value(value)


def _anc_safe_csv(value):
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
    return value


@app.get('/api/anc/export')
def anc_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=anc_data(from_date,to_date,facility)
    headers={'Content-Disposition':f'attachment; filename="anc-{from_date or "all"}-{to_date or "all"}.{format}"','Cache-Control':'no-store'}
    if format=='csv':
        output=StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['Registration ID']+[label for _,label in ANC_FIELDS]+['Visit ID']+['Visit: '+label for _,label in ANC_VISIT_FIELDS])
        for row in rows:
            for visit in row['visits'] or [{}]:
                writer.writerow([_anc_safe_csv(value) for value in [row['recordId']]+[_anc_cell(row,key) for key,_ in ANC_FIELDS]+[visit.get('recordId','')]+[_anc_cell(visit,key) for key,_ in ANC_VISIT_FIELDS]])
        return Response(('\ufeff'+output.getvalue()).encode('utf-8'),media_type='text/csv',headers=headers)
    if format=='xlsx':
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font,PatternFill
        from openpyxl.utils import get_column_letter
        output=BytesIO();book=Workbook(write_only=True)
        def add_sheet(name,labels,values):
            sheet=book.create_sheet(name);sheet.freeze_panes='A2'
            for i in range(len(labels)):sheet.column_dimensions[get_column_letter(i+1)].width=24
            cells=[]
            for label in labels:
                item=WriteOnlyCell(sheet,value=label);item.font=Font(bold=True,color='FFFFFF');item.fill=PatternFill('solid',fgColor='218A39');cells.append(item)
            sheet.append(cells);count=1
            for record in values:
                cells=[]
                for val in record:
                    item=WriteOnlyCell(sheet,value=val)
                    if isinstance(val,str):item.data_type='s'
                    cells.append(item)
                sheet.append(cells);count+=1
            sheet.auto_filter.ref=f'A1:{get_column_letter(len(labels))}{count}'
        add_sheet('ANC registrations',['Registration ID']+[label for _,label in ANC_FIELDS],([r['recordId']]+[_anc_cell(r,key) for key,_ in ANC_FIELDS] for r in rows))
        add_sheet('ANC visits',['Registration ID','ANC number','Visit ID']+[label for _,label in ANC_VISIT_FIELDS],([r['recordId'],r['ancNumber'],v['recordId']]+[_anc_cell(v,key) for key,_ in ANC_VISIT_FIELDS] for r in rows for v in r['visits']))
        book.save(output)
        return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers=headers)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,LongTable,TableStyle,PageBreak
    from xml.sax.saxutils import escape
    output=BytesIO();styles=getSampleStyleSheet();cell=ParagraphStyle('ANCcell',fontSize=7,leading=9,wordWrap='CJK')
    doc=SimpleDocTemplate(output,pagesize=landscape(A4),leftMargin=24,rightMargin=24,topMargin=26,bottomMargin=28)
    content=[]
    def add_fields(row,fields):
        pairs=[(Paragraph(escape(label),cell),Paragraph(escape(str(_anc_cell(row,key))) or '-',cell)) for key,label in fields]
        half=(len(pairs)+1)//2;data=[]
        for i in range(half):data.append([*pairs[i],*(pairs[i+half] if i+half<len(pairs) else ('',''))])
        table=LongTable(data,colWidths=[145,252,145,252],splitByRow=1,splitInRow=1)
        table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#eaf5eb')),('BACKGROUND',(2,0),(2,-1),colors.HexColor('#eaf5eb')),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#dceadd')),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
        content.append(table)
    for n,row in enumerate(rows):
        if n:content.append(PageBreak())
        content.append(Paragraph('ANC Register | '+escape(site['name']),styles['Heading2']))
        content.append(Paragraph(f'Registrations: {from_date or "All dates"} to {to_date or "All dates"} | Record {n+1} of {len(rows)} | {len(row["visits"])} linked visits (all dates)',styles['Normal']))
        content.append(Spacer(1,10));add_fields(row,ANC_FIELDS)
        for visit in row['visits']:
            content.append(PageBreak());content.append(Paragraph('ANC '+escape(str(row['ancNumber'] or row['recordId']))+' | Visit '+str(visit['visitNumber']),styles['Heading2']))
            content.append(Paragraph(escape(site['name'])+' | '+escape(str(visit['visitDate'] or 'Date not recorded')),styles['Normal']));content.append(Spacer(1,10));add_fields(visit,ANC_VISIT_FIELDS)
    if not rows:content=[Paragraph('ANC Register | No registrations for these dates',styles['Heading2'])]
    def page(canvas,document):
        canvas.saveState();canvas.setFillColor(colors.white);canvas.rect(0,0,*landscape(A4),fill=1,stroke=0);canvas.restoreState();canvas.setFont('Helvetica',8)
        canvas.drawString(24,12,'ANC registration and all linked visits | Missing source values shown as a dash.');canvas.drawRightString(818,12,f'Page {document.page}')
    doc.build(content,onFirstPage=page,onLaterPages=page)
    return Response(output.getvalue(),media_type='application/pdf',headers=headers)


# DS-DR TB fields from the original combined register; unverified fields remain null.
TB_FIELDS = [('organisationUnit', 'Organisation unit'), ('eventDate', 'Registration date'), ('diagnosisDate', 'Diagnosis date'), ('dateOfNotification', 'Date of notification'), ('treatmentInitiated', 'Treatment initiated'), ('treatmentInitiatedDate', 'Treatment initiated date'), ('reasonForNotInitiatingTreatment', 'Reason for not initiating treatment'), ('tbNumber', 'TB number'), ('fullName', 'Full name'), ('idNumber', 'Id number'), ('sex', 'Sex'), ('age', 'Age'), ('address', 'Address'), ('phoneNumber', 'Phone number'), ('typeOfReferral', 'Type of referral'), ('tbRiskGroup', 'Tb risk group'), ('nextOfKinFullName', 'Next of kin full name'), ('nextOfKinAddress', 'Next of kin address'), ('nextOfKinPhoneNumber', 'Next of kin phone number'), ('diagnosis', 'Diagnosis'), ('diagnosisType', 'Diagnosis type'), ('transferFrom', 'Transfer from'), ('preTreatmentLabResults', 'Pre treatment lab results'), ('pulmonaryBactereaologicallyConfirmed', 'Pulmonary bactereaologically confirmed'), ('pulmonaryClinicalDiagnosed', 'Pulmonary clinical diagnosed'), ('extrapulmonaryBacteriologicallyConfirmed', 'Extrapulmonary bacteriologically confirmed'), ('patientType', 'Patient type'), ('endOfIntensivePhazeResult', 'End of intensive phase result'), ('endOfIntensivePhazeLabNumber', 'End of intensive phase lab number'), ('endOfMonthResult', 'End of month result'), ('endOfMonthLabNumber', 'End of month lab number'), ('endOfContinuationPhaseResult', 'End of continuation phase result'), ('endOfContinuationPhaseLabNumber', 'End of continuation phase lab number'), ('testType1', 'Test type1'), ('testType1Date', 'Test type1 date'), ('testType1Result', 'Test type1 result'), ('testType2', 'Test type2'), ('testType2Date', 'Test type2 date'), ('testType2Result', 'Test type2 result'), ('hivResult', 'HIV result code'), ('dateOfHivTest', 'Date of hiv test'), ('onART', 'On ART'), ('artStartDate', 'Art start date'), ('artNumber', 'Art number'), ('artInitiatedWithin8Weeks', 'Art initiated within8 weeks'), ('lessThan5YeartbContactsIdentified', 'Less than5 yeartb contacts identified'), ('between5and14YeartbContactsIdentified', 'Between5and14 yeartb contacts identified'), ('above15YeartbContactsIdentified', 'Above15 yeartb contacts identified'), ('tbContactsIdentified', 'Tb contacts identified'), ('lessThan5YeartbContactsScreenedForTb', 'Less than5 yeartb contacts screened for tb'), ('between5and14YeartbContactsScreenedForTb', 'Between5and14 yeartb contacts screened for tb'), ('above15YeartbContactsScreenedForTb', 'Above15 yeartb contacts screened for tb'), ('lessThan5YeartbContactsDiagnosedForTb', 'Less than5 yeartb contacts diagnosed for tb'), ('between5and14YeartbContactsDiagnosedForTb', 'Between5and14 yeartb contacts diagnosed for tb'), ('above15YeartbContactsDiagnosedForTb', 'Above15 yeartb contacts diagnosed for tb'), ('under5YearsContactsStartedOnIpt', 'Under5 years contacts started on ipt'), ('transferedTo', 'Transfered to'), ('transferedToDate', 'Transfered to date'), ('tbDot', 'DOT type'), ('adverseEvent', 'Adverse event'), ('typeOfDotEvaluatedAtEndOfTreatment', 'Type of dot evaluated at end of treatment'), ('treatmentOutcome', 'Recorded treatment outcome'), ('remarks', 'Remarks'), ('onCpt', 'On cpt'), ('cptStartDate', 'Cpt start date'), ('treatmentOutcomeDate', 'Outcome date'), ('totalNumberOfContactsDiagnosedWithTb', 'Contacts recorded in period with TB registration history'), ('typeOfPalliativeCareOffered', 'Type of palliative care offered'), ('palliativeCareEligibility', 'Eligible for palliative care'), ('month', 'Month'), ('tbDotMonth', 'Tb dot month'), ('tbDotDay', 'Tb dot day'), ('dmTest1Done', 'Diabetes test1 done'), ('dmTest1Result', 'Diabetes test1 result'), ('dmTest1Status', 'Diabetes test1 status'), ('dmTest2Done', 'Diabetes test2 done'), ('dmTest2Result', 'Diabetes test2 result'), ('dmTest2Status', 'Diabetes test2 status'), ('appointmentMonth', 'Appointment month'), ('appointmentDay', 'Appointment day'), ('tbHistory', 'Previous treatment history (verified only)'), ('molecularDiagnosticTestResults', 'Person-scoped molecular diagnostic result'), ('drtbDiagnosisInformationDateTaken', 'Drtb diagnosis information date taken'), ('drtbDiagnosisInformationResultReceivedTaken', 'Drtb diagnosis information result received taken'), ('drtbDiagnosisInformationRif', 'Drtb diagnosis information rif'), ('drtbDiagnosisInformationInh', 'Drtb diagnosis information inh'), ('drtbDiagnosisInformationPa', 'Drtb diagnosis information pa'), ('drtbDiagnosisInformationE', 'Drtb diagnosis information e'), ('drtbDiagnosisInformationBdq', 'Drtb diagnosis information bdq'), ('drtbDiagnosisInformationLzd', 'Drtb diagnosis information lzd'), ('drtbDiagnosisInformationMfx', 'Drtb diagnosis information mfx'), ('drtbDiagnosisInformationLfx', 'Drtb diagnosis information lfx'), ('drtbDiagnosisInformationEto', 'Drtb diagnosis information eto'), ('drtbDiagnosisInformationOfx', 'Drtb diagnosis information ofx'), ('drtbDiagnosisInformationCfz', 'Drtb diagnosis information cfz'), ('drClassification', 'Drug-resistance classification'), ('siteOfInfection', 'Site of infection'), ('typeOfTreatmentRegimen', 'Type of treatment regimen')]
TB_CONTACT_COUNT_KEYS = ['lessThan5YeartbContactsIdentified', 'between5and14YeartbContactsIdentified', 'above15YeartbContactsIdentified', 'lessThan5YeartbContactsScreenedForTb', 'between5and14YeartbContactsScreenedForTb', 'above15YeartbContactsScreenedForTb', 'lessThan5YeartbContactsDiagnosedForTb', 'between5and14YeartbContactsDiagnosedForTb', 'above15YeartbContactsDiagnosedForTb', 'under5YearsContactsStartedOnIpt']
TB_CULTURE_ID='301241f4-8512-11e8-a6c2-c48e8faf029b'
TB_LAB_IDS={'95be2b2a-8511-11e8-a6c2-c48e8faf029b',TB_CULTURE_ID}
TB_GLUCOSE_IDS={'36069726-adee-11e7-b30f-3372a2d8551e','36069df7-adee-11e7-b30f-3372a2d8551e'}
TB_PHASES={'41fa2db8-3c4c-11ee-b710-0242ac120003':'endOfIntensivePhaze','5d8aea33-3c4c-11ee-b710-0242ac120003':'endOfContinuationPhase','51709697-3c4c-11ee-b710-0242ac120003':'endOfMonth'}
TB_HRZE_REGIMENS={'HRZE','HR','LZD-BDQ-hdINH-LFX-CFZ-EMB-PZA','BDQ-hdINH-LFX-CFZ-EMB-PZA','LFX-CFZ-EMB-PZA','LZD-BDQ-CS-LFX-CFZ','BDQ-CS-LFX-CFZ','CS-LFX-CFZ','LZD-DLM-CS-LFX-CFZ','DLM-CS-LFX-CFZ','CS-LFX-CFZ'}
TB_DEVIATIONS=['Molecular diagnostic result remains null: original result-ID map unavailable; no global or guessed test-name results.', 'Contact counts scoped to this TB registration; adverse events join the actual tb_id.', 'Latest associated records use date and record-ID tie breakers; phase tests use selected reporting period.', 'Recorded outcome and date are separate; raw outcome values are retained.', 'Pretreatment lookup narrows to source-verified DSM and culture investigation IDs; raw registered pretreatment result is fallback.', 'Treatment-history classification remains null; prior TPT alone does not establish complete history.', 'DR drug sensitivities, unassigned original fields, daily DOT calendars and ambiguous treatment-history classifications remain null.', 'Palliative eligibility uses the actual eligibility flag, not the separate care-offered flag.', 'Age-specific screened, diagnosed and IPT counts remain null: original source does not assign them.']
# DS-DR TB reporting extension. Parent selection follows TbRegisterReportSource.
def _tb_group(rows,key):
    result=defaultdict(list)
    for item in rows:result[item.get(key)].append(item)
    return result

def _tb_sorted(rows,field='date',id_field=None):
    return sorted(rows,key=lambda r:(_day(r.get(field)) or date.min,str(r.get(id_field,'')) if id_field else str(r)),reverse=True)

def _tb_period(value,start,end):
    value=_day(value)
    return isinstance(value,date) and (not start or value>=start) and (not end or value<=end)

def tb_data(from_date,to_date,facility=None):
    clause,args=_dates('t.date',from_date,to_date);c=None
    try:
        c=connect();site=_facility(c);_check_site(site,facility)
        registrations=_query(c,f'SELECT * FROM report.tb_register t WHERE {clause} ORDER BY t.date DESC,t.tb_id DESC',args)
        if not registrations:return [],site
        ids={r['tb_id'] for r in registrations};people={r['person_id'] for r in registrations if r['person_id']}
        fetch=lambda table,key,values:_anc_fetch(c,table,key,values)
        statuses=_tb_group(fetch('report.tb_register_treatment_current_status','tb_id',ids),'tb_id')
        contacts=_tb_group(fetch('report.tb_contact_register','tb_id',ids),'tb_id')
        dots=_tb_group(fetch('report.tb_directly_observed_treatment_register','tb_id',ids),'tb_id')
        appointments=_tb_group(fetch('report.tb_appointment_report','tb_register_id',ids),'tb_register_id')
        visits=_tb_group(fetch('report.tb_visit_register','tb',ids),'tb')
        adverse=_tb_group(fetch('consultation.tb_adverse_event','tb_id',ids),'tb_id')
        risklinks=_tb_group(fetch('report.tb_register_tb_risk_group','tb_register_tb_id',ids),'tb_register_tb_id')
        risknames={r['tb_risk_group_id']:r['name'] for r in _query(c,'SELECT * FROM terminology.tb_risk_group')}
        palliation=_tb_group(fetch('report.tb_register_palliative_care_offered','id',{s['tb_treatment_current_status_id'] for group in statuses.values() for s in group}),'id')
        relations=_tb_group(fetch('report.relationship_register','person_id',people),'person_id')
        kins={r['person_id']:r['next_of_kin_person_id'] for r in fetch('report.next_of_kin_report','person_id',people)}
        members={r['member_id'] for group in contacts.values() for r in group if r['member_id']}
        relatives={r['member_id'] for group in relations.values() for r in group if r['member_id']}
        all_people=people|members|relatives|{x for x in kins.values() if x}
        demographics={r['person_id']:r for r in fetch('report.person_demographic','person_id',all_people)}
        histories=_tb_group(fetch('report.tb_register','person_id',people|members),'person_id')
        identification=_tb_group(fetch('report.person_identification_report','person_id',people),'person_id')
        diagnoses=_tb_group(fetch('report.patient_diagnosis_report','patient_id',{r['patient_id'] for r in registrations if r['patient_id']}),'patient_id')
        arts=_tb_group(fetch('report.art_register','person_id',people),'person_id')
        hts=_tb_group(fetch('report.hts_register','person_id',people),'person_id')
        tpts=_tb_group(fetch('consultation.patient_tpt','person_id',people),'person_id')
        notifications=_tb_group(fetch('consultation.tb_notification','person_id',people),'person_id')
        labs=_tb_group(fetch('report.investigation_register','person_id',people),'person_id')
        labreports=_tb_group(fetch('report.person_investigation_report','person_id',people),'person_id')
        investigations={r['person_investigation_id'] for group in labreports.values() for r in group}
        orders={r['laboratory_investigation_id']:r for r in fetch('report.laboratory_request_order_register','laboratory_investigation_id',investigations)}
        labels=_tb_group(fetch('report.laboratory_request_order_value_register','investigation_order_id',investigations),'investigation_order_id')
        facilities={r['facility_id']:r for r in _query(c,'SELECT * FROM facility.facility')}
        provinces={r['province_id']:r['name'] for r in _query(c,'SELECT * FROM facility.province')}
        def transfer(identifier,when,country=None):
            f=facilities.get(identifier);parts=[f.get('facility_id'),f.get('name'),provinces.get(f.get('province_id'))] if f else [country or identifier]
            if when:parts.append(str(when))
            return ', '.join(str(x) for x in parts if x) or None
        def name(d):return ' '.join(str(d.get(k) or '') for k in ('lastname','firstname')).strip() or None
        data=[]
        for r in registrations:
            person=r['person_id'];when=r['date'];d=demographics.get(person,{})
            out={key:None for key,_ in TB_FIELDS};out.update(_base_row({'recordId':r['tb_id'],'personId':person,'eventDate':when,'sex':d.get('sex'),'birthdate':d.get('birthdate')},site))
            out.update(tbNumber=r['tb_number'],fullName=name(d),address=d.get('address'),phoneNumber=d.get('phone_number'),diagnosis=r['diagnosis'],diagnosisDate=r['diagnosis_date'],dateOfNotification=when,
              treatmentInitiatedDate=r['treatment_start_date'],patientType=r['type'],diagnosisType=r['type_of_tb'],treatmentOutcome=r['outcome'],treatmentOutcomeDate=r['out_come_date'],
              transferFrom=transfer(r['transfer_from'],r['transfer_in_date']),transferedTo=transfer(r['transfer_out_facility_id'],r['out_come_date'],r['transfer_out_country']),transferedToDate=r['out_come_date'],
              extrapulmonaryBacteriologicallyConfirmed=r['extra_pulmonary_site'],preTreatmentLabResults=r['pre_treatment_lab_results'],month=from_date.strftime('%B').upper() if from_date else None)
            if out['diagnosisDate'] and out['diagnosisDate']>when:out['diagnosisDate']=when
            diags=_tb_sorted(diagnoses[r['patient_id']],'date','patient_diagnosis_report_id')
            if diags:out.update(diagnosis=diags[0]['diagnosis'],diagnosisDate=min(diags[0]['date'],when) if diags[0]['date'] else None)
            ident=sorted((i for i in identification[person] if i['type']=='National Id'),key=lambda x:x['identification_id'])
            if ident:out['idNumber']=ident[0]['number']
            st=_tb_sorted(statuses[r['tb_id']],'date','tb_treatment_current_status_id')
            initiations=_tb_sorted([s for s in st if s['treatment_status']=='INITIATE' and s['treatment_start_date']],'treatment_start_date','tb_treatment_current_status_id')
            initiation=initiations[0] if initiations else None;status=st[0] if st else None
            out['treatmentInitiated']='Yes' if initiation else 'No'
            if initiation:out['treatmentInitiatedDate']=initiation['treatment_start_date']
            if status:
                out.update(drClassification=status['tb_drug_resistance_type'],tbDot={'HEALTH_FACILITY':'Health Facility','TRAINED_CHW':'Community','FAMILY':'Other (Specify) - Family'}.get(status['tb_type_dot'],status['tb_type_dot']),typeOfDotEvaluatedAtEndOfTreatment=status['tb_type_dot'],
                  palliativeCareEligibility=_flag(status['client_eligible_for_palliative_care']),typeOfPalliativeCareOffered=', '.join(sorted({x['care_offered'] for x in palliation[status['tb_treatment_current_status_id']] if x['care_offered']})) or None)
                regimen=(status['regimen'] or '').strip()
                out['typeOfTreatmentRegimen']='Individualized' if status['type_of_regimen']=='INDIVIDUALISED_REGIMEN' else 'BPal (M)' if regimen=='BDQ-Pa-LZD-MFX' else 'HRZE(LFX)' if regimen in TB_HRZE_REGIMENS else None
            out['tbRiskGroup']='; '.join(sorted({risknames[x['tb_risk_group_id']] for x in risklinks[r['tb_id']] if x['tb_risk_group_id'] in risknames})) or None
            relative=kins.get(person)
            if not relative and relations[person]:relative=sorted(relations[person],key=lambda x:x['relationship_id'])[0]['member_id']
            kin=demographics.get(relative,{})
            out.update(nextOfKinFullName=name(kin) or r['next_of_kin_full_name'],nextOfKinAddress=kin.get('address') or r['next_of_kin_address'],nextOfKinPhoneNumber=kin.get('phone_number') or r['next_of_kin_phone_number'])
            dtype=r['tb_disease_type'];dsite=r['tb_disease_site']
            out['siteOfInfection']={'PULMONARY':'Pulmonary TB','EXTRA_PULMONARY':'Extra Pulmonary TB'}.get(dtype)
            if dtype and dsite:
                out['pulmonaryBactereaologicallyConfirmed']='Yes' if dtype=='PULMONARY' and dsite=='BACTERIOLLOGICALLY_CONFIRMED' else 'No'
                out['pulmonaryClinicalDiagnosed']='Yes' if dtype=='PULMONARY' and dsite=='CLINICALLY_DIAGNOSED' else 'No'
            referral=r['client_referral_type'];out['typeOfReferral']={'TRAINED_COMMUNITY_WORKERS':'Trained Community Workers','TARGETED_COMMUNITY_TB_OUTREACH':'Targeted Community TB Outreach','REFERRED_COMMUNITY_ONLY':'Targeted Community TB Outreach'}.get(referral,'Other (Specify)' if referral else None)
            ns=_tb_sorted([n for n in notifications[person] if n['date']<=when],'date','tb_notification_id')
            if ns:out['dateOfNotification']=ns[0]['date']
            # Correct source mistakes: per-TB contacts and true TB foreign key for adverse events.
            own_contacts=contacts[r['tb_id']];out['tbContactsIdentified']=len(own_contacts)
            # Age-specific screened/diagnosed/IPT fields are never assigned by the original source.
            # Keep them null rather than deriving new clinical measures.
            diagnosed=sum(1 for contact in own_contacts if _tb_period(contact['date'],from_date,to_date)
                and any(h['date'] and h['date']<=(to_date or when) for h in histories[contact['member_id']]))
            out['totalNumberOfContactsDiagnosedWithTb']=diagnosed
            events=[a for a in adverse[r['tb_id']] if _tb_period(_tb_parse_date(a['ae_onset_date']),from_date,to_date)]
            out['adverseEvent']='Yes' if events else 'No'
            dot=_tb_sorted([a for a in dots[r['tb_id']] if _tb_period(a['date'],from_date,to_date)],'date','dot_id')
            if dot:out.update(tbDotMonth=dot[0]['date'].strftime('%B').upper(),tbDotDay=dot[0]['date'].day)
            appts=_tb_sorted(appointments[r['tb_id']],'date','tb_appointment_id')
            if appts:out.update(appointmentMonth=appts[0]['date'].strftime('%Y %B').upper(),appointmentDay=appts[0]['date'].day)
            # Per-person tests, deterministic most recent date/ID; never another person's result.
            priorlabs=_tb_sorted([i for i in labs[person] if i['date'] and i['date']<=when],'date','laboratory_investigation_id')
            tb_labs=[i for i in priorlabs if i['investigation_id'] in TB_LAB_IDS]
            if tb_labs:out['preTreatmentLabResults']=tb_labs[0]['result']
            # Result-ID mapping constants are unavailable; do not guess molecular types from names.
            end=to_date or when
            art=_tb_sorted([a for a in arts[person] if a['date'] and a['date']<end],'date','art_id')
            if art:out.update(onART='Yes',artStartDate=art[0]['date'],artNumber=art[0]['art_number'],hivResult='Code 1',dateOfHivTest=art[0]['date_of_hiv_test'])
            else:
                out['onART']='No'
                hiv=next((i for i in priorlabs if i['investigation_type']=='HIV'),None)
                result=(hiv['result'] or '').upper() if hiv else None
                out['hivResult']='Code 0' if result=='NEGATIVE' else 'Code 1' if result=='POSITIVE' else 'Code 9'
                hv=_tb_sorted([h for h in hts[person] if h['date'] and h['date']<end],'date','hts_id')
                if hv:out['dateOfHivTest']=hv[0]['date']
            reports=_tb_sorted([i for i in labreports[person] if _tb_period(i['date'],from_date,to_date)],'date','person_investigation_id')
            cultures=[i for i in reports if i['investigation_id']==TB_CULTURE_ID]
            for index,lab in enumerate(cultures[:2],1):out.update({f'testType{index}':lab['test'],f'testType{index}Date':lab['date'],f'testType{index}Result':lab['result']})
            for lab in reports:
                orderid=lab['person_investigation_id'];lablabels=sorted(labels[orderid],key=lambda x:(x['date'] or date.min,x['laboratory_request_order_value_id']),reverse=True)
                if lab['investigation_id'] in TB_LAB_IDS:
                    label=next((x for x in lablabels if x['label']=='testReason'),None)
                    if label and label['label_value'] in TB_PHASES:
                        prefix=TB_PHASES[label['label_value']]
                        if out.get(prefix+'Result') is None:out[prefix+'Result']=lab['result'] or 'INVESTIGATION DONE NO DOCUMENTED RESULT';out[prefix+'LabNumber']=orders.get(orderid,{}).get('laboratory_request_number')
                if lab['investigation_id'] in TB_GLUCOSE_IDS:
                    label=next((x for x in lablabels if x['label']=='bloodGlucoseTestType'),None)
                    choice={'902ba6cc-c59a-4144-b819-59a3cf1feffd':('dmTest1','RBS'),'2a7e5dc6-572c-406c-aa28-c4b857e7afe5':('dmTest2','FBS')}.get(label['label_value'] if label else None)
                    if choice and out.get(choice[0]+'Done') is None:out[choice[0]+'Done']=choice[1];out[choice[0]+'Result']=' '.join(str(x) for x in (lab['result'],lab['date']) if x)
            # Historical treatment classification remains unverified rather than reproducing reversed date arithmetic.
            # Prior TPT alone does not establish a complete previous-TB-treatment history.
            out['linkedVisits']=len(visits[r['tb_id']])
            data.append(out)
        return data,site
    except mysql.connector.Error as error:raise HTTPException(503,f'TB source unavailable (database error {error.errno}). Check read permissions.')
    finally:
        if c is not None:c.close()

def _tb_parse_date(value):
    if isinstance(value,date):return _day(value)
    try:return date.fromisoformat(str(value)[:10])
    except (ValueError,TypeError):return None

@app.get('/api/tb')
def tb_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,site=tb_data(from_date,to_date,facility);offset=(page-1)*page_size
    return {'data':[{k:v for k,v in r.items() if k!='personId'} for r in rows[offset:offset+page_size]],'pagination':{'page':page,'pageSize':page_size,'total':len(rows),'totalPages':(len(rows)+page_size-1)//page_size},'meta':{'source':'TbRegisterReportSource (DS-DR TB)','definition':'TB registration dates select one row per TB episode','facility':site,'deviations':TB_DEVIATIONS}}

@app.get('/api/tb/summary')
def tb_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=tb_data(from_date,to_date,facility);daily=Counter(str(r['eventDate']) for r in rows if r['eventDate']);ages=Counter();sex=Counter(r['sex'] or 'Unknown' for r in rows);outcomes=Counter(r['treatmentOutcome'] or 'Unknown' for r in rows)
    for r in rows:
        age=r['age'];ages['Unknown' if age is None else 'Under 5' if age<5 else '5-14' if age<15 else '15-24' if age<25 else '25-49' if age<50 else '50+']+=1
    return {'total':len(rows),'uniquePeople':len({r['personId'] for r in rows if r['personId']}),'monthlyBySex':_monthly_sex(rows),'daily':[{'date':k,'count':daily[k]} for k in sorted(daily)],'sex':[{'label':k,'count':v} for k,v in sorted(sex.items())],'ageBands':[{'label':k,'count':ages[k]} for k in ['Under 5','5-14','15-24','25-49','50+','Unknown']],'outcomes':[{'label':k,'count':v} for k,v in sorted(outcomes.items())],'recorded':{'treatmentStarted':sum(r['treatmentInitiated']=='Yes' for r in rows),'outcomeRecorded':sum(bool(r['treatmentOutcome']) for r in rows)},'facilities':[site]}

@app.get('/api/tb/export')
def tb_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=tb_data(from_date,to_date,facility);headers={'Content-Disposition':f'attachment; filename="tb-{from_date or "all"}-{to_date or "all"}.{format}"','Cache-Control':'no-store'}
    if format=='csv':
        output=StringIO(newline='');writer=csv.writer(output);writer.writerow([label for _,label in TB_FIELDS])
        for row in rows:writer.writerow([_anc_safe_csv(_anc_cell(row,key)) for key,_ in TB_FIELDS])
        return Response(('\ufeff'+output.getvalue()).encode('utf-8'),media_type='text/csv',headers=headers)
    if format=='xlsx':
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font,PatternFill
        from openpyxl.utils import get_column_letter
        book=Workbook(write_only=True);sheet=book.create_sheet('DS-DR TB registrations');sheet.freeze_panes='A2'
        for i in range(len(TB_FIELDS)):sheet.column_dimensions[get_column_letter(i+1)].width=24
        header=[]
        for _,label in TB_FIELDS:
            cell=WriteOnlyCell(sheet,value=label);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='218A39');header.append(cell)
        sheet.append(header)
        for row in rows:
            values=[]
            for key,_ in TB_FIELDS:
                value=_anc_cell(row,key);cell=WriteOnlyCell(sheet,value=value)
                if isinstance(value,str):cell.data_type='s'
                values.append(cell)
            sheet.append(values)
        sheet.auto_filter.ref=f'A1:{get_column_letter(len(TB_FIELDS))}{len(rows)+1}';output=BytesIO();book.save(output)
        return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers=headers)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,LongTable,TableStyle,PageBreak
    from xml.sax.saxutils import escape
    output=BytesIO();styles=getSampleStyleSheet();style=ParagraphStyle('TBcell',fontSize=7,leading=9,wordWrap='CJK');content=[]
    for n,row in enumerate(rows):
        if n:content.append(PageBreak())
        content.append(Paragraph('DS-DR TB Register | '+escape(site['name']),styles['Heading2']));content.append(Paragraph(escape(str(row['eventDate']))+' | TB number '+escape(str(row['tbNumber'] or row['recordId'])),styles['Normal']));content.append(Spacer(1,8))
        pairs=[(Paragraph(escape(label),style),Paragraph(escape(str(_anc_cell(row,key))) or '—',style)) for key,label in TB_FIELDS]
        table=LongTable(pairs,colWidths=[240,550],splitByRow=1,splitInRow=1);table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#eaf5eb')),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#dceadd')),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]));content.append(table)
    if not rows:content=[Paragraph('DS-DR TB Register | No registrations for these dates',styles['Heading2'])]
    doc=SimpleDocTemplate(output,pagesize=landscape(A4),leftMargin=24,rightMargin=24,topMargin=26,bottomMargin=28);doc.build(content)
    return Response(output.getvalue(),media_type='application/pdf',headers=headers)

from datetime import timedelta


DELIVERY_FIELDS=[('organisationUnit', 'Organisation unit name'), ('ancNumber', 'ANC number'), ('fullName', 'Full name'), ('address', 'Address'), ('phoneNumber', 'Phone number'), ('maternalAge', 'Maternal age'), ('admittedFrom', 'Admitted from'), ('gestationalAge', 'Gestational age'), ('antenatalCorticosteroidsDose', 'Dexamethasone dispensed'), ('numberOfAncVisits', 'Number of ANC visits'), ('numberOfIPtpDoses', 'IPTp doses recorded'), ('gravida', 'Gravida'), ('parity', 'Parity'), ('aliveChildren', 'Living children'), ('pastObstreticComplications', 'Past obstretic complications'), ('admissionDateTime', 'Admission date time'), ('presentation', 'Presentation'), ('hivStatusOnAdmission', 'HIV status on admission'), ('tcInLabour', 'Counselling and testing in labour'), ('maternalPmtctArvTaken', 'Maternal PMTCT ARV regimen'), ('viralLoadTestResultInAnc', 'Viral load test result in ANC'), ('syphilisTestResultInAnc', 'Syphilis test result in ANC'), ('maternalSyphilisTreatment', 'Maternal syphilis treatment'), ('onsetDateTime', 'Onset date time'), ('stage1', 'First-stage duration'), ('stage2', 'Second-stage duration'), ('stage3', 'Third-stage duration'), ('totalLabourDuration', 'Total labour duration'), ('deliveryDateTime', 'Delivery date time'), ('bloodLoss', 'Blood loss'), ('perineum', 'Perineum and repair details'), ('placenta', 'Placenta'), ('drugsGiven', 'Drugs given'), ('deliveryMethod', 'Delivery method'), ('abnormalities', 'Abnormalities'), ('complications', 'Complications'), ('deliveredBy', 'Delivered by'), ('assistedBy', 'Assisted by'), ('birthOutcome', 'Birth outcome'), ('infantSex', 'Infant sex'), ('apgarScoreAt5', 'Apgar score at 5 minutes'), ('apgarScoreAt1', 'Apgar score at 1 minute'), ('resuscitation', 'Resuscitation'), ('birthDefects', 'Birth defects'), ('weight', 'Weight'), ('tetanus', 'Tetanus'), ('headCircumference', 'Head circumference'), ('crownToHeal', 'Crown-to-heel length'), ('childProtectedAgainstTetanus', 'Child protected against tetanus'), ('infantPMTCTARVSupplied', 'Infant PMTCT ARV supplied'), ('infantSyphilisTreatment', 'Infant syphilis treatment'), ('infantFeedingMethod', 'Infant feeding method'), ('infantFedHour', 'Breastfeeding flag and recorded birth time'), ('infantTEO', 'Infant TEO'), ('infantVitaminK', 'Infant vitamin k'), ('comments', 'Comments'), ('eventDate', 'Registration date'), ('placeOfBirth', 'Place of birth'), ('postpartumFamilyPlanningGiven', 'Postpartum family planning given'), ('mothersConditionOnDischarge', 'Mothers condition on discharge'), ('infantConditionOnDischarge', 'Infant condition on discharge'), ('infantClassification', 'Infant classification'), ('maternalSex', 'Maternal sex'), ('birthId', 'Birth record ID'), ('birthKind', 'Birth record kind')]
ART_FIELDS=[('organisationUnit', 'Organisation unit name'), ('fullName', 'Full name'), ('age', 'Age'), ('sex', 'Sex'), ('address', 'Address'), ('contactNumber', 'Contact number'), ('eventDate', 'Registration date'), ('dateEnrolled', 'Enrolment date'), ('artNumber', 'ART number'), ('enlargedLymphNode', 'Enlarged lymph node'), ('pallor', 'Pallor'), ('jaundice', 'Jaundice'), ('cyanosis', 'Cyanosis'), ('mentalStatus', 'Mental status'), ('centralNervousSystem', 'Central nervous system'), ('dateOfHivTest', 'Date of HIV test'), ('hivRetest', 'HIV confirmation record'), ('functionalStatus', 'Functional status'), ('visitStatus', 'Visit status'), ('visitType', 'Visit type'), ('regimen', 'Regimen'), ('originalRegimen', 'Original ART regimen'), ('reason', 'Reason'), ('followUpStatus', 'Follow up status'), ('weight', 'Weight'), ('muac', 'MUAC'), ('height', 'Height'), ('artStage', 'ART stage'), ('whoStage', 'WHO stage'), ('cd4Count', 'CD4 count'), ('tbInvestigated', 'TB investigated'), ('tbScreening', 'TB screening'), ('dateLineRegimen', 'First ART start date'), ('viralLoad', 'Viral load'), ('tbStatus', 'TB status'), ('artCohort', 'ART cohort'), ('hpvTestResult', 'HPV test result'), ('viacResult', 'VIAC result'), ('hpvViacTreatment', 'HPV VIAC treatment'), ('tranferInStatus', 'Transfer-in status'), ('clientProfile', 'Client profile'), ('artInitiationCategory', 'ART initiation category'), ('clientConsentStatus', 'Client consent status'), ('orphanStatus', 'Orphan status'), ('refLinkage', 'Ref linkage')]
ART_VISIT_FIELDS=[('artId', 'ART id'), ('visitNumber', 'Visit number'), ('visitStatus', 'Visit status'), ('visitType', 'Visit type'), ('visitDate', 'Visit date'), ('artStage', 'ART stage'), ('weight', 'Weight'), ('height', 'Height'), ('muac', 'MUAC'), ('ctxStatus', 'CTX status'), ('tbScreened', 'TB screened'), ('tbPositiveResult', 'Recorded TB test result'), ('tbInvestigated', 'TB investigated'), ('eligibleForIPT', 'Eligible for IPT'), ('tbTreatment', 'TB treatment'), ('ipt', 'IPT'), ('fluconazole', 'Fluconazole'), ('recordedVisitStatus', 'Recorded visit status'), ('setCd4Drawn', 'CD4 sample drawn'), ('setCd4Result', 'CD4 result'), ('artWhoStage', 'ART WHO stage'), ('functionalStatus', 'Functional status'), ('viralLoad', 'Viral load'), ('creatitineResult', 'Creatinine result'), ('atlResult', 'ATL result (source label)'), ('hpvResult', 'HPV result'), ('cd4Count', 'CD4 count'), ('reason', 'Reason'), ('whoStage', 'WHO stage'), ('tbStatus', 'TB status'), ('cervicalCancerScreening', 'Cervical cancer screening'), ('viacResult', 'VIAC result'), ('tranferInStatus', 'Transfer-in status'), ('clientProfile', 'Client profile'), ('currentRegimen', 'Current regimen'), ('cryptococcalMeningitisStatus', 'Cryptococcal meningitis status'), ('refLinkage', 'Ref linkage'), ('artCurrentStatus', 'ART current status'), ('changeOrSubstitutionReason', 'Change or substitution reason')]
# Delivery and general ART: source-guided cohorts; source-unavailable values remain null.
def _da_name(d):
    return ' '.join(str(d.get(k) or '') for k in ('firstname','lastname')).strip() or None

def _da_latest(rows,key='date',idkey=''):
    return sorted(rows,key=lambda r:(r.get(key) or date.min,str(r.get(idkey) or '')),reverse=True)

def _da_nulls(fields):return {key:None for key,_ in fields}

def _da_duration(start,end):
    if not isinstance(start,datetime) or not isinstance(end,datetime) or end<start:return None
    minutes=int((end-start).total_seconds()//60);return f'{minutes//60} hours {minutes%60} mins'

def _delivery_birth_rows(base,delivery,live,still,demographics):
    """Independent infant dictionary per recorded outcome; never re-use a mutable maternal row."""
    result=[]
    for kind,records in [('live',live),('still',still)]:
        for birth in records:
            row=dict(base);identifier=birth['live_birth_id'] if kind=='live' else birth['still_birth_id']
            baby=demographics.get(birth.get('person_id'),{}) if kind=='live' else {}
            row.update(recordId=delivery['delivery_id']+':'+kind+':'+identifier,birthId=identifier,birthKind=kind,
              birthOutcome='Live birth' if kind=='live' else 'Still birth',infantSex=baby.get('sex') if baby else (str(birth['sex']) if birth.get('sex') is not None else None),
              weight=birth.get('weight'),headCircumference=birth.get('head_circumference'),crownToHeal=birth.get('height'),deliveryMethod=birth.get('delivery_mode'),deliveryDateTime=birth.get('time') or delivery.get('delivery_date_time'))
            if kind=='live':row.update(apgarScoreAt1=birth.get('apgar1'),apgarScoreAt5=birth.get('apgar5'),resuscitation=_flag(birth.get('resuscitation')),infantFeedingMethod=birth.get('feeding_type'),infantFedHour=('YES - '+str(birth.get('time'))) if _flag(birth.get('breast_feed'))=='Yes' and birth.get('time') else _flag(birth.get('breast_feed')))
            else:row['infantClassification']=str(birth['type']) if birth.get('type') is not None else None
            result.append(row)
    return result

def delivery_data(from_date,to_date,facility=None):
    clause,args=_dates('r.date',from_date,to_date);c=None
    try:
        c=connect();site=_facility(c);_check_site(site,facility)
        registrations=_query(c,f'SELECT * FROM report.delivery_register r WHERE {clause} AND r.date IS NOT NULL ORDER BY r.date DESC,r.delivery_id DESC',args)
        if not registrations:return [],site
        ids={r['delivery_id'] for r in registrations};people={r['person_id'] for r in registrations if r['person_id']};pids={r['patient_id'] for r in registrations if r['patient_id']}
        fetch=lambda table,key,values:_anc_fetch(c,table,key,values)
        live=_tb_group(fetch('report.live_birth_report','delivery_id',ids),'delivery_id');still=_tb_group(fetch('report.still_birth_register','delivery_id',ids),'delivery_id')
        babies={b['person_id'] for records in live.values() for b in records if b.get('person_id')};all_people=people|babies
        demographics={r['person_id']:r for r in fetch('report.person_demographic','person_id',all_people)}
        patients={r['patient_id']:r for r in fetch('report.patient_register','patient_id',pids)}
        rawpatients={r['patient_id']:r for r in fetch('consultation.patient','patient_id',pids)}
        deaths=_tb_group(fetch('report.death_register','person_id',all_people),'person_id')
        ancs={r['anc_id']:r for r in fetch('report.anc_register','anc_id',{r['anc_id'] for r in registrations if r['anc_id']})}
        rawancs={r['anc_id']:r for r in fetch('consultation.anc','anc_id',set(ancs))}
        ancvisits=_tb_group(fetch('report.anc_visit_register','anc_id',set(ancs)),'anc_id')
        placenta={r['delivery_id']:r for r in fetch('report.placenta_report','delivery_id',ids)}
        cervices=_tb_group(fetch('report.cervix_register','delivery_id',ids),'delivery_id')
        readings=_tb_group(fetch('report.partogram_reading_register','cervix',{x['cervix_id'] for records in cervices.values() for x in records}),'cervix')
        summaries=_tb_group(fetch('consultation.delivery_summary','delivery_id',ids),'delivery_id')
        exams=_tb_group(fetch('report.doctor_examination_report','delivery_id',ids)+fetch('report.nurse_examination_report','delivery_id',ids),'delivery_id')
        drugs=_tb_group(fetch('report.list_of_drugs_given_during_labour','patient_id',pids),'patient_id')
        dispenses=_tb_group(fetch('report.dispense_register','person_id',all_people),'person_id');dispense_byid={x['dispense_id']:x for records in dispenses.values() for x in records}
        arts=fetch('report.art_register','person_id',people);artbyid={a['art_id']:a for a in arts}
        statuses=_tb_group(fetch('report.art_current_status_register','art_register_id',set(artbyid)),'art_register_id')
        labs=_tb_group(fetch('report.investigation_register','person_id',all_people),'person_id')
        hts=_tb_group(fetch('report.hts_register','person_id',people),'person_id')
        all_patients=fetch('report.patient_register','person_id',all_people);rawpatients.update({p['patient_id']:p for p in fetch('consultation.patient','patient_id',{p['patient_id'] for p in all_patients})});patient_people={p['patient_id']:p['person_id'] for p in all_patients}
        previous_screens=fetch('report.hts_screening_register','patient_id',set(patient_people))
        defects=_tb_group(fetch('report.person_birth_defect_report','person_id',babies),'person_id')
        defectnames={r['birth_defect_id']:r['name'] for r in _query(c,'SELECT * FROM terminology.birth_defect')}
        history_deliveries=fetch('report.delivery_register','person_id',people)
        pregnancies=fetch('report.pregnancy_report','person_id',people);pregperson={x['pregnancy_id']:x['person_id'] for x in pregnancies}
        preg_live=fetch('report.pregnancy_live_birth_report','pregnancy_id',set(pregperson));preg_still=fetch('report.pregnancy_still_birth_report','pregnancy_id',set(pregperson))
        historical_live=fetch('report.live_birth_report','mother_person_id',people);historical_still=fetch('report.still_birth_register','mother_person_id',people)
        def medicines(person,start,end):return [x for x in dispenses[person] if x['date'] and start<=x['date']<=end]
        def medication_names(person,start,end):return {(x['medicine_name'] or x['medicine']) for x in medicines(person,start,end)}
        def syphilis_treatment(person,start,end):
            names=medication_names(person,start,end)
            for code,label in [('BENZATHINE PENICILLINE','Benzathine Penicillin'),('PROCAINE BENZYLPENICILLIN','Procaine Penicillin'),('DOXYCYCLINE','Doxycycline'),('ERYTHROMYCIN','Erythromycin')]:
                if code in names:return label
            return 'NA'
        rows=[]
        def lab(person,kind,start,end):
            values=_tb_sorted([i for i in labs[person] if i['investigation_type']==kind and i['date'] and (start is None or i['date']>=start) and i['date']<=end],'date','laboratory_investigation_id')
            return values[0]['result'] if values else None
        def hiv_prior(person,when):
            if any(a['person_id']==person and a['date_enrolled'] and a['date_enrolled']<when for a in arts):return 'POSITIVE'
            if any(patient_people.get(x['patient_id'])==person and x['result']=='POSITIVE' and x['date_last_tested'] and x['date_last_tested']<=when for x in previous_screens):return 'POSITIVE'
            tests=_tb_sorted([x for x in labs[person] if x['investigation_type'] in ('HIV','HIV_SYPHILIS_DUO','HIV_DNA_PCR') and x['date'] and x['date']<when and x['result'] is not None],'date','laboratory_investigation_id')
            return tests[0]['result'] if tests else 'UNKNOWN'
        for r in registrations:
            person=r['person_id'];when=r['date'];patient=patients.get(r['patient_id']);d=demographics.get(person)
            if not patient or not d or not (live[r['delivery_id']] or still[r['delivery_id']]):continue
            age,age_display=_age_fields(d['birthdate'],patient['date']);base=_da_nulls(DELIVERY_FIELDS)
            base.update(personId=person,deliveryId=r['delivery_id'],organisationUnit=site['name'],eventDate=when,maternalAge=age,age=age,ageDisplay=age_display,maternalSex=d['sex'],fullName=_da_name(d),address=d['address'],phoneNumber=d['phone_number'],admissionDateTime=patient['admission_time'],
                onsetDateTime=r['contraction_time'],deliveryDateTime=r['delivery_date_time'],deliveryMethod=r['delivery_method'],placeOfBirth=r['referral_type'],presentation=r['presentation'],perineum=r['perineum'],bloodLoss=r['blood_loss'],comments=r['comments'],deliveredBy=r['deliverer'],assistedBy=r['assisted_by'],
                gravida=r['gravida'],parity=r['parity'],aliveChildren=r['alive_children'],pastObstreticComplications=r['past_obstretic_complications'],hivStatusOnAdmission=hiv_prior(person,when),placenta=placenta.get(r['delivery_id'],{}).get('state'))
            current_preg={x['pregnancy_id'] for x in pregnancies if x['person_id']==person}
            base['gravida']=len(current_preg)+len({x['delivery_id'] for x in history_deliveries if x['person_id']==person and x['anc_id'] not in current_preg})
            base['parity']=sum(x['mother_person_id']==person for x in historical_still)+sum(pregperson.get(x['pregnancy_id'])==person for x in preg_live)+sum(pregperson.get(x['pregnancy_id'])==person for x in preg_still)
            base['aliveChildren']=sum(x['mother_person_id']==person for x in historical_live)+sum(pregperson.get(x['pregnancy_id'])==person for x in preg_live)
            histories=[];previous_births=[x for x in preg_live if pregperson.get(x['pregnancy_id'])==person]
            if any(x.get('weight') is not None and x['weight']<2500 for x in previous_births):histories.append('Birth Weight last baby < 2500g')
            if any(x.get('weight') is not None and x['weight']>4500 for x in previous_births):histories.append('Birth Weight last baby > 4500g')
            if any(x['mother_person_id']==person for x in historical_still) or any(pregperson.get(x['pregnancy_id'])==person for x in preg_still):histories.append('Still born or new born loss')
            if any(x['person_id']==person and x.get('outcome')=='MISCARRIED' for x in pregnancies):histories.append('History of miscarriages')
            birth_pregnancies=[x for x in pregnancies if x['person_id']==person and any(b['pregnancy_id']==x['pregnancy_id'] for b in previous_births)]
            old_ages=[_age_fields(d['birthdate'],x['date'])[0] for x in birth_pregnancies if x.get('date')]
            if any(x is not None and x<16 for x in old_ages):histories.append('Age < 16 years')
            if any(x is not None and x>40 for x in old_ages):histories.append('Age > 40 years')
            base['pastObstreticComplications']='; '.join(histories) if histories else 'NIL'
            perineum=r['perineum'] or ''
            if perineum=='TEAR' and r['degree']:perineum+=' -Degree: '+r['degree']
            if r['perinium_method_of_repair']:perineum+=' -Repair Method: '+r['perinium_method_of_repair']
            if r['perinium_suture_type']:perineum+=' -Suture Type: '+r['perinium_suture_type']
            base['perineum']=perineum or None
            names=medication_names(person,when,when)
            base['antenatalCorticosteroidsDose']='YES' if 'DEXAMETHASONE' in medication_names(person,patient['date'],patient['date']) else 'NO'
            for code in ['CONTROL PILL (NORGESTREL AND ETHINYLESTRADIOL WITH FERROUS FUMARATE)','DEPO-PROVERA (MEDROXYPROGESTERONE)','LEVONORGESTREL','JADELLE (LENOVORGESTREL)']:
                if code in names:base['postpartumFamilyPlanningGiven']=code;break
            if base['postpartumFamilyPlanningGiven'] is None:base['postpartumFamilyPlanningGiven']='NO'
            # PPH is based on an explicit summary value; absent summaries stay unknown.
            summary=summaries[r['delivery_id']]
            if any(x['pph']=='YES' for x in summary):base['complications']='PPH'
            elif summary and all(x['pph']=='NO' for x in summary):base['complications']='NIL'
            if exams[r['delivery_id']]:base['abnormalities']='Breech' if any(x['presentation']=='BREECH' for x in exams[r['delivery_id']]) else None
            raw=rawpatients.get(r['patient_id'],{});discharge=raw.get('discharge_time')
            if discharge:base['mothersConditionOnDischarge']='Deceased' if any(x['date_of_death'] and when<=x['date_of_death']<=_day(discharge) for x in deaths[person]) else 'Stable'
            elif raw and raw.get('discharged') is None:base['mothersConditionOnDischarge']='Still Admitted'
            anc=ancs.get(r['anc_id']);lnmp=anc.get('lnmp') if anc else None
            if anc:
                base.update(ancNumber=anc['anc_number'],numberOfAncVisits=len(ancvisits[anc['anc_id']]),admittedFrom=rawancs.get(anc['anc_id'],{}).get('facility'))
                if lnmp and r['time'] and _day(r['time'])>=lnmp:base['gestationalAge']=str((_day(r['time'])-lnmp).days//7)
                if lnmp:
                    base['viralLoadTestResultInAnc']=lab(person,'VIRAL_LOAD',lnmp,when);base['syphilisTestResultInAnc']=lab(person,'SYPHILIS_TEST',lnmp,when)
                    interval=medication_names(person,lnmp,when);base['maternalSyphilisTreatment']=syphilis_treatment(person,lnmp,when);base['numberOfIPtpDoses']=sum((x['medicine_name'] or x['medicine'])=='SULPHADOXINE(Pyrimethamine)' for x in medicines(person,lnmp,when));base['tetanus']='YES' if 'TETANUS TOXOID' in interval else 'NO'
            personstatuses=[st for a in arts if a['person_id']==person for st in statuses[a['art_id']] if st['date']<=when]
            if personstatuses:base['maternalPmtctArvTaken']=_da_latest(personstatuses,'date','art_status_id')[0]['regimen']
            labourhts=_tb_sorted([x for x in hts[person] if x['patient_id']==r['patient_id'] and x['test_purpose']=='L/D'],'date','hts_id')
            if labourhts:
                test=labourhts[0]
                if _flag(test['post_test_counselling'])=='Yes' and _flag(test['results_issued'])=='Yes':base['tcInLabour']='Yes ('+str(test['result'] or 'result not recorded')+')'
                elif test['post_test_counselling'] is not None and test['results_issued'] is not None:base['tcInLabour']='No'
            listed=[]
            for entry in sorted(drugs[r['patient_id']],key=lambda x:(x['time'] or datetime.min,x['report_id'])):
                drug=dispense_byid.get(entry['dispense_id'])
                if drug:listed.append(' '.join(str(x) for x in [drug['medicine_name'] or drug['medicine'],entry['time']] if x))
            if listed:base['drugsGiven']='; '.join(listed)
            newrows=_delivery_birth_rows(base,r,_tb_sorted(live[r['delivery_id']],'date','live_birth_id'),sorted(still[r['delivery_id']],key=lambda x:x['still_birth_id']),demographics)
            cervix=sorted([x for x in cervices[r['delivery_id']] if x['dilatation']==10 and x['time']],key=lambda x:(x['time'],x['cervix_id']),reverse=True)
            transition=None
            if cervix:
                found=sorted([x for x in readings[cervix[0]['cervix_id']] if x['time']],key=lambda x:(x['time'],x['reading_id']),reverse=True)
                if found:transition=found[0]['time']
            for out in newrows:
                start=r['contraction_time'];birth=out['deliveryDateTime'];placentatime=placenta.get(r['delivery_id'],{}).get('time')
                out.update(stage1=_da_duration(start,transition),stage2=_da_duration(transition,birth),stage3=_da_duration(birth,placentatime))
                if all(out[x] is not None for x in ('stage1','stage2','stage3')):out['totalLabourDuration']=_da_duration(start,placentatime)
                child=next((b.get('person_id') for b in live[r['delivery_id']] if b['live_birth_id']==out['birthId']),None)
                if child:
                    given=medication_names(child,when,when)
                    out.update(infantPMTCTARVSupplied='NVP' if 'NEVIRAPINE' in given else 'AZT' if 'ZIDOVUDINE' in given else 'NA',infantSyphilisTreatment=syphilis_treatment(child,when,when),infantTEO='YES' if 'TETRACYCLINE' in given else 'NO',infantVitaminK='YES' if 'VITAMIN K' in given else 'NO',infantClassification='YES' if defects[child] else 'NO')
                    names=[defectnames[x['birth_defect_id']] for x in defects[child] if x['birth_defect_id'] in defectnames]
                    out['birthDefects']='; '.join(sorted(set(names))) if names else 'NO'
                    child_patients=sorted([p for p in all_patients if p['person_id']==child and p['date']==when],key=lambda p:(p['admission_time'] or datetime.min,p['patient_id']))
                    if child_patients:
                        rawchild=rawpatients.get(child_patients[0]['patient_id'],{});child_discharge=rawchild.get('discharge_time')
                        if child_discharge:out['infantConditionOnDischarge']='Deceased' if any(x['date_of_death'] and when<=x['date_of_death']<=_day(child_discharge) for x in deaths[child]) else 'Stable'
                        elif rawchild and rawchild.get('discharged') is None:out['infantConditionOnDischarge']='Still Admitted'
                elif out['birthKind']=='still':out['birthDefects']=r['abnormalities']
                rows.append(out)
        return rows,site
    except mysql.connector.Error as error:raise HTTPException(503,f'Delivery source unavailable (database error {error.errno}). Check read permissions.')
    finally:
        if c is not None:c.close()


def _art_eligible_visit(visit,patient,start):
    if patient is None:return False
    if patient.get('back_captured') is not None and patient.get('date_approved') is None:return False
    actual=patient.get('date')
    return start is None or (actual is not None and actual>=start)


def art_data(from_date,to_date,facility=None):
    clause,args=_dates('a.date',from_date,to_date);c=None
    try:
        c=connect();site=_facility(c);_check_site(site,facility)
        registrations=_query(c,f'SELECT * FROM report.art_register a WHERE {clause} ORDER BY a.date DESC,a.art_id DESC',args)
        if not registrations:return [],site
        ids={r['art_id'] for r in registrations};people={r['person_id'] for r in registrations if r['person_id']};fetch=lambda table,key,values:_anc_fetch(c,table,key,values)
        visits=_tb_group(fetch('report.art_visit_register','art_id',ids),'art_id');statuses=_tb_group(fetch('report.art_current_status_register','art_register_id',ids),'art_register_id')
        demographics={r['person_id']:r for r in fetch('report.person_demographic','person_id',people)}
        patients=fetch('report.patient_register','person_id',people);patientbyid={p['patient_id']:p for p in patients};patientbyperson=_tb_group(patients,'person_id')
        transfers=_tb_group(fetch('report.art_transfer_in_register','art_id',ids),'art_id');confirm=_tb_group(fetch('report.hts_confirm_diagnosis_register','person_id',people),'person_id')
        linkage=_tb_group(fetch('consultation.art_linkage_from','art_id',ids),'art_id');ipt=_tb_group(fetch('report.art_ipt_status_register','art_register_id',ids),'art_register_id')
        stages=_tb_group(fetch('report.art_who_stage_register','art_register_id',ids),'art_register_id')
        profile=_tb_group(fetch('report.client_profile_register','person_id',people),'person_id');tpt=_tb_group(fetch('report.tpt_screening_report','patient_register_id',set(patientbyid)),'patient_register_id')
        labs=_tb_group(fetch('report.investigation_register','person_id',people),'person_id');dispenses=_tb_group(fetch('report.dispense_register','person_id',people),'person_id')
        screening=_tb_group(fetch('report.tb_screening_register','patient_id',set(patientbyid)),'patient_id')
        special=_query(c,'SELECT * FROM terminology.special_investigation');labids=_tb_group(special,'investigation_type')
        rows=[]
        for r in registrations:
            person=r['person_id'];d=demographics.get(person,{});out=_da_nulls(ART_FIELDS);out.update(_base_row({'recordId':r['art_id'],'personId':person,'eventDate':r['date'],'sex':d.get('sex'),'birthdate':d.get('birthdate')},site))
            out.update(fullName=_da_name(d),address=d.get('address'),contactNumber=d.get('phone_number'),artNumber=r['art_number'],dateEnrolled=r['date_enrolled'],artCohort=r['art_cohort_number'],dateOfHivTest=r['date_of_hiv_test'],mentalStatus=r['mental_status'],centralNervousSystem=r['central_nervous_system'],artInitiationCategory=r['art_initiation_category'],clientConsentStatus=_flag(r['consent_to_personal_tracing']))
            for key,column in [('pallor','pallor'),('jaundice','jaundice'),('cyanosis','cyanosis'),('enlargedLymphNode','enlarged_lymph_node')]:out[key]=_flag(r[column])
            # Source query is existence of a confirmation record, not evidence of a particular result.
            out['hivRetest']='Yes' if confirm[person] else 'No'
            trans=_tb_sorted(transfers[r['art_id']],'date_of_transfer_in','id')
            out['tranferInStatus']='Yes '+str(trans[0]['date_of_transfer_in']) if trans and trans[0]['date_of_transfer_in'] else 'No'
            link=sorted(linkage[r['art_id']],key=lambda x:x['art_linkage_from_id'])
            if link:out['refLinkage']=link[0]['linkage_from']
            starts=sorted([x for x in statuses[r['art_id']] if x['state']=='START_ARV'],key=lambda x:(x['date'] or date.min,x['art_status_id']))
            start=starts[0] if starts else None;startdate=start['date'] if start else None
            if start:out.update(originalRegimen=start['regimen'],reason=start['reason'],dateLineRegimen=start['date'])
            orphan=_tb_sorted([x for x in visits[r['art_id']] if x['orphan_status']],'date','art_visit_id')
            if orphan:out['orphanStatus']=orphan[0]['orphan_status']
            eligible=[]
            for v in sorted(visits[r['art_id']],key=lambda x:(x['date'],x['art_visit_id'])):
                patient=patientbyid.get(v['patient_id'])
                if not _art_eligible_visit(v,patient,startdate):continue
                vi=_da_nulls(ART_VISIT_FIELDS);vi.update(artId=r['art_id'],recordId=v['art_visit_id'],visitDate=patient['date'],visitStatus=v['visit_status'],visitType=v['visit_type'],functionalStatus=v['functional_status'],recordedVisitStatus=v['visit_status'],weight=patient['weight'],height=patient['height'],muac=patient['muac'],tbStatus=v['tb_status'])
                when=v['date'];current=_tb_sorted([x for x in statuses[r['art_id']] if x['date'] and x['date']<=when],'date','art_status_id')
                if current:vi.update(currentRegimen=current[0]['regimen'],artCurrentStatus=current[0]['state'],changeOrSubstitutionReason=current[0]['reason'])
                whostages=_tb_sorted([x for x in stages[r['art_id']] if x['date'] and x['date']<=when],'date','art_stage_id')
                if whostages:vi['whoStage']=whostages[0].get('stage')
                medicines=[x for x in dispenses[person] if x['date']==patient['date']]
                vi['ctxStatus']='Yes' if any(x['medicine_name']=='COTRIMOXAZOLE (SULFAMETHOXAZOLE AND TRIMETHOPRIM)' for x in medicines) else 'No'
                if any(x['medicine_name']=='FLUCONAZOLE' for x in medicines):vi['fluconazole']='Dispensed'
                if screening[v['patient_id']]:
                    status=_flag(_tb_sorted(screening[v['patient_id']],'date','patient_tb_screening_id')[0]['presumptive']);vi['tbScreened']='Presumptive' if status=='Yes' else 'Not presumptive' if status=='No' else None
                tblabs=_tb_sorted([x for x in labs[person] if x['date']==when and x['investigation_type'] in ('XPERT_RIF','HISTOLOGY','CULTURE','DSM','TB')],'date','laboratory_investigation_id')
                if tblabs:vi.update(tbInvestigated='Yes',tbPositiveResult=tblabs[0]['result'])
                validipt=_tb_sorted([x for x in ipt[r['art_id']] if x['date'] and x['date']<=when],'date','art_ipt_status_id')
                if validipt:vi.update(ipt=validipt[0]['status'],reason=validipt[0]['reason'])
                profiles=_da_latest([x for x in profile[person] if _day(x['date'])<=when],'date','patient_client_profile_id')
                if profiles:vi['clientProfile']=profiles[0]['client_profile']
                if tpt[v['patient_id']]:vi['eligibleForIPT']=_flag(_tb_sorted(tpt[v['patient_id']],'date','patient_tpt_screening_id')[0]['eligible'])
                admission=patient['admission_time'] or datetime.combine(patient['date'],datetime.min.time())
                later=sorted([x for x in patientbyperson[person] if x['admission_time'] and x['admission_time']>admission],key=lambda x:(x['admission_time'],x['patient_id']))
                until=_day(later[0]['admission_time']-timedelta(days=1)) if later else date.today()+timedelta(days=1)
                def window(kind):
                    identifiers={x['investigation_id'] for x in labids[kind]}
                    tests=sorted([x for x in labs[person] if x['investigation_id'] in identifiers and x['date'] and when<=x['date']<=until and x['result']],key=lambda x:(x['date'],x['laboratory_investigation_id']))
                    return '; '.join(str(x['result']) for x in tests) or None
                vi['cd4Count']=window('CD4_PERCENTAGE') or window('CD4_COUNT');vi['viralLoad']=window('VIRAL_LOAD');vi['creatitineResult']=window('CREATININE');vi['atlResult']=window('ATL');vi['hpvResult']=window('HPV_DNA')
                pap=window('PAP_SMEAR')
                if pap:vi.update(cervicalCancerScreening='Screened',viacResult=pap)
                elif d.get('sex')=='FEMALE':vi['cervicalCancerScreening']=window('VIAC')
                eligible.append(vi)
            out['visits']=eligible;out['recordedStartDate']=startdate;rows.append(out)
        return rows,site
    except mysql.connector.Error as error:raise HTTPException(503,f'ART source unavailable (database error {error.errno}). Check read permissions.')
    finally:
        if c is not None:c.close()

# Dedicated endpoint helpers retain the existing six register routes unchanged.
def _da_page(rows,site,page,page_size,kind):
    offset=(page-1)*page_size
    return {'data':[{k:v for k,v in r.items() if k!='personId'} for r in rows[offset:offset+page_size]],'pagination':{'page':page,'pageSize':page_size,'total':len(rows),'totalPages':(len(rows)+page_size-1)//page_size},'meta':{'facility':site,'source':kind,'definition':'Birth outcomes selected by delivery registration dates' if kind=='DeliveryRegisterReportSource' else 'ART registration dates; approved linked visits on/after first START_ARV'}}

@app.get('/api/delivery')
def delivery_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,site=delivery_data(from_date,to_date,facility);return _da_page(rows,site,page,page_size,'DeliveryRegisterReportSource')

@app.get('/api/art')
def art_register(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None,page: int=Query(1,ge=1,le=1000000),page_size: int=Query(100,alias='pageSize',ge=1,le=100)):
    rows,site=art_data(from_date,to_date,facility);return _da_page(rows,site,page,page_size,'ArtRegisterReportSource')

def _da_summary(rows,site,kind):
    daily=Counter(str(r['eventDate']) for r in rows if r['eventDate']);ages=Counter()
    for r in rows:
        age=r['age'];ages['Unknown' if age is None else 'Under 5' if age<5 else '5-14' if age<15 else '15-24' if age<25 else '25-49' if age<50 else '50+']+=1
    result={'total':len(rows),'uniquePeople':len({r['personId'] for r in rows if r['personId']}),'monthlyBySex':_monthly_sex(rows),'daily':[{'date':k,'count':daily[k]} for k in sorted(daily)],'ageBands':[{'label':k,'count':ages[k]} for k in ['Under 5','5-14','15-24','25-49','50+','Unknown']],'facilities':[site]}
    if kind=='delivery':
        result.update(deliveries=len({r['deliveryId'] for r in rows}),outcomes=[{'label':k,'count':v} for k,v in sorted(Counter(r['birthOutcome'] or 'Unknown' for r in rows).items())],sex=[{'label':k,'count':v} for k,v in sorted(Counter(r['infantSex'] or 'Unknown' for r in rows).items())],recorded={'liveBirths':sum(r['birthKind']=='live' for r in rows),'stillBirths':sum(r['birthKind']=='still' for r in rows)})
    else:result.update(sex=[{'label':k,'count':v} for k,v in sorted(Counter(r['sex'] or 'Unknown' for r in rows).items())],categories=[{'label':k,'count':v} for k,v in sorted(Counter(r['artInitiationCategory'] or 'Unknown' for r in rows).items())],recorded={'linkedVisits':sum(len(r['visits']) for r in rows),'cohortWithStart':sum(bool(r['recordedStartDate']) for r in rows)})
    return result

@app.get('/api/delivery/summary')
def delivery_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=delivery_data(from_date,to_date,facility);return _da_summary(rows,site,'delivery')

@app.get('/api/art/summary')
def art_summary(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):
    rows,site=art_data(from_date,to_date,facility);return _da_summary(rows,site,'art')

def _da_export(kind,format,from_date,to_date,facility):
    rows,site=(delivery_data if kind=='delivery' else art_data)(from_date,to_date,facility)
    fields=DELIVERY_FIELDS if kind=='delivery' else ART_FIELDS
    title='Delivery Register' if kind=='delivery' else 'ART Register'
    headers={'Content-Disposition':f'attachment; filename="{kind}-{from_date or "all"}-{to_date or "all"}.{format}"','Cache-Control':'no-store'}
    def value(row,key):return cell_value(row.get('ageDisplay') if key in ('age','maternalAge') and row.get('ageDisplay') else row.get(key))
    if format=='csv':
        output=StringIO(newline='');writer=csv.writer(output)
        labels=['Record ID']+[label for _,label in fields]+(['Visit ID']+['Visit: '+label for _,label in ART_VISIT_FIELDS] if kind=='art' else [])
        writer.writerow(labels)
        for r in rows:
            for v in r['visits'] or [{}] if kind=='art' else [{}]:
                record=[r['recordId']]+[value(r,key) for key,_ in fields]
                if kind=='art':record+=[v.get('recordId','')]+[value(v,key) for key,_ in ART_VISIT_FIELDS]
                writer.writerow([_anc_safe_csv(x) for x in record])
        return Response(('\ufeff'+output.getvalue()).encode(),media_type='text/csv',headers=headers)
    if format=='xlsx':
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font,PatternFill
        from openpyxl.utils import get_column_letter
        book=Workbook(write_only=True)
        def sheet(name,labels,records):
            tab=book.create_sheet(name);tab.freeze_panes='A2'
            for i in range(len(labels)):tab.column_dimensions[get_column_letter(i+1)].width=24
            cells=[]
            for label in labels:
                cell=WriteOnlyCell(tab,value=label);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='218A39');cells.append(cell)
            tab.append(cells);length=1
            for row in records:
                cells=[]
                for val in row:
                    cell=WriteOnlyCell(tab,value=val)
                    if isinstance(val,str):cell.data_type='s'
                    cells.append(cell)
                tab.append(cells);length+=1
            tab.auto_filter.ref=f'A1:{get_column_letter(len(labels))}{length}'
        sheet('Delivery birth outcomes' if kind=='delivery' else 'ART registrations',['Record ID']+[label for _,label in fields],([r['recordId']]+[value(r,key) for key,_ in fields] for r in rows))
        if kind=='art':sheet('ART visits',['Registration ID','Visit ID']+[label for _,label in ART_VISIT_FIELDS],([r['recordId'],v['recordId']]+[value(v,key) for key,_ in ART_VISIT_FIELDS] for r in rows for v in r['visits']))
        output=BytesIO();book.save(output);return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers=headers)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,LongTable,TableStyle,PageBreak
    from xml.sax.saxutils import escape
    output=BytesIO();styles=getSampleStyleSheet();style=ParagraphStyle('DeliveryARTcell',fontSize=7,leading=9,wordWrap='CJK');content=[]
    def section(record,config):
        pairs=[[Paragraph(escape(label),style),Paragraph(escape(str(value(record,key))) or '—',style)] for key,label in config]
        table=LongTable(pairs,colWidths=[240,550],splitByRow=1,splitInRow=1);table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(0,-1),colors.HexColor('#eaf5eb')),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#dceadd')),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]));content.append(table)
    for n,row in enumerate(rows):
        if n:content.append(PageBreak())
        content.append(Paragraph(title+' | '+escape(site['name']),styles['Heading2']));content.append(Paragraph(escape(str(row['eventDate']))+' | '+escape(str(row['recordId'])),styles['Normal']));content.append(Spacer(1,8));section(row,fields)
        if kind=='art':
            for visit in row['visits']:
                content.append(PageBreak());content.append(Paragraph('ART linked visit | '+escape(str(visit['visitDate'])),styles['Heading2']));content.append(Paragraph('Registration: '+escape(str(row['recordId'])),styles['Normal']));section(visit,ART_VISIT_FIELDS)
    if not rows:content=[Paragraph(title+' | No records for these dates',styles['Heading2'])]
    def white_page(canvas,document):
        canvas.saveState();canvas.setFillColor(colors.white);canvas.rect(0,0,*landscape(A4),fill=1,stroke=0);canvas.restoreState()
    doc=SimpleDocTemplate(output,pagesize=landscape(A4),leftMargin=24,rightMargin=24,topMargin=26,bottomMargin=28);doc.build(content,onFirstPage=white_page,onLaterPages=white_page);return Response(output.getvalue(),media_type='application/pdf',headers=headers)

@app.get('/api/delivery/export')
def delivery_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):return _da_export('delivery',format,from_date,to_date,facility)

@app.get('/api/art/export')
def art_export(format: Literal['csv','xlsx','pdf'],from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),facility: str | None=None):return _da_export('art',format,from_date,to_date,facility)


# ---------------------------------------------------------------- Facility dashboard
# Real indicators for the facility, built from the same register logic the register pages use,
# so dashboard totals match the registers for the same period.
def _month_start(d,back=0):
    index=d.year*12+d.month-1-back
    return date(index//12,index%12+1,1)


def _monthly(daily,months):
    counts=Counter()
    for item in daily:counts[item['date'][:7]]+=item['count']
    return [counts.get(m,0) for m in months]


def _count(items,*labels):
    want={l.upper() for l in labels}
    return sum(i['count'] for i in items if (i['label'] or '').upper() in want)


# Five-year age groups (65+ last) for the population pyramid; ages are completed years at the visit.
AGE_BANDS=[(f'{a}-{a+4}',a,a+4) for a in range(0,65,5)]+[('65+',65,None)]
def _sex_age(rows,keep=None):
    sex=Counter();pyr={b[0]:{'band':b[0],'male':0,'female':0} for b in AGE_BANDS};unknown_age=0
    for r in rows:
        if keep and not keep(r):continue
        sx=(r.get('sex') or '').upper();k='male' if sx=='MALE' else 'female' if sx=='FEMALE' else 'other';sex[k]+=1
        age=r.get('age')
        if age is None:unknown_age+=1;continue
        if k=='other':continue
        band=next(b[0] for b in AGE_BANDS if b[2] is None or age<=b[2])
        pyr[band][k]+=1
    return {'sex':{'male':sex['male'],'female':sex['female'],'other':sex['other']},'pyramid':list(pyr.values()),'ageUnknown':unknown_age}


def _indicators(from_date,to_date):
    """Indicators from the clinical data loaded in this database (today: Avondale only)."""
    c=None
    try:
        c=connect()
        diagnoses=_query(c,"""SELECT diagnosis_id AS code,MAX(diagnosis) AS name,COUNT(*) AS count
          FROM report.patient_diagnosis_report WHERE date BETWEEN %s AND %s AND NULLIF(TRIM(diagnosis),'') IS NOT NULL
          GROUP BY diagnosis_id ORDER BY count DESC LIMIT 10""",(from_date,to_date))
    except mysql.connector.Error:raise HTTPException(503,'Dashboard data unavailable. Check read access.')
    finally:
        if c is not None:c.close()
    att=corrected_summary('attendance',from_date,to_date,None)
    opd=corrected_summary('opd-general',from_date,to_date,None)
    hts=corrected_summary('hts',from_date,to_date,None)
    mal=corrected_summary('malaria',from_date,to_date,None)
    anc=anc_summary(from_date,to_date,None)
    tb=tb_summary(from_date,to_date,None)
    art=art_summary(from_date,to_date,None)
    dlv=delivery_summary(from_date,to_date,None)
    months=[];m=date(from_date.year,from_date.month,1)
    while m<=to_date:
        months.append(m.strftime('%Y-%m'));m=_month_start(m,-1)
    hts_pos=_count(hts['results'],'POSITIVE');hts_neg=_count(hts['results'],'NEGATIVE','HIV AND SYPHILIS NEGATIVE','HIV NEGATIVE AND SYPHILIS POSITIVE')
    mal_pos=_count(mal['results'],'POSITIVE');mal_neg=_count(mal['results'],'NEGATIVE')
    rate=lambda pos,neg:round(pos/(pos+neg)*100,1) if pos+neg else None
    total=att['total'];pct=lambda n:round(n/total*100,1) if total else None
    # Monthly series for every indicator the map can show. Positives come from the register rows.
    hts_rows,_=corrected_data('hts',from_date,to_date,None);mal_rows,_=corrected_data('malaria',from_date,to_date,None)
    # Register rows for the sex and age breakdowns of each map indicator.
    att_rows,_=corrected_data('attendance',from_date,to_date,None);opd_rows,_=corrected_data('opd-general',from_date,to_date,None)
    anc_rows,_=anc_data(from_date,to_date,None);art_rows,_=art_data(from_date,to_date,None);tb_rows,_=tb_data(from_date,to_date,None)
    positive=lambda field:lambda r:(r.get(field) or '').upper()=='POSITIVE'
    def monthly_rows(rows,field):
        c=Counter(str(r['eventDate'])[:7] for r in rows if r['eventDate'] and positive(field)(r))
        return [c.get(m,0) for m in months]
    series={}
    def add(key,label,monthly,rows,keep=None):
        series[key]={'key':key,'label':label,'monthly':monthly,'total':sum(monthly),**_sex_age(rows,keep)}
    add('opd','OPD General visits',_monthly(opd['daily'],months),opd_rows)
    add('attendances','Attendances',_monthly(att['daily'],months),att_rows)
    add('htsTests','HIV tests (HTS)',_monthly(hts['daily'],months),hts_rows)
    add('hivPositive','HIV positive cases',monthly_rows(hts_rows,'testResult'),hts_rows,positive('testResult'))
    add('ancBookings','ANC registrations',_monthly(anc['daily'],months),anc_rows)
    add('artNew','New on ART',_monthly(art['daily'],months),art_rows)
    add('tbRegistered','TB registrations',_monthly(tb['daily'],months),tb_rows)
    add('malariaTests','Malaria tests',_monthly(mal['daily'],months),mal_rows)
    add('malariaPositive','Malaria positive cases',monthly_rows(mal_rows,'investigationResult'),mal_rows,positive('investigationResult'))
    return {
      'series':series,
      'kpis':{'attendances':total,'uniquePatients':att['uniquePeople'],'opdVisits':opd['total'],
        'htsTests':hts['total'],'htsPositive':hts_pos,'htsPositivity':rate(hts_pos,hts_neg),
        'malariaTests':mal['total'],'malariaPositive':mal_pos,'malariaPositivity':rate(mal_pos,mal_neg),
        'ancBookings':anc['total'],'ancFirstBookings':anc['recorded'].get('firstBookings'),
        'artNew':art['total'],'tbRegistered':tb['total'],'deliveries':dlv['total']},
      'monthly':{'months':months,'attendance':_monthly(att['daily'],months),'opd':_monthly(opd['daily'],months),
                 'hts':_monthly(hts['daily'],months),'anc':_monthly(anc['daily'],months)},
      'sex':att['sex'],'ageBands':att['ageBands'],
      'topDiagnoses':[{'code':d['code'],'name':d['name'],'count':d['count']} for d in diagnoses],
      'registers':[{'id':'attendance','name':'Attendance','total':total},{'id':'opd','name':'OPD General','total':opd['total']},
                   {'id':'hts','name':'HTS','total':hts['total']},{'id':'anc','name':'ANC','total':anc['total']},
                   {'id':'art','name':'ART','total':art['total']},{'id':'tb','name':'TB','total':tb['total']},
                   {'id':'malaria','name':'Malaria','total':mal['total']},{'id':'delivery','name':'Delivery','total':dlv['total']}],
      'dataCompleteness':{'diagnosisRecorded':pct(att['recorded']['diagnosis']),'bloodPressureRecorded':pct(att['recorded']['bloodPressure']),
                          'medicationRecorded':pct(att['recorded']['medication']),'tbScreeningRecorded':pct(att['recorded']['tbScreening'])},
    }


def _breakdown(rows,key,name_key):
    groups={}
    for r in rows:
        g=groups.setdefault(r[key],{'id':r[key],'name':r[name_key] or 'Not recorded','facilities':0,'hospitals':0,'clinics':0,'reporting':0,'withCoordinates':0})
        g['facilities']+=1;g['reporting']+=r['hasData'];g['withCoordinates']+=r['latitude'] is not None
        g['hospitals' if r['typeGroup']=='Hospital' else 'clinics']+=1
    return sorted(groups.values(),key=lambda g:g['name'])


@app.get('/api/dashboard')
def national_dashboard(from_date: date | None=Query(None,alias='from'),to_date: date | None=Query(None,alias='to'),
                       province: str | None=None,district: str | None=None,type: str | None=None,facility: str | None=None):
    """Dashboard for any scope: national, a province, a district, a facility type, or one facility."""
    def load(c):
        geo={'provinces':{p['province_id']:p['province_name'] for p in _query(c,'SELECT * FROM registry.province')},
             'districts':{d['district_id']:d for d in _query(c,'SELECT * FROM registry.district')},
             'types':{t['type_code']:t for t in _query(c,'SELECT * FROM registry.facility_type')}}
        return geo,_registry(c,facility,province,district,type),_facility(c)
    geo,scope_rows,site=_with_registry(load)
    if province and province not in geo['provinces']:raise HTTPException(404,f'Unknown province {province!r}.')
    if district and district not in geo['districts']:raise HTTPException(404,f'Unknown district {district!r}.')
    if district and province and geo['districts'][district]['province_id']!=province:raise HTTPException(400,'That district is not in the selected province.')
    if facility and not scope_rows:raise HTTPException(404,f'Facility {facility!r} is not in this selection.')
    if type and type.upper()!='HOSPITAL' and type.upper() not in geo['types']:raise HTTPException(404,f'Unknown facility type {type!r}.')

    latest=site['latestDate'] or date.today()
    to_date=to_date or latest
    from_date=from_date or _month_start(to_date,11)
    if from_date>to_date:raise HTTPException(400,"'from' must be on or before 'to'.")

    reporting=[r for r in scope_rows if r['hasData']]
    level='facility' if facility else 'district' if district else 'province' if province else 'national'
    parts=['Zimbabwe']
    if facility and scope_rows:
        f=scope_rows[0];parts+=[x for x in (f['province'],f['district'],f['name']) if x]
    else:
        if province:parts.append(geo['provinces'][province])
        if district:parts.append(geo['districts'][district]['district_name'])
    type_label=None
    if type:type_label='Hospitals (all levels)' if type.upper()=='HOSPITAL' else geo['types'][type.upper()]['type_label']
    by_type={}
    for t in sorted(geo['types'].values(),key=lambda t:t['sort_order']):
        by_type[t['type_code']]={'code':t['type_code'],'label':t['type_label'],'group':t['type_group'],'facilities':0,'reporting':0}
    for r in scope_rows:
        if r['type'] in by_type:by_type[r['type']]['facilities']+=1;by_type[r['type']]['reporting']+=r['hasData']

    indicators=_indicators(from_date,to_date) if reporting else None
    # Indicator totals per area (provinces nationally, districts inside a province). An area with
    # no reporting facility gets null, not 0, so the map can show "no data" rather than "none".
    # Single-tenant database: all loaded data belongs to the tenant facility. Multi-tenant: compute
    # the series per reporting facility and sum them here.
    per_facility={TENANT_FACILITY_ID:{k:v['total'] for k,v in indicators['series'].items()}} if indicators else {}
    by_area=None
    if level in ('national','province'):
        key,name='provinceId' if level=='national' else 'districtId','province' if level=='national' else 'district'
        areas={}
        if level=='national':
            for pid,pname in geo['provinces'].items():areas[pid]={'id':pid,'name':pname,'facilities':0,'reporting':[]}
        else:
            for did,d in geo['districts'].items():
                if d['province_id']==province:areas[did]={'id':did,'name':d['district_name'],'facilities':0,'reporting':[]}
        for r in scope_rows:
            a=areas.setdefault(r[key],{'id':r[key],'name':r[name] or 'Not recorded','facilities':0,'reporting':[]})
            a['facilities']+=1
            if r['hasData']:a['reporting'].append(r['id'])
        keys=list(indicators['series']) if indicators else []
        by_area={'level':'province' if level=='national' else 'district','areas':[
            {'id':a['id'],'name':a['name'],'facilities':a['facilities'],'reporting':len(a['reporting']),
             'values':{k:(sum(per_facility.get(f,{}).get(k,0) for f in a['reporting']) if a['reporting'] else None) for k in keys}}
            for a in sorted(areas.values(),key=lambda a:a['name'])]}
    return {
      'scope':{'level':level,'path':parts,'label':' › '.join(parts),'province':province,'district':district,'type':type,'typeLabel':type_label,'facility':facility},
      'period':{'from':from_date,'to':to_date,'latestDataDate':latest,
                'partialLastMonth':to_date==latest and (to_date.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)!=to_date},
      'coverage':{'facilities':len(scope_rows),'hospitals':sum(r['typeGroup']=='Hospital' for r in scope_rows),
                  'clinics':sum(r['typeGroup']=='Clinic' for r in scope_rows),'reporting':len(reporting),
                  'withCoordinates':sum(r['latitude'] is not None for r in scope_rows),
                  'byType':list(by_type.values()),
                  'byProvince':_breakdown(scope_rows,'provinceId','province'),
                  'byDistrict':_breakdown(scope_rows,'districtId','district') if province or district else None},
      'facilities':[{k:r[k] for k in ('id','name','province','district','districtId','type','typeLabel','typeGroup','latitude','longitude','hasData')} for r in scope_rows],
      'reportingFacilities':[{'id':r['id'],'name':r['name'],'province':r['province'],'district':r['district'],'typeLabel':r['typeLabel']} for r in reporting],
      # Single-tenant database: the loaded data belongs to the reporting facilities, so indicators are
      # computed only when at least one of them is in scope. Multi-tenant: filter by their IDs instead.
      'indicators':indicators,
      'byArea':by_area,
      # Indicator totals per reporting facility in scope (the map can shade by facility or by map district).
      'byFacility':[{'id':r['id'],'values':per_facility.get(r['id'],{})} for r in reporting],
      'facility':scope_rows[0] if level=='facility' else None,
    }


# ---------------------------------------------------------------- AI assistant (Ollama)
from ai import router as ai_router  # noqa: E402
app.include_router(ai_router)

# ---------------------------------------------------------------- Geospatial intelligence (map time-lapse)
from geo import router as geo_router  # noqa: E402
app.include_router(geo_router)

# ---------------------------------------------------------------- Custom reports
from reports import router as reports_router  # noqa: E402
app.include_router(reports_router)

# ---------------------------------------------------------------- Data workspace
from workspace import router as workspace_router  # noqa: E402
app.include_router(workspace_router)

# ---------------------------------------------------------------- Permissions: hide patient details at the source
import access  # noqa: E402
access.install(sys.modules[__name__])
