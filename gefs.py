import io
import os
import requests

_BASE_URL = os.environ.get("SUPABASE_URL", "").rstrip('/')
_KEY = os.environ.get("SUPABASE_SECRET", "")
_BUCKET = "habsim"
_SESSION = requests.Session()
_COMMON_HEADERS = {
    "Authorization": f"Bearer {_KEY}",
    "apikey": _KEY,
}

def _object_url(path: str) -> str:
    return f"{_BASE_URL}/storage/v1/object/{path}"

def _list_url(bucket: str) -> str:
    return f"{_BASE_URL}/storage/v1/object/list/{bucket}"

def listdir_gefs():
    resp = _SESSION.post(_list_url(_BUCKET), headers=_COMMON_HEADERS, json={"prefix": ""})
    resp.raise_for_status()
    items = resp.json()
    return [item.get('name') for item in items]

def open_gefs(file_name):
    resp = _SESSION.get(_object_url(f"{_BUCKET}/{file_name}"), headers=_COMMON_HEADERS)
    resp.raise_for_status()
    return io.StringIO(resp.content.decode("utf-8"))

def load_gefs(file_name):
    resp = _SESSION.get(_object_url(f"{_BUCKET}/{file_name}"), headers=_COMMON_HEADERS)
    resp.raise_for_status()
    return io.BytesIO(resp.content)

def download_gefs(file_name):
    resp = _SESSION.get(_object_url(f"{_BUCKET}/{file_name}"), headers=_COMMON_HEADERS)
    resp.raise_for_status()
    return resp.content
