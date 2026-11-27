"""AI assistant for the portal, powered by a local Ollama server (http://127.0.0.1:11434).

The portal API proxies Ollama so the browser talks to one origin, and so answers can be grounded in
live portal figures. Only aggregated indicators from the dashboard are sent to the model - never
patient-level records.

Endpoints (all under /api/ai):
  GET  /status                    Ollama reachable?, installed/running models, settings, hardware
  POST /server/start | /server/stop
  POST /models/pull {model}        download in the background; GET /models/pull shows progress
  POST /models/pull/cancel {model}
  DELETE /models/{name}
  POST /models/{name}/load | /unload
  GET|PUT /settings                generation settings ("Optimise"); GET /settings/recommend
  GET|PUT|DELETE /assistants[/{id}], POST /assistants/{id}/build   ("Train": custom Ollama models)
  POST /chat                       streamed answer (NDJSON), optionally grounded in portal data
  POST /chat/cancel {requestId}    stop an answer that is still being written (frees the model)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

OLLAMA = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
DATA_DIR = Path(__file__).with_name('ai-data')
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / 'settings.json'
ASSISTANTS_FILE = DATA_DIR / 'assistants.json'
LOG_DIR = Path(__file__).resolve().parent.parent / 'logs'

router = APIRouter(prefix='/api/ai', tags=['AI assistant'])

# Models offered in the download catalogue (approximate download sizes). Any other Ollama model
# name can also be typed in.
CATALOGUE = [
    {'name': 'llama3.2:1b', 'label': 'Llama 3.2 1B', 'size': '1.3 GB', 'minRamGb': 4, 'note': 'Smallest and fastest; basic answers.'},
    {'name': 'qwen2.5:1.5b', 'label': 'Qwen 2.5 1.5B', 'size': '1.0 GB', 'minRamGb': 4, 'note': 'Very small; good with numbers and tables.'},
    {'name': 'gemma2:2b', 'label': 'Gemma 2 2B', 'size': '1.6 GB', 'minRamGb': 6, 'note': 'Small model with clear writing.'},
    {'name': 'llama3.2:3b', 'label': 'Llama 3.2 3B', 'size': '2.0 GB', 'minRamGb': 8, 'note': 'Good balance for a normal laptop (recommended).'},
    {'name': 'qwen2.5:3b', 'label': 'Qwen 2.5 3B', 'size': '1.9 GB', 'minRamGb': 8, 'note': 'Good balance; strong at reading figures.'},
    {'name': 'phi3.5', 'label': 'Phi 3.5 Mini (3.8B)', 'size': '2.2 GB', 'minRamGb': 8, 'note': 'Good reasoning for its size.'},
    {'name': 'qwen2.5:7b', 'label': 'Qwen 2.5 7B', 'size': '4.7 GB', 'minRamGb': 16, 'note': 'Better answers; needs 16 GB RAM or a GPU.'},
    {'name': 'mistral:7b', 'label': 'Mistral 7B', 'size': '4.1 GB', 'minRamGb': 16, 'note': 'Solid general model; needs 16 GB RAM.'},
    {'name': 'llama3.1:8b', 'label': 'Llama 3.1 8B', 'size': '4.9 GB', 'minRamGb': 16, 'note': 'Best quality here; needs 16 GB RAM or a GPU.'},
]

DEFAULT_SETTINGS = {
    'defaultModel': '',          # used when no assistant is chosen
    'temperature': 0.2,          # low = factual and consistent
    'top_p': 0.9,
    'num_ctx': 4096,             # how much text the model can read at once (question + data context)
    'num_predict': 512,          # maximum answer length in tokens
    'num_thread': None,          # CPU threads; None = Ollama decides
    'num_gpu': None,             # layers on the GPU; None = Ollama decides, 0 = CPU only
    'repeat_penalty': 1.1,
    'keep_alive': '30m',         # how long a model stays in memory after the last question
}

DEFAULT_ASSISTANT = {
    'id': 'impilo-data', 'name': 'Impilo data assistant', 'baseModel': '',
    'domain': "Zimbabwe Ministry of Health and Child Care national health reporting: facility attendance, OPD, HIV testing (HTS), ANC, ART, TB and malaria registers.",
    'instructions': "You help health managers understand the figures in the National Health Portal. Use only the figures in the DATA CONTEXT. If a figure is not there, say you don't have it - never guess or invent numbers. Point out when data is incomplete (for example a partial month, or facilities not yet reporting).",
    'style': 'concise', 'language': 'English', 'examples': [], 'usePortalData': True,
    'modelName': '', 'builtAt': None, 'builtFrom': None,
}

STYLES = {
    'concise': 'Answer in 2-4 short sentences. Lead with the number that answers the question.',
    'detailed': 'Give a short answer first, then explain the figures and what they mean, in a few paragraphs.',
    'bullets': 'Answer as a short list of bullet points, one figure per bullet.',
    'briefing': 'Write like a briefing note for a manager: headline, 3 key points, then a suggested action.',
}


# ------------------------------------------------------------------ storage
def _read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')
    tmp.replace(path)


def settings():
    return {**DEFAULT_SETTINGS, **_read(SETTINGS_FILE, {})}


def assistants():
    items = _read(ASSISTANTS_FILE, None)
    if not items:
        items = [dict(DEFAULT_ASSISTANT)]
        _write(ASSISTANTS_FILE, items)
    return items


# ------------------------------------------------------------------ Ollama helpers
def _ollama(method, path, timeout=10, **kw):
    try:
        r = httpx.request(method, OLLAMA + path, timeout=timeout, **kw)
    except httpx.HTTPError:
        raise HTTPException(503, 'Ollama is not running. Start it on the Models tab (or open the Ollama app).')
    if r.status_code >= 400:
        try:
            msg = r.json().get('error') or r.text
        except ValueError:
            msg = r.text
        raise HTTPException(r.status_code if r.status_code < 500 else 502, f'Ollama: {msg}')
    return r.json() if r.content else {}


def _ollama_up():
    try:
        return httpx.get(OLLAMA + '/api/version', timeout=2).json().get('version')
    except (httpx.HTTPError, ValueError):
        return None


def _options(s):
    keys = ('temperature', 'top_p', 'num_ctx', 'num_predict', 'num_thread', 'num_gpu', 'repeat_penalty')
    return {k: s[k] for k in keys if s.get(k) is not None}


def _find_ollama_exe():
    found = shutil.which('ollama')
    if found:
        return found
    for base in (os.getenv('LOCALAPPDATA'), os.getenv('ProgramFiles'), os.getenv('ProgramW6432')):
        if base:
            for p in (Path(base) / 'Programs' / 'Ollama' / 'ollama.exe', Path(base) / 'Ollama' / 'ollama.exe'):
                if p.exists():
                    return str(p)
    return None


def _hardware():
    info = {'cpuThreads': os.cpu_count(), 'ramGb': None, 'gpu': None}
    if os.name == 'nt':
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX(); m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            info['ramGb'] = round(m.ullTotalPhys / 1024 ** 3, 1)
            info['ramFreeGb'] = round(m.ullAvailPhys / 1024 ** 3, 1)
    else:
        try:
            pages, size = os.sysconf('SC_PHYS_PAGES'), os.sysconf('SC_PAGE_SIZE')
            info['ramGb'] = round(pages * size / 1024 ** 3, 1)
        except (ValueError, OSError):
            pass
    smi = shutil.which('nvidia-smi')
    if smi:
        try:
            out = subprocess.run([smi, '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                                 capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            if out:
                name, mem = [x.strip() for x in out[0].split(',')[:2]]
                info['gpu'] = {'name': name, 'vramGb': round(int(mem) / 1024, 1)}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return info


# ------------------------------------------------------------------ status & server
_server_proc = None


@router.get('/status')
def status():
    version = _ollama_up()
    installed, running = [], []
    if version:
        installed = _ollama('GET', '/api/tags').get('models', [])
        running = _ollama('GET', '/api/ps').get('models', [])
    return {
        'ollama': {'running': bool(version), 'version': version, 'url': OLLAMA, 'installed': bool(version or _find_ollama_exe()),
                   'startedByPortal': _server_proc is not None and _server_proc.poll() is None},
        'installed': [{'name': m['name'], 'size': m.get('size'), 'modified': m.get('modified_at'),
                       'family': (m.get('details') or {}).get('family'), 'parameters': (m.get('details') or {}).get('parameter_size'),
                       'quantization': (m.get('details') or {}).get('quantization_level')} for m in installed],
        'running': [{'name': m['name'], 'size': m.get('size'), 'vram': m.get('size_vram'), 'expires': m.get('expires_at')} for m in running],
        'catalogue': CATALOGUE, 'settings': settings(), 'hardware': _hardware(), 'downloads': _pull_state(),
    }


@router.post('/server/start')
def start_server():
    global _server_proc
    if _ollama_up():
        return {'running': True, 'message': 'Ollama is already running.'}
    exe = _find_ollama_exe()
    if not exe:
        raise HTTPException(404, 'Ollama is not installed on this PC. Install it from https://ollama.com/download (or run: winget install Ollama.Ollama), then try again.')
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / 'ollama.log', 'ab')
    flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
    _server_proc = subprocess.Popen([exe, 'serve'], stdout=log, stderr=log, creationflags=flags)
    for _ in range(30):
        time.sleep(0.5)
        if _ollama_up():
            return {'running': True, 'message': 'Ollama started.'}
    raise HTTPException(504, 'Ollama did not start within 15 seconds. See logs\\ollama.log.')


@router.post('/server/stop')
def stop_server():
    global _server_proc
    if _ollama_up():
        for m in _ollama('GET', '/api/ps').get('models', []):
            _ollama('POST', '/api/generate', json={'model': m['name'], 'prompt': '', 'keep_alive': 0}, timeout=60)
    if _server_proc is not None and _server_proc.poll() is None:
        _server_proc.terminate()
        _server_proc = None
        return {'running': False, 'message': 'Models unloaded and Ollama stopped.'}
    return {'running': bool(_ollama_up()),
            'message': 'All models were unloaded from memory. Ollama itself was started outside the portal (the Ollama app); quit it from its icon in the system tray if you want it fully stopped.'}


# ------------------------------------------------------------------ downloads
_pulls = {}
_pull_lock = threading.Lock()


def _pull_state():
    with _pull_lock:
        return [{k: v for k, v in p.items() if k != 'cancel'} for p in _pulls.values()]


def _pull_worker(name):
    layers = {}
    try:
        with httpx.stream('POST', OLLAMA + '/api/pull', json={'model': name, 'stream': True}, timeout=httpx.Timeout(30, read=None)) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                with _pull_lock:
                    p = _pulls[name]
                    if p['cancel']:
                        p.update(status='cancelled', done=True)
                        return
                msg = json.loads(line)
                if msg.get('error'):
                    raise RuntimeError(msg['error'])
                if msg.get('digest') and msg.get('total'):
                    layers[msg['digest']] = (msg.get('completed') or 0, msg['total'])
                total = sum(t for _, t in layers.values())
                done = sum(c for c, _ in layers.values())
                with _pull_lock:
                    p.update(status=msg.get('status', ''), completed=done, total=total,
                             percent=round(done / total * 100, 1) if total else p.get('percent', 0))
                    if msg.get('status') == 'success':
                        p.update(done=True, percent=100, status='Downloaded')
                        return
        with _pull_lock:
            _pulls[name].update(done=True)
    except Exception as e:  # network errors, unknown model names, Ollama stopping
        with _pull_lock:
            _pulls[name].update(done=True, error=str(e) or e.__class__.__name__, status='failed')


class PullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


@router.post('/models/pull')
def pull_model(req: PullRequest):
    name = req.model.strip()
    if not re.fullmatch(r'[A-Za-z0-9._:/-]+', name):
        raise HTTPException(400, 'That is not a valid model name, e.g. llama3.2:3b')
    if not _ollama_up():
        raise HTTPException(503, 'Start Ollama first.')
    with _pull_lock:
        if name in _pulls and not _pulls[name]['done']:
            return {'started': False, 'message': 'Already downloading.'}
        _pulls[name] = {'model': name, 'status': 'starting', 'percent': 0, 'completed': 0, 'total': 0,
                        'done': False, 'error': None, 'cancel': False, 'started': datetime.now().isoformat(timespec='seconds')}
    threading.Thread(target=_pull_worker, args=(name,), daemon=True).start()
    return {'started': True}


@router.get('/models/pull')
def pull_progress():
    return {'downloads': _pull_state()}


@router.post('/models/pull/cancel')
def cancel_pull(req: PullRequest):
    with _pull_lock:
        if req.model in _pulls:
            _pulls[req.model]['cancel'] = True
    return {'cancelled': True}


@router.delete('/models/{name:path}')
def delete_model(name: str):
    _ollama('DELETE', '/api/delete', json={'model': name})
    with _pull_lock:
        _pulls.pop(name, None)
    return {'deleted': name}


@router.post('/models/{name:path}/load')
def load_model(name: str):
    t0 = time.time()
    _ollama('POST', '/api/generate', json={'model': name, 'prompt': '', 'keep_alive': settings()['keep_alive'],
                                           'options': _options(settings())}, timeout=300)
    return {'loaded': name, 'seconds': round(time.time() - t0, 1)}


@router.post('/models/{name:path}/unload')
def unload_model(name: str):
    _ollama('POST', '/api/generate', json={'model': name, 'prompt': '', 'keep_alive': 0}, timeout=60)
    return {'unloaded': name}


# ------------------------------------------------------------------ optimise
class Settings(BaseModel):
    defaultModel: str = ''
    temperature: float = Field(0.2, ge=0, le=2)
    top_p: float = Field(0.9, gt=0, le=1)
    num_ctx: int = Field(4096, ge=512, le=131072)
    num_predict: int = Field(512, ge=32, le=8192)
    num_thread: int | None = Field(None, ge=1, le=256)
    num_gpu: int | None = Field(None, ge=0, le=999)
    repeat_penalty: float = Field(1.1, ge=0.5, le=2)
    keep_alive: str = Field('30m', pattern=r'^(-1|0|\d+[smh]?)$')


@router.get('/settings')
def get_settings():
    return settings()


@router.put('/settings')
def put_settings(s: Settings):
    _write(SETTINGS_FILE, s.model_dump())
    return settings()


@router.get('/settings/recommend')
def recommend():
    hw = _hardware()
    ram, gpu = hw.get('ramGb') or 8, hw.get('gpu')
    vram = gpu['vramGb'] if gpu else 0
    budget = max(ram * 0.5, vram)  # leave room for Windows, the database and the browser
    fits = [m for m in CATALOGUE if m['minRamGb'] <= max(ram, vram * 2)] or CATALOGUE[:1]
    # Plenty of memory: the largest model that fits. Otherwise the balanced 3B model if it fits.
    balanced = next((m for m in fits if m['name'] == 'llama3.2:3b'), None)
    model = (fits[-1] if budget >= 8 or not balanced else balanced)['name']
    threads = max(2, (hw.get('cpuThreads') or 4) // 2)  # roughly the physical cores
    return {
        'model': model,
        'settings': {**settings(), 'num_ctx': 8192 if budget >= 12 else 4096,  # portal figures need about 1,200 tokens
                     'num_thread': threads, 'num_gpu': None if vram >= 4 else 0, 'keep_alive': '30m' if ram >= 12 else '10m'},
        'why': [f"{hw.get('ramGb')} GB RAM, {hw.get('cpuThreads')} CPU threads" + (f", GPU {gpu['name']} ({vram} GB)" if gpu else ', no NVIDIA GPU found'),
                f"Suggested model {model}: the largest that fits comfortably alongside the portal.",
                f"Reading window {8192 if budget >= 12 else 4096} tokens: room for the portal figures, the conversation and the answer.",
                f"{threads} CPU threads: about one per physical core." + (' GPU used automatically.' if vram >= 4 else ' CPU only (no suitable GPU).')],
        'hardware': hw,
    }


# ------------------------------------------------------------------ train (custom assistants)
class Example(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=4000)
    # 'answer': the exact reply wanted (taught as an example conversation).
    # 'style': guidance on how to answer this kind of question (added to the instructions instead).
    kind: str = Field('answer', pattern='^(answer|style)$')


class Assistant(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    baseModel: str = ''
    domain: str = Field('', max_length=4000)
    instructions: str = Field('', max_length=8000)
    style: str = 'concise'
    language: str = 'English'
    examples: list[Example] = Field([], max_length=500)
    usePortalData: bool = True


def _system_prompt(a):
    parts = [f"You are \"{a['name']}\", an assistant in Zimbabwe's National Health Portal."]
    if a.get('domain'):
        parts.append('Domain: ' + a['domain'].strip())
    if a.get('instructions'):
        parts.append(a['instructions'].strip())
    parts.append('How to answer: ' + STYLES.get(a.get('style'), STYLES['concise']))
    if a.get('language') and a['language'] != 'English':
        parts.append(f"Always answer in {a['language']}.")
    guidance = [ex for ex in a.get('examples', []) if ex.get('kind') == 'style']
    if guidance:
        parts.append('How to answer particular kinds of questions:\n' + '\n'.join(
            f'- When asked something like "{ex["question"].strip()}": {ex["answer"].strip()}' for ex in guidance))
    if a.get('usePortalData'):
        parts.append('Figures come from a DATA CONTEXT message with aggregated portal data for the user\'s current filters. '
                     'Use only those figures; if something is not in it, say so. Never invent numbers or patient details.')
    return '\n\n'.join(parts)


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:40] or 'assistant'


@router.get('/assistants')
def list_assistants():
    return {'assistants': assistants(), 'styles': list(STYLES)}


@router.put('/assistants')
def save_assistant(a: Assistant):
    items = assistants()
    data = a.model_dump()
    data['id'] = a.id or f"{_slug(a.name)}-{uuid.uuid4().hex[:6]}"
    old = next((x for x in items if x['id'] == data['id']), None)
    keep = {k: old.get(k) for k in ('modelName', 'builtAt', 'builtFrom')} if old else {'modelName': '', 'builtAt': None, 'builtFrom': None}
    data.update(keep)
    data['needsRebuild'] = bool(old and old.get('builtAt'))
    items = [x for x in items if x['id'] != data['id']] + [data]
    _write(ASSISTANTS_FILE, items)
    return data


@router.delete('/assistants/{aid}')
def delete_assistant(aid: str):
    items = assistants()
    a = next((x for x in items if x['id'] == aid), None)
    if not a:
        raise HTTPException(404, 'Unknown assistant.')
    if a.get('modelName') and _ollama_up():
        try:
            _ollama('DELETE', '/api/delete', json={'model': a['modelName']})
        except HTTPException:
            pass
    _write(ASSISTANTS_FILE, [x for x in items if x['id'] != aid])
    return {'deleted': aid}


@router.post('/assistants/{aid}/build')
def build_assistant(aid: str):
    """Create a custom Ollama model: base model + system prompt + example answers + settings."""
    items = assistants()
    a = next((x for x in items if x['id'] == aid), None)
    if not a:
        raise HTTPException(404, 'Unknown assistant.')
    base = a.get('baseModel') or settings().get('defaultModel')
    if not base:
        raise HTTPException(400, 'Choose a base model for this assistant first (download one on the Models tab).')
    name = f"impilo-{_slug(a['name'])}"
    system = _system_prompt(a)
    params = {k: v for k, v in _options(settings()).items() if k in ('temperature', 'top_p', 'num_ctx', 'repeat_penalty')}
    messages = []
    for ex in a.get('examples', []):
        if ex.get('kind', 'answer') != 'answer':
            continue
        messages += [{'role': 'user', 'content': ex['question']}, {'role': 'assistant', 'content': ex['answer']}]
    try:  # Ollama 0.5.5 and later
        _ollama('POST', '/api/create', json={'model': name, 'from': base, 'system': system, 'parameters': params,
                                             'messages': messages, 'stream': False}, timeout=600)
    except HTTPException as e:
        if e.status_code != 400:
            raise
        # Older Ollama: describe the same model as a Modelfile.
        mf = [f'FROM {base}', 'SYSTEM """' + system.replace('"""', "'''") + '"""']
        mf += [f'PARAMETER {k} {v}' for k, v in params.items()]
        for m in messages:
            mf.append(f'MESSAGE {m["role"]} """' + m['content'].replace('"""', "'''") + '"""')
        _ollama('POST', '/api/create', json={'name': name, 'modelfile': '\n'.join(mf), 'stream': False}, timeout=600)
    a.update(modelName=name, builtAt=datetime.now().isoformat(timespec='seconds'), builtFrom=base, needsRebuild=False)
    _write(ASSISTANTS_FILE, items)
    return a


# ------------------------------------------------------------------ chat
_context_cache = {}


def _portal_context(f):
    """Aggregated figures for the user's filters, as compact text for the model (cached 5 minutes)."""
    from main import national_dashboard  # late import: main imports this module
    key = json.dumps(f, sort_keys=True)
    hit = _context_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    d = national_dashboard(from_date=_d(f.get('from')), to_date=_d(f.get('to')), province=f.get('province') or None,
                           district=f.get('district') or None, type=f.get('type') or None, facility=f.get('facility') or None)
    p, c, s = d['period'], d['coverage'], d['scope']
    lines = [f"Selection: {s['label']}" + (f" ({s['typeLabel']})" if s.get('typeLabel') else ''),
             f"Period: {p['from']} to {p['to']} (data available up to {p['latestDataDate']}" + ('; the last month is partial' if p.get('partialLastMonth') else '') + ')',
             f"Facilities in selection: {c['facilities']} ({c['hospitals']} hospitals, {c['clinics']} clinics); "
             f"reporting to the warehouse: {c['reporting']}" + (f" ({', '.join(r['name'] for r in d['reportingFacilities'])})" if d['reportingFacilities'] else '')]
    lines.append('Facilities by type: ' + ', '.join(f"{t['label']} {t['facilities']}" for t in c['byType'] if t['facilities']))
    if c.get('byProvince') and s['level'] == 'national':
        lines.append('Facilities by province (reporting): ' + ', '.join(f"{a['name']} {a['facilities']} ({a['reporting']})" for a in c['byProvince']))
    ind = d.get('indicators')
    if not ind:
        lines.append('No facility in this selection reports activity data yet, so there are no activity figures.')
    else:
        months = ind['monthly']['months']
        lines.append('Indicator totals for the period, with sex split and monthly values (' + ', '.join(months) + '):')
        for v in ind['series'].values():
            sx = v['sex']
            lines.append(f"- {v['label']}: {v['total']} (male {sx['male']}, female {sx['female']}" + (f", other/unknown {sx['other']}" if sx['other'] else '') +
                         f"); by month: {', '.join(str(x) for x in v['monthly'])}")
        k = ind['kpis']
        lines.append(f"Unique patients: {k['uniquePatients']}. HIV positivity: {k['htsPositivity']}%. Malaria positivity: {k['malariaPositivity']}%. ANC first bookings: {k['ancFirstBookings']}.")
        lines.append('Attendances by age group: ' + ', '.join(f"{a['label']} {a['count']}" for a in ind['ageBands']))
        # Age groups and sex for the HIV indicators, so "which age group..." questions can be answered.
        for key in ('hivPositive', 'htsTests'):
            v = ind['series'].get(key)
            if v:
                bands = [f"{b['band']}: {b['male']} male, {b['female']} female" for b in v['pyramid'] if b['male'] or b['female']]
                lines.append(f"{v['label']} by age group: " + ('; '.join(bands) or 'none'))
        # Figures per reporting facility, so "which facility has the most..." questions can be answered.
        names = {r['id']: r for r in d['reportingFacilities']}
        per_fac = sorted(d.get('byFacility') or [], key=lambda f: -(f['values'].get('attendances') or 0))[:30]
        if per_fac:
            labels = {k: v['label'] for k, v in ind['series'].items()}
            lines.append('Figures by reporting facility (only these facilities send data; any other facility has no data, which is not the same as zero):')
            for f in per_fac:
                r = names.get(f['id'], {})
                lines.append(f"- {r.get('name', f['id'])} ({r.get('typeLabel', '')}, {r.get('district', '')}, {r.get('province', '')}): " +
                             ', '.join(f"{labels[k]} {f['values'][k]}" for k in labels if k in f['values']))
        # Figures per area (provinces nationally, districts inside a province).
        ba = d.get('byArea')
        if ba:
            show = ('hivPositive', 'htsTests', 'opd', 'attendances', 'ancBookings', 'artNew')
            with_data = [a for a in ba['areas'] if a['reporting']]
            no_data = [a['name'] for a in ba['areas'] if not a['reporting']]
            lines.append(f"Figures by {ba['level']} (areas with reporting facilities):")
            for a in with_data:
                lines.append(f"- {a['name']}: " + ', '.join(f"{ind['series'][k]['label']} {a['values'].get(k)}" for k in show if k in a['values']))
            if no_data:
                lines.append(f"{ba['level'].capitalize()}s with no reporting facilities (no data, not zero): " + ', '.join(no_data))
        lines.append('Top diagnoses: ' + '; '.join(f"{t['name']} ({t['code']}) {t['count']}" for t in ind['topDiagnoses']))
        dc = ind['dataCompleteness']
        lines.append(f"Record completeness (share of attendances): diagnosis {dc['diagnosisRecorded']}%, medication {dc['medicationRecorded']}%, "
                     f"TB screening {dc['tbScreeningRecorded']}%, blood pressure {dc['bloodPressureRecorded']}%.")
    text = 'DATA CONTEXT (aggregated figures from the National Health Portal; no patient-level data):\n' + '\n'.join(lines)
    _context_cache[key] = (time.time(), text)
    return text


def _d(v):
    return date.fromisoformat(v) if v else None


class ChatMessage(BaseModel):
    role: str = Field(pattern='^(user|assistant)$')
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = []
    assistantId: str | None = None
    filters: dict = {}
    requestId: str | None = Field(default=None, max_length=64)


class CancelRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=64)


# Answers being written, by the browser's requestId. Stopping closes the connection to Ollama, which makes
# Ollama stop generating at once (just dropping the browser connection leaves the model running).
_active = {}  # requestId -> {'cancelled': threading.Event, 'response': httpx.Response | None}
_active_lock = threading.Lock()


@router.post('/chat/cancel')
def cancel_chat(req: CancelRequest):
    with _active_lock:
        job = _active.get(req.requestId)
    if not job:
        return {'cancelled': False}
    job['cancelled'].set()
    r = job.get('response')
    if r is not None:
        try: r.close()
        except Exception: pass
    return {'cancelled': True}


def _chart_for(req):
    """A chart for questions that ask for one (worked out from the data, not by the model)."""
    try:
        import charts, geo
        p = charts.plan(req.message, [m.model_dump() for m in req.history])
        if not p: return None, None
        m = geo._main()
        latest = m._with_registry(lambda c: m._facility(c))['latestDate'] or date.today()
        return charts.build(p, latest)
    except Exception:
        return None, None  # a chart is a bonus: never let it stop the answer


@router.post('/chat')
def chat(req: ChatRequest):
    rid = req.requestId or uuid.uuid4().hex
    job = {'cancelled': threading.Event(), 'response': None, 'started': time.time()}
    with _active_lock:
        for k in [k for k, v in _active.items() if time.time() - v['started'] > 900]:
            _active.pop(k, None)  # requests the browser left before the answer began
        _active[rid] = job
    chart, figures = _chart_for(req)
    up = _ollama_up()
    s = settings()
    a = next((x for x in assistants() if x['id'] == req.assistantId), None) or assistants()[0]
    model = a.get('modelName') or a.get('baseModel') or s.get('defaultModel')
    if not up or not model:
        if chart:  # the chart doesn't need the model: show it on its own
            def only_chart():
                yield json.dumps({'type': 'meta', 'assistant': a['name'], 'grounded': True}) + '\n'
                yield json.dumps({'type': 'chart', 'chart': chart}) + '\n'
                yield json.dumps({'type': 'token', 'text': "Here's the chart from the portal's figures. (The assistant isn't available to describe it right now.)"}) + '\n'
                yield json.dumps({'type': 'done'}) + '\n'
            return StreamingResponse(only_chart(), media_type='application/x-ndjson', headers={'Cache-Control': 'no-store'})
        if not up: raise HTTPException(503, 'The assistant is not available right now. Ask an administrator to start it (Configuration › Models).')
        raise HTTPException(400, 'The assistant is not set up yet. Ask an administrator to choose a model (Configuration › Models).')
    messages = []
    if not a.get('modelName'):  # built assistants already carry their instructions and examples
        messages.append({'role': 'system', 'content': _system_prompt(a)})
        for ex in a.get('examples', []):
            if ex.get('kind', 'answer') != 'answer':
                continue
            messages += [{'role': 'user', 'content': ex['question']}, {'role': 'assistant', 'content': ex['answer']}]
    context = None
    if a.get('usePortalData'):
        context = _portal_context({k: req.filters.get(k) for k in ('province', 'district', 'type', 'facility', 'from', 'to')})
        messages.append({'role': 'system', 'content': context})
    messages += [m.model_dump() for m in req.history[-6:]] + [{'role': 'user', 'content': req.message}]
    if figures:
        messages.insert(len(messages) - 1, {'role': 'system', 'content': figures})
    opts = _options(s)
    if context and (opts.get('num_ctx') or 0) < 4096:
        # The portal figures alone are ~1,200 tokens; a smaller window would silently cut them off.
        opts['num_ctx'] = 4096

    def stream():
        try:
            yield from _stream()
        finally:
            with _active_lock:
                _active.pop(rid, None)

    def _stream():
        yield json.dumps({'type': 'meta', 'assistant': a['name'], 'grounded': bool(context or chart)}) + '\n'
        if chart:
            yield json.dumps({'type': 'chart', 'chart': chart}, default=str) + '\n'
        # Reasoning models (qwen3, deepseek-r1, ...) "think" before answering. Ask them not to, so the whole
        # answer length goes to the reply; if a model still sends <think>...</think>, it is left out.
        body = {'model': model, 'messages': messages, 'stream': True, 'options': opts, 'keep_alive': s['keep_alive'], 'think': False}
        for attempt in (1, 2):
            sent, thinking, buf = False, False, ''
            if job['cancelled'].is_set():
                return
            try:
                with httpx.stream('POST', OLLAMA + '/api/chat', json=body, timeout=httpx.Timeout(30, read=600)) as r:
                    job['response'] = r
                    if job['cancelled'].is_set():
                        return
                    if r.status_code >= 400:
                        r.read()
                        err = r.json().get('error', r.text) if r.content else str(r.status_code)
                        if attempt == 1 and 'think' in str(err).lower():
                            body.pop('think', None); continue  # older Ollama or a model without the option
                        yield json.dumps({'type': 'error', 'message': 'The assistant could not answer right now. Please try again, or ask an administrator to check the AI configuration.'}) + '\n'
                        return
                    for line in r.iter_lines():
                        if job['cancelled'].is_set():
                            return
                        if not line:
                            continue
                        msg = json.loads(line)
                        if msg.get('error'):
                            yield json.dumps({'type': 'error', 'message': 'The assistant stopped unexpectedly. Please try again.'}) + '\n'
                            return
                        buf += (msg.get('message') or {}).get('content', '')
                        out = ''
                        while buf:
                            if thinking:
                                end = buf.find('</think>')
                                if end < 0: buf = buf[-8:]; break
                                buf, thinking = buf[end + 8:], False
                            else:
                                start = buf.find('<think>')
                                if start < 0:
                                    keep = next((k for k in range(min(7, len(buf)), 0, -1) if '<think>'.startswith(buf[-k:])), 0)
                                    out, buf = out + buf[:len(buf) - keep], buf[len(buf) - keep:]
                                    break
                                out, buf, thinking = out + buf[:start], buf[start + 7:], True
                        if out and (sent or out.strip()):
                            text = out if sent else out.lstrip()
                            sent = True
                            yield json.dumps({'type': 'token', 'text': text}) + '\n'
                        if msg.get('done'):
                            if buf and not thinking: yield json.dumps({'type': 'token', 'text': buf}) + '\n'; sent = True
                            if not sent:
                                yield json.dumps({'type': 'error', 'message': "The assistant ran out of room before it finished its answer. Try asking again, or ask an administrator to raise the answer length (Configuration › Optimise)."}) + '\n'
                                return
                            yield json.dumps({'type': 'done'}) + '\n'
                return
            except Exception:
                if job['cancelled'].is_set():
                    return  # stopped by the person: closing the connection lands here
                yield json.dumps({'type': 'error', 'message': 'The assistant is not reachable right now. Please try again shortly.'}) + '\n'
                return

    return StreamingResponse(stream(), media_type='application/x-ndjson', headers={'Cache-Control': 'no-store'})


# ------------------------------------------------------------------ example files
# Upload CSV, Excel (.xlsx) or JSON with columns: question, answer, and optionally type
# ("answer" = exact reply, "style" = how to answer). A separate "how to answer" / "answer style" column
# also works: a row with only that filled in becomes a style example.
_Q = {'question', 'questions', 'q', 'prompt', 'ask'}
_A = {'answer', 'answers', 'a', 'expected answer', 'response', 'reply'}
_S = {'answer style', 'style', 'how to answer', 'guidance', 'instruction', 'instructions'}
_T = {'type', 'kind', 'answer type'}


def _norm(h):
    return re.sub(r'[^a-z ]+', ' ', str(h or '').lower()).strip()


def _rows_from_file(name, data):
    ext = Path(name or '').suffix.lower()
    if ext in ('.csv', '.txt'):
        import csv, io
        text = data.decode('utf-8-sig', errors='replace')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    elif ext == '.xlsx':
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [['' if c is None else str(c) for c in r] for r in wb.worksheets[0].iter_rows(values_only=True)]
    elif ext == '.json':
        items = json.loads(data.decode('utf-8-sig'))
        items = items.get('examples', items) if isinstance(items, dict) else items
        if not isinstance(items, list) or not items:
            raise ValueError('The JSON file should be a list of objects with question and answer.')
        keys = list(dict.fromkeys(k for it in items if isinstance(it, dict) for k in it))
        rows = [keys] + [[str(it.get(k, '') or '') for k in keys] for it in items if isinstance(it, dict)]
    else:
        raise ValueError('Upload a .csv, .xlsx or .json file.')
    return [r for r in rows if any(str(c).strip() for c in r)]


@router.post('/examples/parse')
async def parse_examples(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(413, 'The file is larger than 2 MB.')
    try:
        rows = _rows_from_file(file.filename, data)
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, str(e) or 'Could not read the file.')
    except Exception:
        raise HTTPException(400, 'Could not read the file. Check it is a valid CSV, Excel or JSON file.')
    if len(rows) < 2:
        raise HTTPException(400, 'The file needs a header row (question, answer) and at least one example.')
    head = [_norm(h) for h in rows[0]]
    col = lambda names: next((i for i, h in enumerate(head) if h in names), None)
    qi, ai_, si, ti = col(_Q), col(_A), col(_S), col(_T)
    if qi is None or (ai_ is None and si is None):
        raise HTTPException(400, 'Could not find the columns. The first row should name them: question, answer (and optionally type).')
    get = lambda r, i: str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ''
    examples, skipped = [], []
    for n, r in enumerate(rows[1:], start=2):
        q, ans, sty, typ = get(r, qi), get(r, ai_), get(r, si), get(r, ti).lower()
        if not q:
            skipped.append({'row': n, 'reason': 'no question'}); continue
        is_style = typ in ('style', 'answer style', 'how to answer', 'guidance') or (not ans and sty)
        text = (sty or ans) if is_style else ans
        if not text:
            skipped.append({'row': n, 'reason': 'no answer or answer style'}); continue
        examples.append({'question': q[:2000], 'answer': text[:4000], 'kind': 'style' if is_style else 'answer'})
        if not is_style and sty:  # both filled in: keep the exact answer and the guidance
            examples.append({'question': q[:2000], 'answer': sty[:4000], 'kind': 'style'})
    if not examples:
        raise HTTPException(400, 'No usable rows: each row needs a question and an answer or answer style.')
    return {'examples': examples[:500], 'skipped': skipped, 'rows': len(rows) - 1, 'truncated': len(examples) > 500}
