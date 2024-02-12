import io

import supabase

import os
from supabase import create_client, Client

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_SECRET")
supabase: Client = create_client(url, key)

bucket = supabase.storage.from_('habsim')

def listdir_gefs():
    files_info = bucket.list()
    file_names = []
    for file_info in files_info:
        file_names.append(file_info['name'])
    return file_names

def open_gefs(file_name):
    res = bucket.download(file_name)
    f = io.StringIO(res.decode("utf-8"))
    return f

def load_gefs(file_name):
    res = bucket.download(file_name)
    f = io.BytesIO(res)
    return f

def download_gefs(file_name):
    res = bucket.download(file_name)
    return res
