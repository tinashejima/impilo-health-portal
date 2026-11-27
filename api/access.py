"""Who may see what. Checked by the API on every request, so hiding a menu item is never the only guard.

Each account has:
  access       {level: national|province|district|facility, areas: [ids]}  the part of the country they cover
  modules      the portal sections they can open (see MODULES)
  patientData  whether they may see who a patient is (names, ID numbers, phone numbers, addresses, ...)
  downloads    {reports, registers, raw}  which downloads they may make
Administrators can do everything.

Patient details are hidden at the source (the register data functions), so registers, their CSV, Excel
and PDF downloads, and the data workspace all show "•••" instead of identifying details.
"""
import contextvars
import re
from functools import wraps

from fastapi.responses import JSONResponse

MODULES = {
    'overview': 'Overview',
    'intel': 'Data intelligence',
    'geo': 'Geospatial intelligence',
    'registers': 'Registers',
    'reports': 'Reports',
    'facilities': 'Facilities reporting',
    'ask': 'Ask the data',
    'workspace': 'Data workspace',
    'config': 'Configuration',
    'users': 'Users & access',
}
ADMIN_ONLY = {'config', 'users'}
LEVELS = ('national', 'province', 'district', 'facility')
MASK = '•••'

# Fields that identify a patient. Anything else (age, sex, dates, results, diagnoses) stays visible.
SENSITIVE = {
    'fullName', 'firstName', 'lastName', 'firstname', 'lastname', 'surname', 'name_of_patient',
    'idNumber', 'nationalId', 'national_id', 'birthCertificateNumber', 'passportNumber',
    'phoneNumber', 'phone', 'phone_number', 'contactNumber', 'mobile',
    'address', 'nextOfKin', 'nextOfKinAddress', 'nextOfKinFullName', 'nextOfKinPhone', 'nextOfKinPhoneNumber',
    'birthdate', 'dateOfBirth', 'dob', 'personId', 'person_id', 'patientId',
    'ancNumber', 'artNumber', 'htsNumber', 'indexCaseHtsNumber', 'tbNumber', 'rbfNumber', 'opdNumber', 'oiArtNumber',
}

_mask_patients = contextvars.ContextVar('mask_patients', default=False)


def defaults(role):
    """Settings for accounts made before permissions existed: everything (as before), so nobody loses access."""
    return {'access': {'level': 'national', 'areas': []},
            'modules': [m for m in MODULES if m not in ADMIN_ONLY or role == 'admin'],
            'patientData': True, 'downloads': {'reports': True, 'registers': True, 'raw': True}}


def permissions(user):
    """The account's effective permissions (administrators get everything)."""
    if user.get('role') == 'admin':
        return {'access': {'level': 'national', 'areas': []}, 'modules': list(MODULES), 'patientData': True,
                'downloads': {'reports': True, 'registers': True, 'raw': True}}
    d = defaults(user.get('role'))
    access = user.get('access') or d['access']
    level = access.get('level') if access.get('level') in LEVELS else 'national'
    return {'access': {'level': level, 'areas': [str(a) for a in (access.get('areas') or [])] if level != 'national' else []},
            'modules': [m for m in (user['modules'] if 'modules' in user else d['modules']) if m in MODULES and m not in ADMIN_ONLY],
            'patientData': bool(user.get('patientData', d['patientData'])),
            'downloads': {k: bool((user.get('downloads') or d['downloads']).get(k, False)) for k in ('reports', 'registers', 'raw')}}


def validate(level, areas, modules, downloads):
    if level not in LEVELS: raise ValueError('Choose national, provincial, district or facility access.')
    if level != 'national' and not areas: raise ValueError(f'Choose at least one {level} for this account.')
    bad = [m for m in modules if m not in MODULES]
    if bad: raise ValueError(f'Unknown section: {", ".join(bad)}.')
    return True


# ---------------------------------------------------------------- where the data is
def _registry():
    """Province and district of every facility, for checking areas."""
    import main
    from geo import _cached
    def load():
        rows = main._with_registry(lambda c: main._query(c, 'SELECT facility_id, province_id, district_id FROM registry.facility_registry'))
        return {r['facility_id']: (r['province_id'], r['district_id']) for r in rows}
    return _cached(('access-registry',), load)


def covers_facility(perm, facility_id):
    a = perm['access']
    if a['level'] == 'national': return True
    prov, dist = _registry().get(facility_id, (None, None))
    if a['level'] == 'province': return prov in a['areas']
    if a['level'] == 'district': return dist in a['areas']
    return facility_id in a['areas']


def _tenant():
    import main
    return main.TENANT_FACILITY_ID


# ---------------------------------------------------------------- request checks
REGISTERS = ('attendance', 'opd-general', 'hts', 'malaria', 'anc', 'tb', 'delivery', 'art')
_REG_RE = re.compile(r'^/api/(' + '|'.join(REGISTERS) + r')(/summary|/export)?$')


def module_for(path, method):
    if _REG_RE.match(path): return 'registers'
    if path.startswith('/api/dashboard'): return 'overview'
    if path.startswith('/api/geo/'): return 'geo'
    if path.startswith('/api/reports'): return 'reports'
    if path.startswith('/api/workspace'): return 'workspace'
    if path == '/api/ai/chat': return 'ask'
    if path.startswith('/api/ai/'): return 'ask' if method == 'GET' else 'config'
    if path.startswith('/api/auth/users'): return 'users'
    return None  # sign-in, health, facility registry and geography: open to every signed-in user


def download_for(path):
    if _REG_RE.match(path) and path.endswith('/export'): return 'registers'
    if path == '/api/reports/export': return 'reports'
    if path.startswith('/api/workspace/') and path.endswith('/export'): return 'raw'
    return None


def deny(message):
    return JSONResponse({'detail': message}, status_code=403)


def _area_words(perm):
    a = perm['access']
    n = len(a['areas'])
    return {'province': 'province', 'district': 'district', 'facility': 'facility'}[a['level']] + ('s' if n != 1 else '')


def scope_query(request, perm):
    """Keep province/district/facility filters inside the account's area: fill them in when missing and
    refuse ones outside it. Returns an error response, or None when the request may go ahead."""
    a = perm['access']
    if a['level'] == 'national': return None
    from urllib.parse import parse_qsl, urlencode
    q = dict(parse_qsl(request.scope.get('query_string', b'').decode(), keep_blank_values=True))
    reg = _registry()
    # Each filter may list several IDs (comma-separated); every one must be inside the area.
    ids = lambda k: [x.strip() for x in (q.get(k) or '').split(',') if x.strip()]
    provs, dists, facs = ids('province'), ids('district'), ids('facility')
    if a['level'] == 'province':
        if any(reg.get(f, (None,))[0] not in a['areas'] for f in facs): return deny('That facility is outside your area.')
        if any(p not in a['areas'] for p in provs): return deny('That province is outside your area.')
        if any(not any(d == dist and p in a['areas'] for p, d in reg.values()) for dist in dists): return deny('That district is outside your area.')
        if not provs and not dists and not facs: q['province'] = ','.join(a['areas'])
    elif a['level'] == 'district':
        if any(reg.get(f, (None, None))[1] not in a['areas'] for f in facs): return deny('That facility is outside your area.')
        if any(d not in a['areas'] for d in dists): return deny('That district is outside your area.')
        if not dists and not facs:
            q['district'] = ','.join(a['areas'])
            q.pop('province', None)
    else:
        if any(f not in a['areas'] for f in facs): return deny('That facility is outside your area.')
        if not facs:
            q['facility'] = ','.join(a['areas'])
            q.pop('province', None); q.pop('district', None)
    request.scope['query_string'] = urlencode(q).encode()
    return None


def check(request, user):
    """Run before every signed-in API call. Returns an error response or None."""
    path, method = request.url.path, request.method
    perm = permissions(user)
    request.state.perm = perm
    module = module_for(path, method)
    if module:
        if module in ADMIN_ONLY and user.get('role') != 'admin':
            if module == 'config': return deny('Only administrators can change the AI configuration.')
            return deny('Only administrators can manage users.')
        if module not in ADMIN_ONLY and module not in perm['modules']:
            # The chat page also reads the assistant list; allow that for anyone who may chat.
            return deny(f'Your account does not include {MODULES[module]}. Ask an administrator for access.')
    dl = download_for(path)
    if dl and not perm['downloads'][dl]:
        what = {'registers': 'register', 'reports': 'report', 'raw': 'raw data'}[dl]
        return deny(f'Your account is not allowed to download {what} files. Ask an administrator if you need this.')
    if module in ('overview', 'geo') and method == 'GET':
        err = scope_query(request, perm)
        if err: return err
    # Patient-level rows (registers, workspace, AI, reports) only come from facilities with data. If the
    # account's area doesn't include them there is nothing to show; say so rather than leak other areas.
    if module in ('registers', 'workspace', 'reports', 'ask') and not covers_facility(perm, _tenant()):
        if path in ('/api/reports', '/api/reports/meta') or path.startswith('/api/ai/') and method == 'GET' and path != '/api/ai/chat':
            return None
        return deny(f'None of the facilities in your {_area_words(perm)} send data to the portal yet.')
    # Hide who patients are from people without patient-level access.
    _mask_patients.set(module in ('registers', 'workspace') and not perm['patientData'])
    return None


# ---------------------------------------------------------------- masking at the source
def mask_row(row):
    out = {}
    for k, v in row.items():
        if k in SENSITIVE and v not in (None, ''): out[k] = MASK
        elif isinstance(v, list): out[k] = [mask_row(x) if isinstance(x, dict) else x for x in v]
        elif isinstance(v, dict): out[k] = mask_row(v)
        else: out[k] = v
    return out


def masking():
    return _mask_patients.get()


def install(main):
    """Wrap the register data functions so their rows come back masked when the caller may not see patients."""
    for name in ('corrected_data', 'anc_data', 'art_data', 'tb_data', 'delivery_data'):
        original = getattr(main, name)
        if getattr(original, '_masked', False): continue
        @wraps(original)
        def wrapper(*a, __orig=original, **k):
            rows, site = __orig(*a, **k)
            return ([mask_row(r) for r in rows] if _mask_patients.get() else rows), site
        wrapper._masked = True
        setattr(main, name, wrapper)
